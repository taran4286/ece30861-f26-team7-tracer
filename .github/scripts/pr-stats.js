// Rebuilds the pinned "PR stats" issue from every pull request's description.
// Run by .github/workflows/pr-stats.yml through actions/github-script.
//
// A description reports its usage with a line like:
//   Model: claude · Tokens: 48213
// Every PR counts toward its author's total; tokens count only when the line
// names a known model.

const MARKER = '<!-- pr-stats -->';
const TITLE = 'PR stats';
const MODELS = ['claude', 'codex'];

function parseUsage(body) {
  const text = body || '';
  const model = text.match(/\bModel:\s*([a-z]+)/i);
  const tokens = text.match(/\bTokens:\s*(\d[\d,]*)/i);
  const name = model && model[1].toLowerCase();
  if (!MODELS.includes(name) || !tokens) return null;
  return { model: name, tokens: Number(tokens[1].replace(/,/g, '')) };
}

function tally(pulls) {
  const users = new Map();
  for (const pr of pulls) {
    if (!pr.user || pr.user.type === 'Bot') continue;
    const login = pr.user.login;
    if (!users.has(login)) {
      users.set(login, { prs: 0, tokens: Object.fromEntries(MODELS.map((m) => [m, 0])) });
    }
    const row = users.get(login);
    row.prs += 1;
    const usage = parseUsage(pr.body);
    if (usage) row.tokens[usage.model] += usage.tokens;
  }
  return users;
}

function render(users) {
  const n = (x) => x.toLocaleString('en-US');
  const label = (m) => m[0].toUpperCase() + m.slice(1);
  const rows = [...users]
    .map(([login, row]) => ({ login, ...row, total: MODELS.reduce((s, m) => s + row.tokens[m], 0) }))
    .sort((a, b) => b.prs - a.prs || b.total - a.total || a.login.localeCompare(b.login))
    .map((r) => `| ${r.login} | ${r.prs} | ${MODELS.map((m) => n(r.tokens[m])).join(' | ')} | ${n(r.total)} |`);
  return [
    MARKER,
    'Pull requests and tokens per author, rebuilt by the `PR stats` workflow whenever a pull request is opened or edited. Edits to this issue are overwritten.',
    '',
    `| Author | PRs | ${MODELS.map((m) => `${label(m)} tokens`).join(' | ')} | Total tokens |`,
    `| --- | ---: | ${MODELS.map(() => '---:').join(' | ')} | ---: |`,
    ...rows,
    '',
    'Tokens come from a `Model: claude · Tokens: 48213` line in the pull request description (`claude` or `codex`). A pull request without it still counts, with no tokens.',
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
