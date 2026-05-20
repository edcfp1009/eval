"""
FreightPOP Copilot Eval - Scorer v3 (trace-aware)

Reads two CSVs and scores each matched case against its expected trajectory.

Required columns:
    Traces CSV  : traceId, sessionId, output, startTime, datasetItemId, tool_calls
    Dataset CSV : id, input, expectedOutput, metadata

The expectedOutput column must contain a JSON object with:
    tool_calls : list[{"tool": str, "args_must_include": dict}]
    must_not   : list[str]  — abstract behavior labels (not auto-checkable)

The metadata column may optionally contain:
    case_number : str|int  — e.g. "03" or 3 (used to build case_id label)

Output (one JSON object per line, written to stdout):
    {"case_id":"case_03","trace_id":"...","passed":false,
     "reason":"tool_name_mismatch — expected updateFields, got navigateToPage",
     "checks":[...]}

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


def parse_tool_calls(raw: str) -> list[str]:
    """Accept JSON list or comma-separated string of tool names."""
    if not raw:
        return []
    raw = raw.strip()
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(t).strip() for t in parsed if t]
        except json.JSONDecodeError:
            pass
    return [t.strip() for t in raw.split(",") if t.strip()]


def trajectory_matches(actual: list[str], expected: list[str]) -> bool:
    """Ordered subsequence check: every tool in expected must appear in actual, in order."""
    if not expected:
        return True
    it = iter(actual)
    return all(tool in it for tool in expected)


def score_case(trace: dict, dataset_item: dict) -> dict:
    # Build case_id from metadata.case_number if present
    meta = parse_json(dataset_item.get("metadata", ""))
    raw_case = meta.get("case_number")
    if raw_case is not None:
        case_number = str(raw_case).zfill(2)
        case_id = f"case_{case_number}"
    else:
        case_id = f"item_{dataset_item.get('id', 'unknown')}"

    trace_id = trace.get("traceId", "")

    # Actual tool calls from trace
    tool_calls_raw = trace.get("tool_calls", "") or trace.get("output", "")
    actual_tools: list[str] = parse_tool_calls(tool_calls_raw)

    checks = []
    passed = True
    reason = None

    # Edge case: no tool calls at all
    if not actual_tools:
        return {
            "case_id": case_id,
            "trace_id": trace_id,
            "passed": False,
            "reason": "no_tool_calls_in_trace",
            "checks": ["no tool calls found in trace"],
        }

    # Scoring criteria live in expectedOutput (not metadata)
    expected_output = parse_json(dataset_item.get("expectedOutput", ""))

    # must_not: abstract behavior labels — cannot be auto-checked against tool names
    must_not: list[str] = expected_output.get("must_not", [])
    if must_not:
        checks.append(f"NOTE: must_not behaviors require manual review: {must_not}")

    # expected tool_calls: [{tool: "name", args_must_include: {...}}, ...]
    expected_entries: list[dict] = expected_output.get("tool_calls", [])

    if not expected_entries:
        # No tool call spec defined — pass by default
        checks.append("PASS: no tool_calls defined in expected_output")
        return {"case_id": case_id, "trace_id": trace_id, "passed": True, "reason": "ok", "checks": checks}

    expected_tool_names = [entry.get("tool", "") for entry in expected_entries if entry.get("tool")]

    if not expected_tool_names:
        checks.append("PASS: expected tool_calls entries have no tool names")
        return {"case_id": case_id, "trace_id": trace_id, "passed": True, "reason": "ok", "checks": checks}

    # Check ordered subsequence: all expected tools must appear in actual, in order
    if trajectory_matches(actual_tools, expected_tool_names):
        checks.append(f"PASS: matched expected tools {expected_tool_names}")
        # args_must_include cannot be checked — fetch_traces.py only captures tool names
        for entry in expected_entries:
            if entry.get("args_must_include"):
                checks.append(
                    f"NOTE: args_must_include for '{entry['tool']}' not auto-checked "
                    f"(args not captured): {entry['args_must_include']}"
                )
    else:
        passed = False
        actual_set = set(actual_tools)
        expected_set = set(expected_tool_names)
        missing = expected_set - actual_set
        unexpected = actual_set - expected_set

        if missing and not unexpected:
            first_missing = sorted(missing)[0]
            reason = f"missing_required_tool — {first_missing}"
        elif unexpected and len(expected_tool_names) == 1:
            got = actual_tools[0] if actual_tools else "nothing"
            reason = f"tool_name_mismatch — expected {expected_tool_names[0]}, got {got}"
        else:
            reason = "expected_tools_not_matched"
        checks.append(f"FAIL: actual={actual_tools}, expected={expected_tool_names}")

    return {
        "case_id": case_id,
        "trace_id": trace_id,
        "passed": passed,
        "reason": reason or "ok",
        "checks": checks,
    }


def main():
    parser = argparse.ArgumentParser(description="Score eval traces against dataset items")
    parser.add_argument("--traces",   required=True, help="Path to traces CSV")
    parser.add_argument("--dataset",  required=True, help="Path to dataset CSV")
    parser.add_argument("--run-name", required=True, help="Eval run label (informational)")
    args = parser.parse_args()

    traces_rows  = load_csv(args.traces)
    dataset_rows = load_csv(args.dataset)

    # Index dataset by id
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
