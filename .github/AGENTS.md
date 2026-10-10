# .github/: CI and repo settings

| File | Does |
| --- | --- |
| `CODEOWNERS` | Makes all four team members owners of everything; the PR stats issue lists these owners |
| `workflows/gitleaks.yml` | Scans the full git history for secrets on every PR and push to `main`; must stay at 0 findings |
| `workflows/pr-stats.yml` and `scripts/pr-stats.js` | Rebuild the pinned "PR stats" issue when a PR opens, closes or reopens |
| `workflows/agents-md.yml` and `scripts/check_agents_md.py` | Fail a PR unless the nearest `AGENTS.md` of every changed file changed too |

## Rules

- `pr-stats.yml` uses `pull_request_target`, so it runs `main`'s code with a token that can write issues. Never check out, install or run a PR's code in it; keep the sparse checkout of `.github` from the default branch and `persist-credentials: false`.
- Other workflows use `pull_request` with `contents: read`.
- Pass `${{ }}` values into `run:` steps through `env:`, never inline, so a branch or PR name can't inject shell commands.
- Pin action versions (`@v4`) and tool versions (`GITLEAKS_VERSION`).
- Update `CODEOWNERS` when the team changes; the PR stats script reads its `@user` entries.
- Keep `check_agents_md.py` in step with the root "Keeping the guides current" rules. The folders that need no guide of their own are in its `UNGUIDED_TOP_LEVEL`.
- The `AGENTS.md updated` check blocks merges only once a branch protection rule on `main` marks it as required (repository settings).
