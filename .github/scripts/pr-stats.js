// Rebuilds the pinned "PR stats" issue from every pull request's description.
// Run by .github/workflows/pr-stats.yml through actions/github-script.
//
// A description reports its usage with a line like:
//   Model: Claude Opus 5.5 · Effort: high · Tokens: 48213
// Every PR counts toward its author's total; tokens count only when one line
// names both a model and a token count (effort is optional). The last such line
// wins, so an example earlier in the description isn't counted.

const MARKER = '<!-- pr-stats -->';
const TITLE = 'PR stats';
const USAGE_LINE =
  /^[^\S\n]*Model:[^\S\n]*([^·|\n]*[^\s·|])[^\S\n]*[·|][^\S\n]*(?:Effort:[^\S\n]*([^·|\n]*[^\s·|])[^\S\n]*[·|][^\S\n]*)?Tokens:[^\S\n]*(\d[\d,]*)[^\S\n]*$/gim;

function parseUsage(body) {
  const match = [...(body || '').matchAll(USAGE_LINE)].pop();
  if (!match) return null;
  const clean = (x) => x.replace(/\s+/g, ' ');
  return {
    model: clean(match[1]),
    effort: match[2] ? clean(match[2]) : null,
    tokens: Number(match[3].replace(/,/g, '')),
  };
}

function tally(pulls) {
  const users = new Map();
  for (const pr of pulls) {
    if (!pr.user || pr.user.type === 'Bot') continue;
    const login = pr.user.login;
    if (!users.has(login)) users.set(login, { prs: 0, tokens: 0, models: new Map() });
    const user = users.get(login);
    user.prs += 1;
    const usage = parseUsage(pr.body);
    if (!usage) continue;
    // Group "claude opus 5.5" with "Claude Opus 5.5", keeping the first spelling seen.
    const key = `${usage.model}\n${usage.effort || ''}`.toLowerCase();
    if (!user.models.has(key)) user.models.set(key, { name: usage.model, effort: usage.effort, prs: 0, tokens: 0 });
    const model = user.models.get(key);
    model.prs += 1;
    model.tokens += usage.tokens;
    user.tokens += usage.tokens;
  }
  return users;
}

function render(users) {
  const n = (x) => x.toLocaleString('en-US');
  const byLogin = [...users]
    .map(([login, user]) => ({ login, ...user }))
    .sort((a, b) => b.prs - a.prs || b.tokens - a.tokens || a.login.localeCompare(b.login));
  const byModel = byLogin.flatMap((u) =>
    [...u.models.values()]
      .sort((a, b) => b.tokens - a.tokens || a.name.localeCompare(b.name))
      .map((m) => `| ${u.login} | ${m.name} | ${m.effort || '—'} | ${m.prs} | ${n(m.tokens)} |`),
  );
  return [
    MARKER,
    'Pull requests and tokens per author, rebuilt by the `PR stats` workflow whenever a pull request is opened or edited. Edits to this issue are overwritten.',
    '',
    '| Author | PRs | Tokens |',
    '| --- | ---: | ---: |',
    ...byLogin.map((u) => `| ${u.login} | ${u.prs} | ${n(u.tokens)} |`),
    '',
    '**By model and effort**',
    '',
    '| Author | Model | Effort | PRs | Tokens |',
    '| --- | --- | --- | ---: | ---: |',
    ...byModel,
    '',
    'Tokens come from a `Model: Claude Opus 5.5 · Effort: high · Tokens: 48213` line in the pull request description. A pull request without it still counts, with no tokens.',
  ].join('\n');
}

async function findOrCreateIssue({ github, context, core }) {
  const { owner, repo } = context.repo;
  const issues = await github.paginate(github.rest.issues.listForRepo, { owner, repo, state: 'open', per_page: 100 });
  const existing = issues.find((i) => !i.pull_request && (i.body || '').startsWith(MARKER));
  if (existing) return existing;

  const { data: created } = await github.rest.issues.create({ owner, repo, title: TITLE, body: MARKER });
  try {
    await github.graphql('mutation($id: ID!) { pinIssue(input: { issueId: $id }) { issue { id } } }', { id: created.node_id });
  } catch (err) {
    core.warning(`Created issue #${created.number} but could not pin it; pin it by hand. ${err.message}`);
  }
  return created;
}

module.exports = async ({ github, context, core }) => {
  const { owner, repo } = context.repo;
  const pulls = await github.paginate(github.rest.pulls.list, { owner, repo, state: 'all', per_page: 100 });
  const body = render(tally(pulls));
  const issue = await findOrCreateIssue({ github, context, core });
  if (issue.body !== body) {
    await github.rest.issues.update({ owner, repo, issue_number: issue.number, body });
  }
  core.info(`Updated issue #${issue.number} from ${pulls.length} pull requests.`);
};

module.exports.parseUsage = parseUsage;
module.exports.tally = tally;
module.exports.render = render;
