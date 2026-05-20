"""
FreightPOP Copilot Eval - Scorer v3 (trace-aware)

Reads two CSVs and scores each matched case against its expected trajectory.

Required columns:
    Traces CSV  : traceId, sessionId, output, startTime, datasetItemId
    Dataset CSV : id, input, expectedOutput, metadata

The metadata column must contain a JSON object with:
    expected_trajectory_options : list[list[str]]   — at least one must match
    must_not                    : list[str]          — tools forbidden in trace
    case_number                 : str               — e.g. "03"

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


def parse_metadata(raw: str) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def parse_tool_calls(raw: str) -> list[str]:
    """Accept JSON list or comma-separated string."""
    if not raw:
        return []
    raw = raw.strip()
    if raw.startswith("["):
        try:
            return json.loads(raw)
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
    meta = parse_metadata(dataset_item.get("metadata", ""))
    case_number = str(meta.get("case_number", "")).zfill(2)
    case_id = f"case_{case_number}" if case_number else f"item_{dataset_item.get('id', 'unknown')}"
    trace_id = trace.get("traceId", "")

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

    # Check must_not (forbidden tools)
    must_not: list[str] = meta.get("must_not", [])
    for forbidden in must_not:
        if forbidden in actual_tools:
            passed = False
            reason = f"forbidden_tool_called — {forbidden}"
            checks.append(f"FAIL: forbidden tool '{forbidden}' was called")
            break
    if not passed:
        return {"case_id": case_id, "trace_id": trace_id, "passed": passed, "reason": reason, "checks": checks}

    # Check expected_trajectory_options
    options: list[list[str]] = meta.get("expected_trajectory_options", [])
    if not options:
        # No trajectory spec — pass by default
        checks.append("PASS: no expected_trajectory_options defined, treating as pass")
        return {"case_id": case_id, "trace_id": trace_id, "passed": True, "reason": "ok", "checks": checks}

    matched_option = None
    for option in options:
        if trajectory_matches(actual_tools, option):
            matched_option = option
            break

    if matched_option is None:
        # Find the closest option to give a useful error message
        passed = False
        best_option = options[0]
        # Check if it looks like a name mismatch vs a sequence mismatch
        actual_set = set(actual_tools)
        expected_set = set(best_option)
        unexpected = actual_set - expected_set
        missing = expected_set - actual_set

        if missing and not unexpected:
            first_missing = next(iter(missing))
            reason = f"missing_required_param — {first_missing}"
        elif unexpected and len(best_option) == 1:
            reason = f"tool_name_mismatch — expected {best_option[0]}, got {actual_tools[0] if actual_tools else 'nothing'}"
        else:
            reason = "no expected_trajectory matched"
        checks.append(f"FAIL: actual={actual_tools}, options={options}")
    else:
        checks.append(f"PASS: matched trajectory {matched_option}")

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
                "case_id":  f"unmatched_{trace.get('traceId','?')[:8]}",
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
