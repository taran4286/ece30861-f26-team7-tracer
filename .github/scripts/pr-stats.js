// Rebuilds the pinned "PR stats" issue with the pull requests each codeowner
// has raised and merged. Run by .github/workflows/pr-stats.yml through
// actions/github-script.

const fs = require('fs');

const MARKER = '<!-- pr-stats -->';
const TITLE = 'PR stats';
const CODEOWNERS = '.github/CODEOWNERS';

// Every @user named in CODEOWNERS, in file order. Teams (@org/team) and email
// owners are skipped since they aren't PR authors.
function parseCodeowners(text) {
  const owners = new Map();
  for (const line of text.split('\n')) {
    const tokens = line.replace(/#.*/, '').trim().split(/\s+/).slice(1);
    for (const token of tokens) {
      if (!/^@[^/]+$/.test(token)) continue;
      const login = token.slice(1);
      if (!owners.has(login.toLowerCase())) owners.set(login.toLowerCase(), login);
    }
  }
  return [...owners.values()];
}

// One entry per codeowner, including those with no pull requests yet. Logins
// match case-insensitively; PRs by anyone else aren't counted.
function tally(pulls, owners) {
  const users = new Map(owners.map((login) => [login.toLowerCase(), { login, raised: 0, merged: 0 }]));
  for (const pr of pulls) {
    const user = pr.user && users.get(pr.user.login.toLowerCase());
    if (!user) continue;
    user.raised += 1;
    if (pr.merged_at) user.merged += 1;
  }
  return users;
}

function render(users) {
  const rows = [...users.values()]
    .sort((a, b) => b.raised - a.raised || b.merged - a.merged || a.login.localeCompare(b.login))
    .map((u) => `| ${u.login} | ${u.raised} | ${u.merged} |`);
  return [
    MARKER,
    'Pull requests raised and merged per codeowner, rebuilt by the `PR stats` workflow. Edits to this issue are overwritten.',
    '',
    '| Codeowner | PRs raised | PRs merged |',
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
  const owners = parseCodeowners(fs.readFileSync(CODEOWNERS, 'utf8'));
  if (owners.length === 0) throw new Error(`No @user owners found in ${CODEOWNERS}.`);
  const pulls = await github.paginate(github.rest.pulls.list, { owner, repo, state: 'all', per_page: 100 });
  const body = render(tally(pulls, owners));
  const issue = await findOrCreateIssue({ github, context, core });
  if (issue.body !== body) {
    await github.rest.issues.update({ owner, repo, issue_number: issue.number, body });
  }
  core.info(`Updated issue #${issue.number} for ${owners.length} codeowners from ${pulls.length} pull requests.`);
};

module.exports.parseCodeowners = parseCodeowners;
module.exports.tally = tally;
module.exports.render = render;
