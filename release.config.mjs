// semantic-release config — consistent with the other GoPay SDK repos: no 'v' prefix.
// The tag is the version: hatch-vcs reads it at build time, so nothing in the tree is bumped.
// Publishing to PyPI happens on the GitHub mirror (.github/workflows/pypi-publish.yml).

// Release notes link to the public GitHub mirror, never to the internal Bitbucket repository.
// semantic-release builds the commit and compare links from repositoryUrl, which stays the
// Bitbucket origin because that is where it pushes the release commit and tag. The notes end up
// in CHANGELOG.md (also in the sdist) and in the release commit message, all of them public, so
// they are pointed at the mirror instead. The mirror is a force-push of Bitbucket master and its
// tags, so every commit hash and tag the notes name exists there once "Mirror to GitHub" runs.
const GITHUB_HOST = 'https://github.com';
const GITHUB_OWNER = 'gopaycommunity';
const GITHUB_REPOSITORY = 'gopay-python-api-v4';

// "#12" and "@name" in a commit subject name a Bitbucket pull request or user, which mean nothing
// on the mirror. The preset's transform links them in the subject (and nowhere else) from the
// Bitbucket context before finalizeContext runs, so they are turned back into plain text here.
const unlink = (text) => (typeof text === 'string' ? text.replace(/\[([#@][^\]]*)\]\([^)]*\)/g, '$1') : text);

// The conventionalcommits preset builds commit and compare URLs as {{host}}/{{owner}}/{{repository}}/….
const linkToGitHub = (context) => ({
  ...context,
  host: GITHUB_HOST,
  owner: GITHUB_OWNER,
  repository: GITHUB_REPOSITORY,
  commitGroups: context.commitGroups.map((group) => ({
    ...group,
    // "closes #12" references name Bitbucket pull requests too.
    commits: group.commits.map((commit) => ({ ...commit, subject: unlink(commit.subject), references: [] })),
  })),
});

// Bitbucket, Jira, and GoPay's internal environments — the spec feed CLAUDE.md names among them,
// whose hostname is otherwise kept out of this repository.
const INTERNAL_HOST = /bitbucket\.org|atlassian\.net|\b(?:beta|dev|test)\.gopay\.com\b/i;

// Inline plugin, listed after the notes generator: fails the release if the notes still name an
// internal host. generateNotes gets the notes the plugins before it produced, and returning
// nothing adds nothing to them. It runs in the PR's dry-run preview too, but only when the PR
// carries a commit that releases (feat, fix, perf, refactor, breaking): with nothing to release,
// semantic-release stops before generating notes.
const assertPublicNotes = {
  generateNotes: (_pluginConfig, { nextRelease }) => {
    const match = INTERNAL_HOST.exec(nextRelease.notes ?? '');
    if (match) {
      throw new Error(`Release notes name the internal host ${match[0]}. They are public (CHANGELOG.md, the release commit, PyPI), so they may only link to ${GITHUB_HOST}/${GITHUB_OWNER}/${GITHUB_REPOSITORY}.`);
    }
  },
};

export default {
  branches: ['master'],
  tagFormat: '${version}',
  plugins: [
    ['@semantic-release/commit-analyzer', {
      preset: 'conventionalcommits',
      releaseRules: [
        { type: 'feat',     release: 'minor' },
        { type: 'fix',      release: 'patch' },
        { type: 'perf',     release: 'patch' },
        { type: 'refactor', release: 'patch' },
        { breaking: true,   release: 'major' },
      ],
    }],
    ['@semantic-release/release-notes-generator', {
      preset: 'conventionalcommits',
      writerOpts: { finalizeContext: linkToGitHub },
    }],
    [assertPublicNotes],
    ['@semantic-release/changelog', {
      changelogFile: 'CHANGELOG.md',
    }],
    ['@semantic-release/git', {
      assets: ['CHANGELOG.md'],
      message: 'chore(release): ${nextRelease.version} [skip ci]\n\n${nextRelease.notes}',
    }],
  ],
};
