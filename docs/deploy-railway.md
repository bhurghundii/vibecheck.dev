# Deploying to Railway

The only deployment path tested so far.

The reference deployment of vibecheck runs on [Railway](https://railway.com). Nothing in vibecheck depends on Railway — any host that can run the Dockerfile next to a Postgres 16 database works - I just personally use Railway cause it was easy:

1. **Create the project from your fork.** In Railway, *New Project → Deploy from GitHub repo* and pick your fork. Railway detects the `Dockerfile` and builds from it; no `railway.toml` is needed.
2. **Add Postgres.** In the same project, *+ New → Database → PostgreSQL*. Migrations apply automatically when the app starts, so there's nothing to run by hand.
3. **Set the app service's variables** (*Variables* tab):

   | Variable | Value |
   |---|---|
   | `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` (a reference to the Postgres service; adjust the name if you renamed it) |
   | `VIBECHECK_TOKEN` | output of `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
   | `FAKE_GRADER` | `true` to try it out, `false` for real grading |
   | `MODEL_API_KEY` | your Anthropic API key (only needed when `FAKE_GRADER=false`; or set `MODEL_PROVIDER=bedrock` and the [Bedrock variables](configuration.md#amazon-bedrock) instead) |
   | `PORT` | `8000` — the container always listens on 8000, so tell Railway to route there |
   | `VIBECHECK_BASE_URL` | filled in after the next step |
   | `GITHUB_STATUS_TOKEN` | a fine-grained GitHub token with *Commit statuses: read and write* on the repos you'll gate — lets vibecheck post the `vibecheck/verdict` check |

4. **Give it a public URL.** *Settings → Networking → Generate Domain*, with target port `8000`. Then set `VIBECHECK_BASE_URL` to that domain including the scheme (e.g. `https://your-app.up.railway.app`) and let it redeploy. vibecheck won't boot without it.
5. **Point Railway's healthcheck at `/healthz`** (*Settings → Deploy → Healthcheck Path*), so a deploy that can't reach Postgres or fails config validation never takes traffic.
6. **Check it's up:** `curl https://your-app.up.railway.app/healthz` should return `{"status": "ok"}`.
7. **Wire up the repo** as below, using that same URL for the `VIBECHECK_BASE_URL` secret and the same token for `VIBECHECK_TOKEN`.

The `Dockerfile` intentionally skips a BuildKit cache mount because Railway's builder requires one scoped to its own internal service ID (see the comment in the file). A Railway domain is still on the public internet, so the [before you make it public](security.md) checklist applies here too.
