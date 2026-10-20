# Quickstart

Self-host it locally.

Requires Docker and Docker Compose.

```sh
git clone https://github.com/bhurghundii/vibecheck.dev.git
cd vibecheck.dev
cp .env.example .env
# edit .env: set VIBECHECK_TOKEN to a real secret (see below) and VIBECHECK_BASE_URL
docker compose up --build
curl localhost:8000/healthz
# {"status": "ok"}
```

Generate a real token rather than hand-typing one:

```sh
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

By default vibecheck runs with `FAKE_GRADER=true`, which passes any answer containing the literal string `looks-good` — enough to see the whole flow end to end with zero API cost. To grade for real, set `FAKE_GRADER=false` and `MODEL_API_KEY=<your Anthropic API key>` in `.env`, or use your own AWS account with `MODEL_PROVIDER=bedrock` (see [Amazon Bedrock](configuration.md#amazon-bedrock)).

Try the answer page without wiring up a GitHub Action at all:

```sh
make seed   # inserts a realistic session directly into Postgres, prints its URL
```

Open the printed URL, answer the question, and watch the verdict resolve.
