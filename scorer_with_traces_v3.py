"""
FreightPOP Copilot Eval - Scorer v3 (trace-aware)

Reads two CSVs and scores each matched case against its expected trajectory.

Required columns:
    Traces CSV  : traceId, sessionId, output, startTime, datasetItemId, tool_calls
    Dataset CSV : id, input, expectedOutput, metadata

Supported expectedOutput schemas (tried in order):
    tool_calls                     : list[{tool, args_must_include}] — ordered
    expected_trajectory_options    : list[{label, tools: list[str]}] — any option matches
    expected_trajectory            : list[{step, tool} | {step_group, tools_unordered}]
    expected_tools_unordered       : list[str]
    expected_tools_must_include    : list[str]
    tool_calls_that_must_not_happen: list[str] — LITERAL tool names forbidden

Note: must_not items are abstract behavior labels, not literal tool names.
      tool_calls_that_must_not_happen contains literal tool names.

Output (one JSON object per line, written to stdout):
    {"case_id":"case_03","trace_id":"...","passed":false,
     "reason":"...", "checks":[...]}

Exit code 0 always (caller inspects JSON lines).

Usage:
    python scorer_with_traces_v3.py \\
        --traces  /tmp/eval_traces.csv \\
        --dataset /tmp/eval_dataset.csv \\
        --run-name phase0_5.4mini_2026_05_20
"""

import argparse
import csv
import json
import sys


def load_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def parse_json(raw: str) -> dict:
    if not raw:
        return {}
    try:
        result = json.loads(raw)
        return result if isinstance(result, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def parse_tool_calls(raw) -> list[str]:
    """Accept JSON list or comma-separated string of tool names."""
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if t]
    raw = str(raw).strip()
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(t).strip() for t in parsed if t]
        except json.JSONDecodeError:
            pass
    return [t.strip() for t in raw.split(",") if t.strip()]


def ordered_subsequence_match(actual: list[str], expected: list[str]) -> bool:
    """All tools in expected must appear in actual, in order."""
    if not expected:
        return True
    it = iter(actual)
    return all(tool in it for tool in expected)


def all_present(actual: list[str], required: list[str]) -> tuple[bool, list[str]]:
    """Check if all required tools appear in actual (any order). Returns (ok, missing)."""
    actual_set = set(actual)
    missing = [t for t in required if t not in actual_set]
    return (len(missing) == 0), missing


def extract_trajectory_tools(trajectory: list) -> list[str]:
    """
    Flatten expected_trajectory into a list of required tool names.
    Handles both {step, tool} and {step_group, tools_unordered} shapes.
    """
    tools = []
    for step in trajectory:
        if isinstance(step, dict):
            if "tool" in step and step["tool"]:
                tools.append(step["tool"])
            if "tools_unordered" in step:
                tools.extend(step["tools_unordered"])
    return tools


