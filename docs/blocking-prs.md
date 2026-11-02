# Blocking a PR until it passes

By default vibecheck is advisory: the `grill` job comments a question link and passes. To make the answer gate merging, vibecheck has to report a `vibecheck/verdict` commit status, and GitHub has to require it. This page walks the whole path, in order.

## What you need

- A running vibecheck server that GitHub's runners can reach (see [Deploying to Railway](deploy-railway.md)).
- A public repo, or a private repo on a plan that supports branch protection or rulesets. On a free account, GitHub only offers them for public repos; the API answers "Upgrade to GitHub Pro or make this repository public". On a private repo without the plan, the status still shows on the PR but nothing stops the merge button.
- The [workflow](wire-it-up.md) in the repo you want to gate.

## 1. Create the status token

vibecheck posts the verdict with a GitHub token you give it. Make a fine-grained token:

1. GitHub -> Settings -> Developer settings -> Personal access tokens -> Fine-grained tokens -> Generate new token.
2. Resource owner: the account or org that owns the repo. Repository access: only the repos you will gate.
3. Repository permissions: **Commit statuses: Read and write**. Metadata read is added automatically.
4. Set an expiry you will remember. Copy the token (it starts with `github_pat_`); GitHub shows it once.

A classic token with only the `repo:status` scope also works for public repos, but it is not limited to one repo, so prefer the fine-grained one.

## 2. Set the variables

On the vibecheck server, set `GITHUB_STATUS_TOKEN` to the token from step 1, with no quotes or trailing spaces, and redeploy. In the startup log you should see `commit status reporting enabled`; `commit status reporting disabled (GITHUB_STATUS_TOKEN unset)` means vibecheck will post nothing.

In the repo (Settings -> Secrets and variables -> Actions, or `gh secret set`), add two secrets:

| Secret | Value |
|---|---|
| `VIBECHECK_BASE_URL` | the server's public URL, with the scheme and no trailing slash |
| `VIBECHECK_TOKEN` | the same value as the server's `VIBECHECK_TOKEN` |

## 3. Open a PR and check the status appears

Open a PR (from a branch or a fork). Within a minute or so:

- the `grill` job passes and comments a question link;
- the PR shows `vibecheck/verdict` as **pending** with "Waiting for the PR author to answer".

If no `vibecheck/verdict` appears, see [Troubleshooting](#troubleshooting). Do not require it until it shows up, or every PR will be stuck.

## 4. Require the check

In the repo, Settings -> Branches (or Settings -> Rules -> Rulesets), add or edit a rule for your default branch, enable "Require status checks to pass before merging", and add `vibecheck/verdict`. The search only lists a status once it has been reported on a PR, which is why step 3 comes first. Do not add `grill`: it opens the session and passes even when vibecheck is down.

You can also add it to an existing ruleset with the API, which works even before the UI lists it:

```sh
gh api -X PUT repos/OWNER/REPO/rulesets/RULESET_ID --input ruleset.json
```

with a `required_status_checks` rule in the ruleset's `rules`:

```json
{"type": "required_status_checks",
 "parameters": {"strict_required_status_checks_policy": false,
                "required_status_checks": [{"context": "vibecheck/verdict"}]}}
```

Rulesets with no bypass actors block everyone, including you, so keep a way out until you have seen the gate work: add the repository admin role as a bypass actor in "pull request" mode, which keeps the merge button disabled by default and offers an explicit bypass.

## What the author sees

`vibecheck/verdict` is pending until the walkthrough is decided, green once enough questions are answered well enough, and red if too many are failed. The merge button stays disabled until it is green. To see both paths, answer wrong on one PR and right on another; with `FAKE_GRADER=true`, an answer passes if it contains `looks-good`.

## Failing and trying again

By default a failed verdict is final: the author pushes a new commit to get a fresh session. Set `ALLOW_RESTART=true` on the server to offer a way back without a new commit. The result page then has a **Start over** button that resets the same session on the same commit and link: the status goes back to pending, and the author gets a fresh set of questions (vibecheck asks for ones it hasn't asked before, since the failed attempt revealed the answers). There is no limit on restarts. Each one costs one model call, spent only when someone presses the button on a failed session.

Pushing a new commit also creates a fresh session, as before. Because restarts are unlimited, vibecheck checks understanding but cannot stop someone grinding through retries; if you need a hard limit, add one at your proxy or ask for it as a setting.

## Things that stop or stall a PR

- **Rejected diffs.** A diff that trips an `EVALUATOR` check or exceeds `MAX_DIFF_BYTES` is rejected before any question is asked and turns the `grill` job red.
- **Expired sessions.** A session lasts `SESSION_TTL_DAYS` (default 7). After that the question link shows "This session expired", and the status stays pending, so the PR stays blocked. Pushing a new commit creates a fresh session. Raise `SESSION_TTL_DAYS` if reviews run longer than a week.
- **Waiting costs nothing.** An unanswered verdict is a database row; there is no timer, nagging or per-day charge.
- **Admin bypass.** To keep admins from merging around the gate, enable "Do not allow bypassing the above settings" (or remove bypass actors from the ruleset).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `grill` warns that secrets are not set | `VIBECHECK_BASE_URL` or `VIBECHECK_TOKEN` missing in the repo |
| `grill` passes but there is no question comment | The server returned an error; the job log has the response |
| Question comment appears but no `vibecheck/verdict` | `GITHUB_STATUS_TOKEN` unset on the server (look for the `disabled` startup line) |
| Server log: GitHub rejected the commit status, 401 | The token is wrong, expired or revoked, or has stray quotes or spaces; create a new one and redeploy |
| Same, 403 or 404 | The token is valid but lacks *Commit statuses: Read and write* on this repo |
| `/healthz` returns 502 | The service is not answering: check the deploy logs, and that the domain's target port and `PORT` are both `8000` |
| Every request returns 401 | `VIBECHECK_TOKEN` differs between the server and the repo secret |
| PR is green although you failed the quiz | `vibecheck/verdict` is not a required check yet, or never reported; green `grill` is expected |

Variables are read at startup, so redeploy after changing any of them. Pushing a new commit to the PR creates a new session and re-posts the status.
