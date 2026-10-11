"""Check a PR's branch name, title and commit messages against the repo's conventions.

Usage: python .github/scripts/check_conventions.py [--branch NAME] [--title TITLE] [--commits RANGE]

Locally, with no arguments, it checks the current branch and the commits in
origin/main..HEAD; pass --title to check a PR title too. Branches follow
Conventional Branch 1.1.0; titles and commit subjects follow Conventional Commits
1.0.0 with a required scope, e.g. feat(db): add audit_entries table. Merge commits
are skipped.

Exits 0 on success and 1 on failure.
"""

import argparse
import re
import subprocess
import sys

BRANCH_TYPES = "feature|feat|bugfix|fix|hotfix|release|chore|ai|copilot|cursor|claude|codex"
SEGMENT = r"[a-z0-9]+(?:\.[a-z0-9]+)*"
BRANCH = re.compile(rf"(?:main|master|develop|(?:{BRANCH_TYPES})/{SEGMENT}(?:-{SEGMENT})*)")

COMMIT_TYPES = "feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert"
# type(scope): description, with a required lowercase scope and an optional ! for breaking changes.
SUBJECT = re.compile(rf"(?:{COMMIT_TYPES})\([a-z][a-z0-9-]*\)!?: \S.*")


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        sys.exit(f"check_conventions: git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--branch")
    parser.add_argument("--title")
    parser.add_argument("--commits", default="origin/main..HEAD")
    args = parser.parse_args()
    branch = args.branch or git("branch", "--show-current").strip()
    problems = []

    if not BRANCH.fullmatch(branch):
        problems.append(f"branch {branch!r} isn't Conventional Branch, e.g. feature/add-incidents")
    if args.title is not None and not SUBJECT.fullmatch(args.title):
        problems.append(f"PR title {args.title!r} isn't type(scope): description")
    for line in git("log", "--no-merges", "--format=%h %s", args.commits).splitlines():
        sha, _, subject = line.partition(" ")
        if not SUBJECT.fullmatch(subject):
            problems.append(f"commit {sha} {subject!r} isn't type(scope): description")

    if problems:
        print("check_conventions: fix these (see Branches, commits and pull requests in AGENTS.md):")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print(f"check_conventions: ok; branch {branch}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
