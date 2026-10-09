"""Report an agent session's token usage for a pull request to the "PR stats" issue.

Posts one comment on the issue; the PR stats workflow rebuilds the table from
these comments. Reporting again from the same session replaces that session's
earlier report, so run it after opening a PR and again after later pushes.

Claude Code: run it from inside the session. The model, effort level and
tokens are read from the session's own log, found through
CLAUDE_CODE_SESSION_ID (or pass --log).

    python scripts/report_usage.py [--pr 12] [--dry-run]

Other tools: pass the values from the tool's own usage report.

    python scripts/report_usage.py --model "GPT 6 Astra" --effort medium --tokens 48213 --session <id>

Tokens = input + output + cache writes, leaving out cache reads. In OpenAI
terms that's prompt_tokens - cached_tokens + completion_tokens.
"""

import argparse
import json
import os
import subprocess
import sys
import uuid
from collections import defaultdict
from pathlib import Path

MARKER = "<!-- pr-stats -->"
RECORD = "pr-usage"


def gh(*args, stdin=None):
    result = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        sys.exit(f"gh {' '.join(args[:2])} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def find_claude_log(session_id):
    matches = list((Path.home() / ".claude" / "projects").glob(f"*/{session_id}.jsonl"))
    if not matches:
        sys.exit(f"No Claude Code log found for session {session_id}; pass --log.")
    return matches[0]


def read_claude_log(path):
    """Sum tokens per (model, effort), counting each API response once."""
    replies = {}
    session = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = entry.get("message") or {}
            usage = message.get("usage")
            if entry.get("type") != "assistant" or not usage or not message.get("model"):
                continue
            session = session or entry.get("sessionId")
            # One response is logged as several entries that repeat the same usage.
            replies[message.get("id") or entry.get("uuid")] = (message["model"], entry.get("effort"), usage)

    totals = defaultdict(int)
    for model, effort, usage in replies.values():
        totals[(model, effort)] += (
            usage.get("input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
            + usage.get("output_tokens", 0)
        )
    usage = [{"model": m, "effort": e, "tokens": t} for (m, e), t in totals.items() if t > 0]
    if not usage:
        sys.exit(f"No model usage found in {path}.")
    return session or path.stem, usage


def find_stats_issue():
    numbers = gh(
        "api", "--paginate", "repos/{owner}/{repo}/issues?state=open&per_page=100",
        "--jq", f'.[] | select(.pull_request == null) | select((.body // "") | startswith("{MARKER}")) | .number',
    ).split()
    if not numbers:
        sys.exit('No open "PR stats" issue; run the PR stats workflow once from the Actions tab to create it.')
    return numbers[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pr", type=int, help="pull request number (default: the current branch's)")
    parser.add_argument("--log", type=Path, help="Claude Code session log (default: from CLAUDE_CODE_SESSION_ID)")
    parser.add_argument("--model", help="model name, for tools other than Claude Code")
    parser.add_argument("--effort", help="effort level, with --model")
    parser.add_argument("--tokens", type=int, help="tokens used, with --model")
    parser.add_argument("--session", help="session ID, with --model; reuse it to replace an earlier report")
    parser.add_argument("--dry-run", action="store_true", help="print the comment instead of posting it")
    args = parser.parse_args()

    if args.model:
        if args.tokens is None or args.tokens < 0 or not args.effort:
            parser.error("--model needs --effort and a non-negative --tokens")
        source = "manual"
        session = args.session or str(uuid.uuid4())
        if not args.session:
            print(f"No --session given; using {session}. Pass it next time to replace this report.", file=sys.stderr)
        usage = [{"model": args.model, "effort": args.effort, "tokens": args.tokens}]
    else:
        session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
        if not args.log and not session_id:
            parser.error("not in a Claude Code session; pass --log, or --model, --effort and --tokens")
        source = "claude-code"
        session, usage = read_claude_log(args.log or find_claude_log(session_id))

    pr = args.pr or int(gh("pr", "view", "--json", "number", "--jq", ".number"))
    record = {"pr": pr, "session": session, "source": source, "usage": usage}
    lines = ", ".join(f"{u['model']} at {u['effort'] or 'unknown'} effort, {u['tokens']:,} tokens" for u in usage)
    body = f"<!-- {RECORD} {json.dumps(record)} -->\nUsage for #{pr} from session `{session[:8]}`: {lines}."

    if args.dry_run:
        print(body)
        return
    issue = find_stats_issue()
    print(gh("issue", "comment", issue, "--body-file", "-", stdin=body))


if __name__ == "__main__":
    main()
