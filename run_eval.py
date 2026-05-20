"""
FreightPOP Copilot Eval - Run Orchestrator

One-command eval runner. Calls fetch_traces.py and scorer_with_traces_v3.py
as subprocesses, matches traces to dataset cases, scores them, and writes
results back to Langfuse.

Usage:
    python run_eval.py \\
        --run-name   "phase0_5.4mini_2026_05_20" \\
        --dataset-name "dataset_v2_full_17" \\
        --hours 24 \\
        [--limit 100] \\
        [--dry-run] \\
        [--debug]
"""

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import tempfile
import traceback

DIVIDER = "─" * 41


def load_env(debug: bool) -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        # Manual fallback
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    v = v.strip().strip('"').strip("'")
                    os.environ.setdefault(k.strip(), v)


def require_env() -> tuple[str, str, str]:
    pub  = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    sec  = os.environ.get("LANGFUSE_SECRET_KEY", "")
    base = os.environ.get("LANGFUSE_BASE_URL", "https://us.cloud.langfuse.com")
    missing = [n for n, v in [
        ("LANGFUSE_PUBLIC_KEY", pub),
        ("LANGFUSE_SECRET_KEY", sec),
    ] if not v]
    if missing:
        print(f"ERROR: missing environment variables: {', '.join(missing)}")
        print("Create a .env file from .env.example and fill in your Langfuse keys.")
        sys.exit(1)
    return pub, sec, base


def run_fetch_traces(hours: int, limit: int, out_path: str, debug: bool) -> bool:
    script = os.path.join(os.path.dirname(__file__), "fetch_traces.py")
    cmd = [sys.executable, script, "--hours", str(hours), "--limit", str(limit), "--out", out_path]
    print(f"Fetching traces (last {hours}h, max {limit})...")
    result = subprocess.run(cmd, capture_output=not debug)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
        print(f"ERROR: fetch_traces.py failed (exit {result.returncode})")
        if stderr:
            print(stderr)
        return False
    return True


def extract_case_marker(input_text: str) -> str | None:
    """Return zero-padded case number string (e.g. '03') or None."""
    if not input_text:
        return None
    # Match [case_XX], [case_X], [caseXX], or [caseX]
    m = re.search(r"\[case_?(\d+)\]", input_text, re.IGNORECASE)
    if m:
        return m.group(1).zfill(2)
    return None


def fetch_dataset_items(dataset_name: str, public_key: str, secret_key: str, base_url: str, debug: bool) -> list[dict]:
    try:
        from langfuse import Langfuse
    except ImportError:
        print("ERROR: langfuse package not installed. Run: pip install langfuse")
        sys.exit(1)

    lf = Langfuse(public_key=public_key, secret_key=secret_key, host=base_url)
    try:
        dataset = lf.get_dataset(name=dataset_name)
    except Exception as e:
        if debug:
            traceback.print_exc()
        print(f"ERROR: could not fetch dataset '{dataset_name}' from Langfuse: {e}")
        sys.exit(1)

    items = dataset.items
    if not items:
        print(f"WARNING: dataset '{dataset_name}' returned 0 items. Check the name in Langfuse.")
    return items


def build_case_index(dataset_items: list) -> dict[str, object]:
    """Map zero-padded case number string to dataset item."""
    index: dict[str, object] = {}
    for item in dataset_items:
        meta = {}
        raw_meta = getattr(item, "metadata", None) or {}
        if isinstance(raw_meta, str):
            try:
                meta = json.loads(raw_meta)
            except json.JSONDecodeError:
                pass
        elif isinstance(raw_meta, dict):
            meta = raw_meta

        case_num = meta.get("case_number")
        if case_num is None:
            print(f"  WARNING: dataset item '{item.id}' has no case_number in metadata — skipping")
            continue
        key = str(case_num).zfill(2)
        index[key] = item
    return index


def read_traces_jsonl(path: str) -> list[dict]:
    traces = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                traces.append(json.loads(line))
    return traces


