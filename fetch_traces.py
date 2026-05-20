"""
FreightPOP Copilot Eval - Langfuse Trace Fetcher

Pulls traces from Langfuse Cloud API including their child observations (spans + generations),
extracts tool span names, and outputs JSONL ready for the scorer.

Usage:
    python3 fetch_traces.py \
        --public-key  pk-lf-... \
        --secret-key  sk-lf-... \
        --limit       5 \
        --out         traces.jsonl

Output (one JSON object per line):
    {
      "traceId":   "0ebf02ed-...",
      "sessionId": "69fa56cb...",
      "userId":    "...",
      "timestamp": "2026-05-05T13:45:14.188Z",
      "input":     "Add a new package",
      "output":    "Added - there are now 2 packages...",
      "tool_calls":["getPackagesState", "shipmentAddPackage"],
      "metadata":  {"threadId": "...", "companyId": "17795", ...}
    }

Side effect:
    Prints a "tool name inventory" at the end - frequency count of tool span
    names seen across all fetched traces. Use this to align dataset tool names.
"""

import argparse
import json
import subprocess
import sys
import urllib.parse
from collections import Counter

LANGFUSE_BASE_DEFAULT = "https://us.cloud.langfuse.com"
LANGFUSE_RESOLVE = "us.cloud.langfuse.com:443:54.203.223.227"


def make_auth_header(public_key: str, secret_key: str) -> str:
    return f"{public_key}:{secret_key}"


def http_get(url: str, auth_header: str) -> dict:
    result = subprocess.run(
        ["curl", "-s", "--max-time", "30", "--resolve", LANGFUSE_RESOLVE,
         "-u", auth_header, "-H", "Accept: application/json", url],
        capture_output=True
    )
    if result.returncode != 0:
        print(f"curl error (code {result.returncode}) on {url}: {result.stderr.decode('utf-8', errors='replace')}", file=sys.stderr)
        raise RuntimeError(f"curl failed with code {result.returncode}")
    try:
        data = json.loads(result.stdout.decode("utf-8"))
    except json.JSONDecodeError:
        print(f"JSON 解析失敗 {url}: {result.stdout[:500]}", file=sys.stderr)
        raise
    if isinstance(data, dict) and "message" in data and len(data) == 1:
        print(f"API 錯誤 {url}: {data['message']}", file=sys.stderr)
        raise RuntimeError(data["message"])
    return data


def fetch_traces_list(base: str, auth_header: str, limit: int, from_iso: str | None = None) -> list[dict]:
    """
    GET /api/public/traces - returns trace summaries (no observations).
    Docs: https://api.reference.langfuse.com/#tag/trace
    """
    params = {"limit": limit, "page": 1}
    if from_iso:
        params["fromTimestamp"] = from_iso
    qs = urllib.parse.urlencode(params)
    url = f"{base}/api/public/traces?{qs}"
    print(f"GET {url}", file=sys.stderr)
    data = http_get(url, auth_header)
    return data.get("data", [])


def fetch_trace_detail(base: str, auth_header: str, trace_id: str) -> dict:
    """
    GET /api/public/traces/{traceId} - returns trace + all child observations.
    """
    url = f"{base}/api/public/traces/{trace_id}"
    return http_get(url, auth_header)


def extract_tool_calls(observations: list[dict]) -> list[str]:
    """
    Tool spans are named 'tool: <camelCaseName>'. We strip the prefix
    and return the bare tool name in call order.
    Generation spans (no prefix) are excluded.
    """
    PREFIX = "tool: "
    tools = []
    sorted_obs = sorted(observations, key=lambda o: o.get("startTime") or "")
    for obs in sorted_obs:
        if obs.get("type") != "SPAN":
            continue
        name = obs.get("name", "") or ""
        if name.startswith(PREFIX):
            tools.append(name[len(PREFIX):].strip())
    return tools


def is_system_trace(trace: dict) -> bool:
    """Filter out Langfuse auto-generated title traces."""
    inp = trace.get("input", "") or ""
    if isinstance(inp, str) and "system prompt" in inp.lower():
        return True
    out = trace.get("output", "") or ""
    if isinstance(out, str) and out.strip().startswith('{"content":'):
        return True
    return False


def extract_final_output(trace: dict) -> str:
    """The trace's top-level output is what the user sees."""
    out = trace.get("output", "")
    if isinstance(out, dict):
        return json.dumps(out, ensure_ascii=False)
    return str(out or "")


