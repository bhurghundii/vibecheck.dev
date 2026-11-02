# Wire it into a repo

Add a workflow that opens a vibecheck session on every PR. Its one job, `grill`, is non-blocking: it comments the question link and passes, and if vibecheck is unreachable or rejects the request it warns instead of failing the PR. To make the answer gate merging, also require the `vibecheck/verdict` status in branch protection; leave it out and vibecheck is advisory. Minimal shape:

```yaml
on:
  # pull_request_target so fork PRs get the secrets too. Safe only because
  # no step checks out or runs the PR's code: see the note below.
  pull_request_target:
    types: [opened, synchronize, reopened]

jobs:
  grill:
    runs-on: ubuntu-latest
    steps:
      - name: Ask vibecheck
        env:
          VIBECHECK_BASE_URL: ${{ secrets.VIBECHECK_BASE_URL }}
          VIBECHECK_TOKEN: ${{ secrets.VIBECHECK_TOKEN }}
          GH_TOKEN: ${{ github.token }}
        run: |
          # Fetch the diff from the API (compare BASE...HEAD), never from a
          # checkout. POST /sessions with it and comment the link on the PR.
          # On any error, emit a ::warning and exit 0 so the job never
          # blocks. vibecheck posts the vibecheck/verdict status itself. Full
          # script with all the edge cases handled: see the workflow
          # linked below.
```

The exact working workflow this repo uses on itself is [`.github/workflows/vibecheck.yml`](https://github.com/bhurghundii/vibecheck.dev/blob/main/.github/workflows/vibecheck.yml). You'll need two repo/org secrets: `VIBECHECK_BASE_URL` (your deployment's public URL) and `VIBECHECK_TOKEN` (the same value the server is configured with), plus `GITHUB_STATUS_TOKEN` set on the server.

**Fork PRs.** The workflow uses `pull_request_target` so that PRs from forks are gated as well. GitHub gives a plain `pull_request` run from a fork no secrets, so those PRs would be skipped. A `pull_request_target` run gets secrets and a write token, and always runs the workflow file from the base branch, so a fork cannot change what it does. That is only safe while the workflow never runs code from the PR. It fetches the diff from the GitHub API as text and passes it to `jq` as data. Do not add an `actions/checkout` of the PR's head, or any step that builds, installs or runs files from the PR.