def write_traces_csv(rows: list[dict], path: str) -> None:
    """Write traces CSV in the format scorer_with_traces_v3.py expects."""
    fieldnames = ["traceId", "sessionId", "output", "startTime", "datasetItemId", "tool_calls"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({
                "traceId":       row.get("traceId", ""),
                "sessionId":     row.get("sessionId", ""),
                "output":        row.get("output", ""),
                "startTime":     row.get("timestamp", ""),
                "datasetItemId": row.get("datasetItemId", ""),
                "tool_calls":    json.dumps(row.get("tool_calls", [])),
            })


def write_dataset_csv(items: list, path: str) -> None:
    """Write dataset CSV in the format scorer_with_traces_v3.py expects."""
    fieldnames = ["id", "input", "expectedOutput", "metadata"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for item in items:
            raw_meta = getattr(item, "metadata", None) or {}
            if isinstance(raw_meta, dict):
                meta_str = json.dumps(raw_meta)
            else:
                meta_str = str(raw_meta)

            inp = getattr(item, "input", None) or ""
            if isinstance(inp, dict):
                inp = json.dumps(inp)

            exp = getattr(item, "expected_output", None) or ""
            if isinstance(exp, dict):
                exp = json.dumps(exp)

            w.writerow({
                "id":             item.id,
                "input":          str(inp),
                "expectedOutput": str(exp),
                "metadata":       meta_str,
            })


def run_scorer(traces_csv: str, dataset_csv: str, run_name: str, debug: bool) -> list[dict]:
    script = os.path.join(os.path.dirname(__file__), "scorer_with_traces_v3.py")
    cmd = [sys.executable, script, "--traces", traces_csv, "--dataset", dataset_csv, "--run-name", run_name]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")
        print(f"ERROR: scorer_with_traces_v3.py failed (exit {result.returncode})")
        if stderr:
            print(stderr)
        if debug:
            print(result.stdout.decode("utf-8", errors="replace"))
        sys.exit(1)

    results = []
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if line:
            try:
                results.append(json.loads(line))
            except json.JSONDecodeError:
                if debug:
                    print(f"  DEBUG scorer line (not JSON): {line}")
    return results


def write_to_langfuse(
    scored: list[dict],
    case_index: dict[str, object],
    run_name: str,
    public_key: str,
    secret_key: str,
    base_url: str,
    debug: bool,
) -> str:
    try:
        from langfuse import Langfuse
    except ImportError:
        print("ERROR: langfuse package not installed.")
        sys.exit(1)

    lf = Langfuse(public_key=public_key, secret_key=secret_key, host=base_url)

    for result in scored:
        case_id  = result.get("case_id", "")
        trace_id = result.get("trace_id", "")
        passed   = result.get("passed", False)
        checks   = result.get("checks", [])

        # Derive case number from case_id (e.g. "case_03" → "03")
        m = re.match(r"case_(\d+)", case_id)
        if not m:
            continue
        case_num = m.group(1).zfill(2)
        item = case_index.get(case_num)
        if not item:
            continue

        try:
            item.link(
                trace_id=trace_id,
                run_name=run_name,
                run_metadata={"passed": passed, "checks": checks},
            )
        except Exception as e:
            if debug:
                traceback.print_exc()
            print(f"  WARNING: could not write result for {case_id} to Langfuse: {e}")

    lf.flush()
    return f"{base_url.rstrip('/')}/datasets"


def main():
    parser = argparse.ArgumentParser(
        description="Run FreightPOP Copilot eval end-to-end."
    )
    parser.add_argument("--run-name",     required=True, help="Label for this eval run in Langfuse")
    parser.add_argument("--dataset-name", required=True, help="Langfuse dataset name")
    parser.add_argument("--hours",        required=True, type=int, help="How many hours back to fetch traces")
    parser.add_argument("--limit",        type=int, default=100, help="Max traces to fetch (default 100)")
    parser.add_argument("--dry-run",      action="store_true", help="Score and print but do NOT write to Langfuse")
    parser.add_argument("--debug",        action="store_true", help="Show full tracebacks on error")
    args = parser.parse_args()

    try:
        _run(args)
    except SystemExit:
        raise
    except Exception as e:
        if args.debug:
            traceback.print_exc()
        else:
            print(f"ERROR: {e}")
            print("Re-run with --debug to see the full error.")
        sys.exit(1)


def _run(args) -> None:
    load_env(args.debug)
    public_key, secret_key, base_url = require_env()

    # ── Step 1: Fetch traces ──────────────────────────────────────────────
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tf:
        traces_jsonl = tf.name

    ok = run_fetch_traces(args.hours, args.limit, traces_jsonl, args.debug)
    if not ok:
        print("ERROR: fetch_traces.py failed or returned no traces. Aborting.")
        sys.exit(1)

    traces = read_traces_jsonl(traces_jsonl)
    os.unlink(traces_jsonl)

    if not traces:
        print("ERROR: No traces found in the fetch output.")
        print(f"  Check that --hours {args.hours} is wide enough and that you ran the UI cases.")
        sys.exit(1)

    # ── Step 2: Fetch dataset ─────────────────────────────────────────────
    print(f"Fetching dataset '{args.dataset_name}' from Langfuse...")
    dataset_items = fetch_dataset_items(args.dataset_name, public_key, secret_key, base_url, args.debug)
    case_index    = build_case_index(dataset_items)

    # ── Step 3: Match traces to dataset cases ─────────────────────────────
    matched_traces: list[dict] = []
    skipped_no_marker   = 0
    skipped_no_match    = 0
    marker_collisions: dict[str, list[str]] = {}  # case_num → list of trace IDs

    for trace in traces:
        case_num = extract_case_marker(trace.get("input", ""))
        if case_num is None:
            skipped_no_marker += 1
            continue

        if case_num not in case_index:
            # Try without leading zero
            alt = case_num.lstrip("0") or "0"
            if alt in case_index:
                case_num = alt
            else:
                print(f"  WARNING: marker [case_{case_num}] found in trace {trace['traceId'][:16]}... but no matching dataset item — skipping")
                skipped_no_match += 1
                continue

        item = case_index[case_num]
        marker_collisions.setdefault(case_num, []).append(trace["traceId"])
        enriched = dict(trace)
        enriched["datasetItemId"] = item.id
        enriched["_case_num"]     = case_num
        matched_traces.append(enriched)

    # Collision handling: keep only most recent trace per case
    deduped_traces: list[dict] = []
    seen_cases: dict[str, dict] = {}
    for t in matched_traces:
        cn = t["_case_num"]
        existing = seen_cases.get(cn)
        if existing is None:
            seen_cases[cn] = t
        else:
            # Prefer trace with tool calls; break ties by newest timestamp
            has_tools_new = bool(t.get("tool_calls"))
            has_tools_old = bool(existing.get("tool_calls"))
            ts_new = t.get("timestamp", "") or ""
            ts_old = existing.get("timestamp", "") or ""
            prefer_new = (has_tools_new and not has_tools_old) or (has_tools_new == has_tools_old and ts_new > ts_old)
            if prefer_new:
                print(f"  WARNING: duplicate [case_{cn}] — discarding trace {existing['traceId'][:16]}..., keeping {t['traceId'][:16]}...")
                seen_cases[cn] = t
            else:
                print(f"  WARNING: duplicate [case_{cn}] — discarding trace {t['traceId'][:16]}..., keeping {existing['traceId'][:16]}...")

    deduped_traces = list(seen_cases.values())

    # Find not-run cases
    run_case_nums = {t["_case_num"] for t in deduped_traces}
    not_run = [f"case_{cn}" for cn in sorted(case_index.keys()) if cn not in run_case_nums]

    # ── Step 4: Write temp CSVs ───────────────────────────────────────────
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w") as tf:
        traces_csv_path = tf.name
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w") as tf:
        dataset_csv_path = tf.name

    # Only write matched dataset items
    matched_item_ids = {t["datasetItemId"] for t in deduped_traces}
    matched_items = [item for item in dataset_items if item.id in matched_item_ids]

    write_traces_csv(deduped_traces, traces_csv_path)
    write_dataset_csv(matched_items, dataset_csv_path)

    # ── Step 5: Score ─────────────────────────────────────────────────────
    print("Scoring...")
    scored = run_scorer(traces_csv_path, dataset_csv_path, args.run_name, args.debug)

    os.unlink(traces_csv_path)
    os.unlink(dataset_csv_path)

    # ── Step 6: Tally results ─────────────────────────────────────────────
    passed_cases  = [r for r in scored if r.get("passed")]
    failed_cases  = [r for r in scored if not r.get("passed")]
    total_matched = len(scored)

    pass_rate = (len(passed_cases) / total_matched * 100) if total_matched else 0.0

    # ── Step 7: Print summary ─────────────────────────────────────────────
    print()
    print(DIVIDER)
    print(f"Run: {args.run_name}")
    print(f"Dataset: {args.dataset_name} ({len(dataset_items)} cases)")
    print(DIVIDER)
    print(f"Traces fetched: {len(traces)}")
    print(f"Traces matched to cases: {total_matched}")
    skipped_total = skipped_no_marker + skipped_no_match
    print(f"Traces skipped (no marker): {skipped_no_marker}")
    if skipped_no_match:
        print(f"Traces skipped (marker not matched): {skipped_no_match}")
    print()
    print("Scoring results:")
    print(f"  PASS: {len(passed_cases)}")
    print(f"  FAIL: {len(failed_cases)}")
    print(f"  Pass rate: {pass_rate:.1f}% ({len(passed_cases)}/{total_matched})")
    if not_run:
        not_run_str = ", ".join(not_run)
        print(f"  Not run: {len(not_run)} case{'s' if len(not_run) != 1 else ''} ({not_run_str})")

    if failed_cases:
        print()
        print("Failed cases:")
        for r in failed_cases:
            print(f"  - {r['case_id']}: {r.get('reason', 'unknown')}")

    # ── Step 8: Write to Langfuse ─────────────────────────────────────────
    if args.dry_run:
        print()
        print("Dry run — results NOT written to Langfuse.")
    else:
        print()
        print(f"Writing results to Langfuse run '{args.run_name}'...")
        view_url = write_to_langfuse(
            scored, case_index, args.run_name,
            public_key, secret_key, base_url, args.debug,
        )
        print(f"Written to Langfuse run: {args.run_name}")
        print(f"View at: {view_url}")

    print(DIVIDER)


if __name__ == "__main__":
    main()