def extract_input(trace: dict) -> str:
    """The user's message."""
    inp = trace.get("input", "")
    if isinstance(inp, dict):
        return json.dumps(inp, ensure_ascii=False)
    return str(inp or "")


def load_env_file(path: str = ".env") -> dict:
    """Minimal .env loader - no dependencies. Skips comments/blank lines."""
    out = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


def main():
    parser = argparse.ArgumentParser(description="Fetch Langfuse traces with tool spans")
    parser.add_argument("--public-key", help="Langfuse public key (defaults to LANGFUSE_PUBLIC_KEY in .env)")
    parser.add_argument("--secret-key", help="Langfuse secret key (defaults to LANGFUSE_SECRET_KEY in .env)")
    parser.add_argument("--base-url",   help="Langfuse base URL (defaults to LANGFUSE_BASE_URL in .env, else us.cloud.langfuse.com)")
    parser.add_argument("--env",        default=".env", help=".env file path (default: .env in current dir)")
    parser.add_argument("--limit", type=int, default=5, help="How many traces to fetch (default 5)")
    parser.add_argument("--hours", type=int, default=None, help="Only fetch traces from the last N hours")
    parser.add_argument("--out",   default="traces.jsonl", help="Output JSONL path")
    args = parser.parse_args()

    env = load_env_file(args.env)
    public_key = args.public_key or env.get("LANGFUSE_PUBLIC_KEY")
    secret_key = args.secret_key or env.get("LANGFUSE_SECRET_KEY")
    base_url   = args.base_url   or env.get("LANGFUSE_BASE_URL") or LANGFUSE_BASE_DEFAULT

    if not public_key or not secret_key:
        print("ERROR: missing keys. Pass --public-key/--secret-key or put them in .env", file=sys.stderr)
        sys.exit(2)

    auth = make_auth_header(public_key, secret_key)

    print(f"Base URL: {base_url}", file=sys.stderr)
    from_iso = None
    if args.hours:
        from datetime import datetime, timedelta, timezone
        from_dt = datetime.now(timezone.utc) - timedelta(hours=args.hours)
        from_iso = from_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"Window: last {args.hours}h (since {from_iso})", file=sys.stderr)

    print(f"Fetching up to {args.limit} traces from Langfuse...", file=sys.stderr)
    summaries = fetch_traces_list(base_url, auth, args.limit, from_iso=from_iso)
    print(f"Got {len(summaries)} trace summaries", file=sys.stderr)

    if not summaries:
        print("No traces returned. Check API keys and project.", file=sys.stderr)
        sys.exit(1)

    tool_counter: Counter = Counter()
    written = 0

    with open(args.out, "w", encoding="utf-8") as f:
        for i, summary in enumerate(summaries, 1):
            trace_id = summary["id"]
            print(f"  [{i}/{len(summaries)}] {trace_id[:16]}...", file=sys.stderr)

            try:
                detail = fetch_trace_detail(base_url, auth, trace_id)
            except Exception as e:
                print(f"    skip: {e}", file=sys.stderr)
                continue

            if is_system_trace(detail):
                print(f"    skip (system trace)", file=sys.stderr)
                continue

            observations = detail.get("observations", [])
            tool_calls = extract_tool_calls(observations)
            tool_counter.update(tool_calls)

            record = {
                "traceId":   detail.get("id"),
                "sessionId": detail.get("sessionId"),
                "userId":    detail.get("userId"),
                "timestamp": detail.get("timestamp"),
                "input":     extract_input(detail),
                "output":    extract_final_output(detail),
                "tool_calls": tool_calls,
                "metadata":  detail.get("metadata", {}),
                "obs_count": len(observations),
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            written += 1

    print(f"\nWrote {written} traces to {args.out}", file=sys.stderr)

    # Tool name inventory
    print("\n" + "=" * 60, file=sys.stderr)
    print("TOOL NAME INVENTORY (sorted by frequency)", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    if tool_counter:
        for name, count in tool_counter.most_common():
            print(f"  {count:4d}  {name}", file=sys.stderr)
    else:
        print("  (no tool spans found in fetched traces)", file=sys.stderr)
        print("  -> either traces had no tool calls, or span name format differs from 'tool: <name>'", file=sys.stderr)
    print("=" * 60, file=sys.stderr)


if __name__ == "__main__":
    main()
