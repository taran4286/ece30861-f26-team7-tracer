// Rebuilds the pinned "PR stats" issue with the pull requests each author has
// raised and merged. Run by .github/workflows/pr-stats.yml through
// actions/github-script.

const MARKER = '<!-- pr-stats -->';
const TITLE = 'PR stats';

function tally(pulls) {
  const users = new Map();
  for (const pr of pulls) {
    if (!pr.user || pr.user.type === 'Bot') continue;
    const login = pr.user.login;
    if (!users.has(login)) users.set(login, { raised: 0, merged: 0 });
    const user = users.get(login);
    user.raised += 1;
    if (pr.merged_at) user.merged += 1;
  }
  return users;
}

function render(users) {
  const rows = [...users]
    .map(([login, u]) => ({ login, ...u }))
    .sort((a, b) => b.raised - a.raised || b.merged - a.merged || a.login.localeCompare(b.login))
    .map((u) => `| ${u.login} | ${u.raised} | ${u.merged} |`);
  return [
    MARKER,
    'Pull requests raised and merged per author, rebuilt by the `PR stats` workflow. Edits to this issue are overwritten.',
    '',
    '| Author | PRs raised | PRs merged |',
    '| --- | ---: | ---: |',
    ...rows,
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

module.exports.tally = tally;
module.exports.render = render;
