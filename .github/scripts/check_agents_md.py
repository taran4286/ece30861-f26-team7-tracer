"""Fail unless every changed file's nearest AGENTS.md changed in the same PR.

Usage: python .github/scripts/check_agents_md.py [BASE]

BASE defaults to origin/main. Compares committed changes (BASE...HEAD), so commit
before running it locally. A file's nearest AGENTS.md is the first one found in its
folder or a parent folder; files outside a guided folder map to the root AGENTS.md.
Whitespace-only edits to an AGENTS.md don't count.

Also fails if a guided folder has no AGENTS.md: every top-level folder except the
skill folders, and every Python package under app/.

Exits 0 on success and 1 on failure.
"""

import subprocess
import sys
from collections import defaultdict
from pathlib import PurePosixPath

GUIDE = "AGENTS.md"
# The skill folders are covered by the root AGENTS.md.
UNGUIDED_TOP_LEVEL = {".agents", ".claude"}


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8")


def git_paths(*args: str) -> list[PurePosixPath]:
    """Run a git command that prints NUL-separated paths (-z) and return them."""
    result = git(*args)
    if result.returncode != 0:
        sys.exit(f"check_agents_md: git {' '.join(args)} failed: {result.stderr.strip()}")
    return [PurePosixPath(p) for p in result.stdout.split("\0") if p]


def guide_changed(base: str, guide: PurePosixPath) -> bool:
    result = git("diff", "--quiet", "-w", "--ignore-blank-lines", f"{base}...HEAD", "--", str(guide))
    if result.returncode not in (0, 1):
        sys.exit(f"check_agents_md: git diff failed for {guide}: {result.stderr.strip()}")
    return result.returncode == 1


def guided_folders(tracked: list[PurePosixPath]) -> set[PurePosixPath]:
    folders = {PurePosixPath(p.parts[0]) for p in tracked if len(p.parts) > 1}
    folders -= {PurePosixPath(name) for name in UNGUIDED_TOP_LEVEL}
    folders |= {p.parent for p in tracked if p.name == "__init__.py" and p.parts[0] == "app"}
    return folders


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
    tracked = git_paths("ls-tree", "-r", "-z", "--name-only", "HEAD")
    changed = git_paths("diff", "--name-only", "-z", "--no-renames", f"{base}...HEAD")
    guide_dirs = {p.parent for p in tracked if p.name == GUIDE}
    failed = False

    missing_guides = sorted(guided_folders(tracked) - guide_dirs)
    if missing_guides:
        failed = True
        print("check_agents_md: these folders need their own AGENTS.md:")
        for folder in missing_guides:
            print(f"  {folder / GUIDE}")

    needed: dict[PurePosixPath, list[PurePosixPath]] = defaultdict(list)
    for path in changed:
        if path.name == GUIDE:
            continue
        guide = next((d / GUIDE for d in path.parents if d in guide_dirs), None)
        if guide is None:
            print(f"check_agents_md: no AGENTS.md covers {path}; the root one is missing")
            return 1
        needed[guide].append(path)

    stale = {g: files for g, files in needed.items() if not guide_changed(base, g)}
    if stale:
        failed = True
        print("check_agents_md: update these AGENTS.md files in this PR:")
        for guide, files in sorted(stale.items()):
            print(f"  {guide} (for {', '.join(map(str, files))})")

    if failed:
        return 1
    updated = ", ".join(str(g) for g in sorted(needed)) or "none needed"
    print(f"check_agents_md: ok; {len(changed)} changed files; guides updated: {updated}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
