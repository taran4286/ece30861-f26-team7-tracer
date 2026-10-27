# .github/: CI and repo settings

| File | Does |
| --- | --- |
| `CODEOWNERS` | Makes all four team members owners of everything; the PR stats issue lists these owners |
| `workflows/gitleaks.yml` | Scans the full git history for secrets on every PR and push to `main`; must stay at 0 findings |
| `workflows/pr-stats.yml` and `scripts/pr-stats.js` | Rebuild the pinned "PR stats" issue when a PR opens, closes or reopens, or on a manual run. The issue is the open one whose body starts with `<!-- pr-stats -->`; if none is open, the script creates one and pins it, only warning if the pin fails. It counts PRs by the `@user` owners in `CODEOWNERS` (logins matched case-insensitively, team owners skipped) and overwrites any edit to the issue |
| `workflows/agents-md.yml` and `scripts/check_agents_md.py` | Fail a PR unless the nearest `AGENTS.md` of every changed file changed too, or the PR description has a skip line for it, and fail it when a folder that needs its own guide has none; re-run when the description is edited |
| `workflows/skills.yml` | Fails a PR when `.claude/skills/` and `.agents/skills/` differ |

## Rules

- `pr-stats.yml` uses `pull_request_target`, so it runs the base branch's workflow with a token that can write issues. Never check out, install or run a PR's code in it; keep the sparse checkout of `.github` from the default branch and `persist-credentials: false`.
- Other workflows run on `pull_request` (`gitleaks.yml` also on push to `main`); `agents-md.yml` and `skills.yml` set `permissions: contents: read`.
- Pass `${{ }}` values into `run:` steps through `env:`, never inline, so a branch or PR name can't inject shell commands.
- Reference actions by major version tag (e.g. `actions/checkout@v4`) and tools by exact version (`GITLEAKS_VERSION`).
- Update `CODEOWNERS` when the team changes; the PR stats script reads its `@user` entries.
- Keep `check_agents_md.py` in step with the root "Keeping the guides current" rules. The folders that need no guide of their own are in its `UNGUIDED_TOP_LEVEL`.
- `check_agents_md.py [BASE] --body "<PR description>"`: BASE defaults to `origin/main`. It compares committed changes (`BASE...HEAD`) only, so commit and fetch first, or uncommitted edits and a stale `origin/main` give wrong results. Changed `AGENTS.md` files need no guide of their own.
- A skip line must start at the beginning of a line, bare or as a `- ` list item, with the guide's path optionally in backticks: `` `app/AGENTS.md`: no change needed because <reason> ``. An indented line or a `*` bullet doesn't count.
- Keep `check_agents_md.py` standard-library only, runnable as `python` locally and as the runner's `python3` in CI, so its workflow needs no setup step.
- Don't add `paths:` filters to required checks: a required check that never runs leaves the PR waiting forever.
- The `AGENTS.md updated` and `Skills in sync` checks block merges only once a branch protection rule on `main` marks them as required (repository settings).
