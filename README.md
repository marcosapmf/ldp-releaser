# LDP Releaser

A small web page that runs on your machine and drives a release from start
to finish: it builds the backend (Asah) in Jenkins, creates and moves the
Jira tickets, opens the Helm charts pull requests, waits for them to be
merged, asks you to confirm the rollout, builds and deploys the frontend
(Faro), tags the released commits in GitHub and closes the tickets.

Everything specific to a team (Jenkins, Jira, repositories, environments)
comes from settings files that are **not** part of this repository.

## Requirements

- Python 3.11 or newer (only the standard library is used)
- git 2.25 or newer
- [GitHub CLI](https://cli.github.com/) logged in with `gh auth login`, with
  push access to the charts repository and the repositories you tag
- An SSH key or credential helper that lets `git push` to the charts repository
- A local clone of the charts repository
- The gcloud CLI with your own `gcloud auth login`, if the team file lists
  gcloud configurations

## Getting started

```sh
git clone <this repository> ldp-releaser
cd ldp-releaser
git config core.hooksPath hooks   # enables the leak check before each commit
./ldp-releaser
```

The first start asks for:

1. **The team settings file** (`ldp-releaser.team.toml`). Get it from your
   team; it is shared privately (for example through 1Password). Give its
   path, or press Enter to fetch it with the 1Password CLI (`op`) if you have
   it installed.
2. **Your own settings**, each checked on the spot:
   - your Jenkins user ID and API token
   - your Atlassian account email and API token
   - the path to your clone of the charts repository
   - the port for the page (default 8766)

If the team file lists gcloud configurations, the setup also creates the
missing ones with your own gcloud login (run `gcloud auth login` first).
Configurations you already have are never changed, and your active
configuration stays active.

Then open `http://127.0.0.1:<port>`. Later starts go straight to the page.
Run `./ldp-releaser --setup` to replace the team file or change your
credentials, for example after a token rotation.

Both files are saved in `~/.config/ldp-releaser/` and are readable only by
you. They contain secrets: never share your personal file, and never commit
either of them.

## Settings

| File | Who writes it | Contents |
|---|---|---|
| `ldp-releaser.team.toml` | the team owner, shared privately | Jenkins, Jira and Argo CD URLs, job and parameter names, repositories, values files per environment, Jira workflow, Slack webhook |
| `config.toml` | the first-run setup, one per person | Jenkins user and token, Jira email and token, charts clone path, port |

See [`team.example.toml`](team.example.toml) and
[`config.example.toml`](config.example.toml) for every setting, with
placeholder values.

### Updating the team settings

Whoever owns the team file edits their local copy, then replaces the shared
one. With the 1Password CLI:

```sh
op document edit "<item>" ~/.config/ldp-releaser/ldp-releaser.team.toml --vault "<vault>"
```

Then tell the team to run `./ldp-releaser --setup` and answer yes to
replacing the team settings, so everyone fetches the new version. If a
newer version of LDP Releaser needs a setting the file does not have yet,
the next start lists the missing settings and asks for an updated file: add
them to the shared file the same way, then fetch it again.

Personal settings can also come from environment variables, which take
precedence over the file: `LDP_JENKINS_USER`, `LDP_JENKINS_TOKEN`,
`LDP_JIRA_EMAIL`, `LDP_JIRA_TOKEN` (`JIRA_API_USER` and `JIRA_API_TOKEN` are
used as a fallback), `LDP_CHARTS_CHECKOUT`, `LDP_ASAH_CHECKOUT`, `LDP_PORT` and
`LDP_SLACK_WEBHOOK_URL`.

## What a run does

The **Release** tab runs Asah and Faro together, with one environment picker
for everything: Asah is released, the Composer DAGs are uploaded (if ticked)
and Faro is built and deployed to the same environments, one environment
after the other for Faro. The **Asah** tab only runs
the Asah part (build, tickets, charts pull requests, rollout, optional
Composer DAGs, tag), and the **Faro** tab only builds, deploys and tags
Faro. Each tab shows its own latest run; earlier runs are in **History**.

Each section of the form is optional; leave it empty to skip those steps.

1. **Asah build**: runs the build job for the branch and reads the image tag
   suffix and the built commit from its log.
2. **Jira tickets**: creates a Task and a Technical Task (or reuses open
   ones, see below) and moves them to In Progress.
3. **Charts pull request**: sets the tag suffix in each selected
   environment's values file, opens a pull request to the develop branch,
   calls the Slack workflow and waits for the merge.
4. **Production pull request** (only for environments listed in
   `charts.production.environments`): cherry-picks the merged commit onto
   the production branch, opens a pull request and waits for the merge.
5. **Rollout confirmation**: links to the Argo CD instance of each
   environment; you confirm once the apps run the new suffix and are Healthy.
6. **Composer DAGs** (optional, when the team file has the Composer setup):
   uploads the DAGs of the released Asah commit to the gcloud configurations
   of the released environments, checking the gcloud login first (see
   below).
7. **Faro build and deploy**: builds the branch for the configured build
   environment and deploys the image it pushed.
8. **GitHub tags** (PRD releases only): tags the exact commits that were
   built.
9. **Closing the tickets**: automatic for PRD releases; otherwise the page
   asks whether to close them or keep them open.

If open release tickets exist when you start a run (for example from a
release that did not finish), the form asks whether to reuse them or to
create new ones and close the old ones as discarded. Tickets of the whole
team are offered, with their assignee.

Runs are saved in `~/.local/share/ldp-releaser/runs/`. If the server stops,
the next start resumes unfinished runs: Jenkins builds are re-attached and
waits for merges, the rollout or a decision start again. A run that stopped
while changing Jira, git or GitHub is marked as failed instead, so nothing
is done twice; start a new run and reuse its tickets.

## Composer DAGs

If the team file has the Composer setup (`asah.composer_directory`,
`asah.composer_script` and `gcloud.environments`), the **Composer** tab
uploads the DAGs of an Asah branch, tag or commit to the Composer
environments of the chosen release environments. It is separate from
releases and only runs when you start it.

1. It checks that your gcloud login still works. If it has expired, the
   page shows **Log in to gcloud**: it starts `gcloud auth login`, which
   opens the sign-in page in your browser (or shows the link and asks for
   the verification code), and the upload continues once you are signed in.
2. It fetches that version from the team's Asah repository into a temporary
   worktree of your Asah clone, so your own branch and changes are untouched.
3. For each gcloud configuration of the chosen environments, it runs the
   upload script with `LCP_PROJECT_ID`, `PROJECT_ID` and `REGION` taken from
   the team file, never from your active gcloud configuration. If the login
   expires during an upload, it waits for a new login and uploads again.

The page has a setup panel that checks Jenkins, Jira, GitHub, the charts
clone, git, Python and Slack, and keeps **Run** disabled while something
required is broken.

## Security

- The page only answers on `127.0.0.1` and rejects requests from other sites.
- Settings files are created with `600` permissions.
- `scripts/check_leaks.py` (run by the pre-commit hook and the tests) fails
  when a file contains a token, a Slack webhook, an internal host name or any
  value from your local settings files. This repository is public: keep it
  free of anything that identifies internal systems.

## Development

```sh
python3 -m unittest discover -s tests -t .
```

The tests run the real app against in-process fakes of Jenkins, Jira and
Slack, a fake `gh` and a throwaway charts repository, so they never touch
real systems.
