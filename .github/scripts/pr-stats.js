// Rebuilds the pinned "PR stats" issue. Run by .github/workflows/pr-stats.yml
// through actions/github-script.
//
// PR and merged counts come from GitHub's pull request list. Token usage comes
// from comments that scripts/report_usage.py posts on the issue:
//   <!-- pr-usage {"pr": 11, "session": "...", "usage": [{"model": "...", "effort": "high", "tokens": 48213}]} -->
// A report counts only if the PR's author posted it and nobody has edited or
// hidden it since. The latest report from a session replaces its earlier ones.

const MARKER = '<!-- pr-stats -->';
const TITLE = 'PR stats';
const RECORD = /^<!-- pr-usage (\{.*\}) -->/;

const COMMENTS_QUERY = `query($owner: String!, $repo: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $repo) {
    issue(number: $number) {
      comments(first: 100, after: $after) {
        nodes { author { login } body createdAt lastEditedAt isMinimized url }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}`;

// "claude-opus-5-5" -> "Claude Opus 5.5"; other names are shown as given.
function modelName(id) {
  const parts = id.match(/^claude-([a-z]+)-(\d+)-(\d+)(?:-\d{8})?$/i);
  if (!parts) return id;
  return `Claude ${parts[1][0].toUpperCase()}${parts[1].slice(1)} ${parts[2]}.${parts[3]}`;
}

function parseRecord(body) {
  const match = (body || '').match(RECORD);
  if (!match) return null;
  try {
    const record = JSON.parse(match[1]);
    const valid =
      Number.isInteger(record.pr) &&
      typeof record.session === 'string' &&
      Array.isArray(record.usage) &&
      record.usage.every(
        (u) => u && typeof u.model === 'string' && u.model.trim() && Number.isInteger(u.tokens) && u.tokens >= 0,
      );
    return valid ? record : 'malformed';
  } catch {
    return 'malformed';
  }
}

// Picks the reports that count, oldest first, and lists the rest with a reason.
function selectReports(comments, pullsByNumber) {
  const latest = new Map();
  const ignored = [];
  for (const c of [...comments].sort((a, b) => a.createdAt.localeCompare(b.createdAt))) {
    const record = parseRecord(c.body);
    if (!record) continue;
    const pr = record === 'malformed' ? null : pullsByNumber.get(record.pr);
    const author = c.author && c.author.login;
    let reason = null;
    if (record === 'malformed') reason = 'malformed report';
    else if (c.lastEditedAt) reason = 'edited after posting';
    else if (c.isMinimized) reason = 'hidden';
    else if (!pr) reason = `no pull request #${record.pr}`;
    else if (!author || author.toLowerCase() !== pr.user.login.toLowerCase()) reason = `not posted by the author of #${record.pr}`;
    if (reason) ignored.push({ url: c.url, reason });
    else latest.set(`${record.pr}\n${record.session}`, { ...record, login: pr.user.login });
  }
  return { reports: [...latest.values()], ignored };
}

function tally(pulls, reports) {
  const users = new Map();
  const user = (login) => {
    if (!users.has(login)) users.set(login, { prs: 0, merged: 0, tokens: 0, models: new Map() });
    return users.get(login);
  };
  for (const pr of pulls) {
    if (!pr.user || pr.user.type === 'Bot') continue;
    const u = user(pr.user.login);
    u.prs += 1;
    if (pr.merged_at) u.merged += 1;
  }
  for (const report of reports) {
    const u = user(report.login);
    for (const { model, effort, tokens } of report.usage) {
      const name = modelName(model.trim());
      const level = typeof effort === 'string' && effort.trim() ? effort.trim() : null;
      // Group names that differ only in case, keeping the first spelling seen.
      const key = `${name}\n${level || ''}`.toLowerCase();
      if (!u.models.has(key)) u.models.set(key, { name, effort: level, prs: new Set(), tokens: 0 });
      const row = u.models.get(key);
      row.prs.add(report.pr);
      row.tokens += tokens;
      u.tokens += tokens;
    }
  }
  return users;
}

function render(users, ignored) {
  const n = (x) => x.toLocaleString('en-US');
  const byLogin = [...users]
    .map(([login, u]) => ({ login, ...u }))
    .sort((a, b) => b.prs - a.prs || b.tokens - a.tokens || a.login.localeCompare(b.login));
  const byModel = byLogin.flatMap((u) =>
    [...u.models.values()]
      .sort((a, b) => b.tokens - a.tokens || a.name.localeCompare(b.name))
      .map((m) => `| ${u.login} | ${m.name} | ${m.effort || '—'} | ${m.prs.size} | ${n(m.tokens)} |`),
  );
  const lines = [
    MARKER,
    'Pull requests and tokens per author, rebuilt by the `PR stats` workflow. Edits to this issue body are overwritten.',
    '',
    '| Author | PRs | Merged | Tokens |',
    '| --- | ---: | ---: | ---: |',
    ...byLogin.map((u) => `| ${u.login} | ${u.prs} | ${u.merged} | ${n(u.tokens)} |`),
    '',
    '**By model and effort**',
    '',
    '| Author | Model | Effort | PRs | Tokens |',
    '| --- | --- | --- | ---: | ---: |',
    ...byModel,
    '',
    'Tokens come from usage reports that `scripts/report_usage.py` posts as comments below. A report counts only if the pull request\'s author posted it and it hasn\'t been edited or hidden.',
  ];
  if (ignored.length) {
    lines.push('', `**Ignored reports (${ignored.length})**`, '', ...ignored.map((i) => `- ${i.url}: ${i.reason}`));
  }
  return lines.join('\n');
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

async function listComments({ github, context }, number) {
  const { owner, repo } = context.repo;
  const comments = [];
  let after = null;
  do {
    const { repository } = await github.graphql(COMMENTS_QUERY, { owner, repo, number, after });
    const page = repository.issue.comments;
    comments.push(...page.nodes);
    after = page.pageInfo.hasNextPage ? page.pageInfo.endCursor : null;
  } while (after);
  return comments;
}

module.exports = async ({ github, context, core }) => {
  const { owner, repo } = context.repo;
  const pulls = await github.paginate(github.rest.pulls.list, { owner, repo, state: 'all', per_page: 100 });
  const issue = await findOrCreateIssue({ github, context, core });
  const comments = await listComments({ github, context }, issue.number);
  const { reports, ignored } = selectReports(comments, new Map(pulls.map((p) => [p.number, p])));
  const body = render(tally(pulls, reports), ignored);
  if (issue.body !== body) {
    await github.rest.issues.update({ owner, repo, issue_number: issue.number, body });
  }
  core.info(`Updated issue #${issue.number} from ${pulls.length} pull requests and ${reports.length} usage reports.`);
};

module.exports.modelName = modelName;
module.exports.parseRecord = parseRecord;
module.exports.selectReports = selectReports;
module.exports.tally = tally;
module.exports.render = render;