def score_case(trace: dict, dataset_item: dict) -> dict:
    # Build case_id label
    meta = parse_json(dataset_item.get("metadata", ""))
    raw_case = meta.get("case_number")
    if raw_case is not None:
        case_id = f"case_{str(raw_case).zfill(2)}"
    else:
        case_id = f"item_{dataset_item.get('id', 'unknown')}"

    trace_id = trace.get("traceId", "")
    tool_calls_raw = trace.get("tool_calls", "") or trace.get("output", "")
    actual_tools: list[str] = parse_tool_calls(tool_calls_raw)

    checks: list[str] = []
    expected_output = parse_json(dataset_item.get("expectedOutput", ""))

    # ── 1. Check literal forbidden tools (tool_calls_that_must_not_happen) ──
    literal_forbidden: list[str] = expected_output.get("tool_calls_that_must_not_happen", [])
    for forbidden in literal_forbidden:
        if forbidden in actual_tools:
            checks.append(f"FAIL: forbidden tool '{forbidden}' was called")
            return {
                "case_id": case_id, "trace_id": trace_id,
                "passed": False,
                "reason": f"forbidden_tool_called — {forbidden}",
                "checks": checks,
            }

    # Note abstract must_not labels (not auto-checkable)
    must_not: list[str] = expected_output.get("must_not", [])
    if must_not:
        checks.append(f"NOTE: must_not behaviors require manual review: {must_not}")

    # ── 2. Determine expected tools by schema ────────────────────────────────

    # Schema A: tool_calls list (may be empty for behavior-only cases)
    if "tool_calls" in expected_output:
        entries: list = expected_output["tool_calls"]
        expected_names = [e["tool"] for e in entries if isinstance(e, dict) and e.get("tool")]

        if not expected_names:
            # Behavior-only: no tool calls required
            # No tool calls in trace is CORRECT for these cases
            checks.append("PASS: tool_calls=[] — behavior-only case, no tool calls required")
            return {"case_id": case_id, "trace_id": trace_id, "passed": True, "reason": "ok", "checks": checks}

        if not actual_tools:
            checks.append(f"FAIL: no tool calls in trace, expected {expected_names}")
            return {"case_id": case_id, "trace_id": trace_id, "passed": False,
                    "reason": "no_tool_calls_in_trace", "checks": checks}

        if ordered_subsequence_match(actual_tools, expected_names):
            checks.append(f"PASS: matched tool_calls {expected_names}")
            for e in entries:
                if isinstance(e, dict) and e.get("args_must_include"):
                    checks.append(
                        f"NOTE: args_must_include for '{e['tool']}' not auto-checked: {e['args_must_include']}"
                    )
            return {"case_id": case_id, "trace_id": trace_id, "passed": True, "reason": "ok", "checks": checks}
        else:
            ok, missing = all_present(actual_tools, expected_names)
            if missing:
                reason = f"missing_required_tool — {missing[0]}"
            elif len(expected_names) == 1:
                got = actual_tools[0] if actual_tools else "nothing"
                reason = f"tool_name_mismatch — expected {expected_names[0]}, got {got}"
            else:
                reason = "expected_tools_not_matched"
            checks.append(f"FAIL: actual={actual_tools}, expected={expected_names}")
            return {"case_id": case_id, "trace_id": trace_id, "passed": False, "reason": reason, "checks": checks}

    # Schema B: expected_trajectory_options — any one option must match
    if "expected_trajectory_options" in expected_output:
        options: list = expected_output["expected_trajectory_options"]
        if not actual_tools:
            # Check if any option requires zero tools (valid no-tool path)
            empty_option = next((o for o in options if not o.get("tools")), None)
            if empty_option:
                checks.append(f"PASS: no tool calls in trace, matched empty option '{empty_option.get('label', '?')}'")
                return {"case_id": case_id, "trace_id": trace_id, "passed": True, "reason": "ok", "checks": checks}
            checks.append(f"FAIL: no tool calls in trace, no empty option available")
            return {"case_id": case_id, "trace_id": trace_id, "passed": False,
                    "reason": "no_tool_calls_in_trace", "checks": checks}

        for option in options:
            option_tools: list[str] = option.get("tools") or []
            if not option_tools:
                # Empty tools option always matches (any behavior acceptable)
                checks.append(f"PASS: matched empty-tools option '{option.get('label', '?')}'")
                return {"case_id": case_id, "trace_id": trace_id, "passed": True, "reason": "ok", "checks": checks}
            ok, _ = all_present(actual_tools, option_tools)
            if ok:
                checks.append(f"PASS: matched option '{option.get('label', '?')}' — tools={option_tools}")
                return {"case_id": case_id, "trace_id": trace_id, "passed": True, "reason": "ok", "checks": checks}

        all_option_tools = [o.get("tools", []) for o in options]
        checks.append(f"FAIL: actual={actual_tools}, options={all_option_tools}")
        return {"case_id": case_id, "trace_id": trace_id, "passed": False,
                "reason": "no_expected_trajectory_option_matched", "checks": checks}

    # Schema C: expected_trajectory (ordered steps with possible unordered groups)
    if "expected_trajectory" in expected_output:
        trajectory: list = expected_output["expected_trajectory"]
        required_tools = extract_trajectory_tools(trajectory)
        if not required_tools:
            checks.append("PASS: expected_trajectory has no required tool names")
            return {"case_id": case_id, "trace_id": trace_id, "passed": True, "reason": "ok", "checks": checks}

        if not actual_tools:
            checks.append(f"FAIL: no tool calls in trace, expected trajectory tools {required_tools}")
            return {"case_id": case_id, "trace_id": trace_id, "passed": False,
                    "reason": "no_tool_calls_in_trace", "checks": checks}

        ok, missing = all_present(actual_tools, required_tools)
        if ok:
            checks.append(f"PASS: all expected_trajectory tools present: {required_tools}")
        else:
            checks.append(f"FAIL: actual={actual_tools}, missing trajectory tools={missing}")
            return {"case_id": case_id, "trace_id": trace_id, "passed": False,
                    "reason": f"missing_required_tool — {missing[0]}", "checks": checks}
        return {"case_id": case_id, "trace_id": trace_id, "passed": True, "reason": "ok", "checks": checks}

    # Schema D: expected_tools_unordered or expected_tools_must_include
    for field in ("expected_tools_unordered", "expected_tools_must_include"):
        if field in expected_output:
            required_tools: list[str] = expected_output[field]
            if not required_tools:
                checks.append(f"PASS: {field} is empty")
                return {"case_id": case_id, "trace_id": trace_id, "passed": True, "reason": "ok", "checks": checks}

            if not actual_tools:
                checks.append(f"FAIL: no tool calls in trace, expected {required_tools}")
                return {"case_id": case_id, "trace_id": trace_id, "passed": False,
                        "reason": "no_tool_calls_in_trace", "checks": checks}

            ok, missing = all_present(actual_tools, required_tools)
            if ok:
                checks.append(f"PASS: all {field} tools present: {required_tools}")
                return {"case_id": case_id, "trace_id": trace_id, "passed": True, "reason": "ok", "checks": checks}
            else:
                checks.append(f"FAIL: actual={actual_tools}, missing={missing}")
                return {"case_id": case_id, "trace_id": trace_id, "passed": False,
                        "reason": f"missing_required_tool — {missing[0]}", "checks": checks}

    # No scorable tool criteria found — note and pass
    checks.append("PASS: no auto-scorable tool criteria found in expected_output (manual review needed)")
    return {"case_id": case_id, "trace_id": trace_id, "passed": True, "reason": "no_scoring_criteria", "checks": checks}


def main():
    parser = argparse.ArgumentParser(description="Score eval traces against dataset items")
    parser.add_argument("--traces",   required=True, help="Path to traces CSV")
    parser.add_argument("--dataset",  required=True, help="Path to dataset CSV")
    parser.add_argument("--run-name", required=True, help="Eval run label (informational)")
    args = parser.parse_args()

    traces_rows  = load_csv(args.traces)
    dataset_rows = load_csv(args.dataset)

    dataset_by_id: dict[str, dict] = {row["id"]: row for row in dataset_rows}

    for trace in traces_rows:
        item_id = trace.get("datasetItemId", "")
        if not item_id or item_id not in dataset_by_id:
            result = {
                "case_id":  f"unmatched_{trace.get('traceId', '?')[:8]}",
                "trace_id": trace.get("traceId", ""),
                "passed":   False,
                "reason":   f"dataset_item_not_found — id={item_id!r}",
                "checks":   [],
            }
        else:
            result = score_case(trace, dataset_by_id[item_id])

        print(json.dumps(result, ensure_ascii=False))
        sys.stdout.flush()


if __name__ == "__main__":
    main()
