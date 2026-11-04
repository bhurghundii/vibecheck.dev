# Configuration

All config is environment variables, validated at startup — vibecheck refuses to boot with a missing required var or an insecure `VIBECHECK_TOKEN`, rather than failing confusingly later. See [`.env.example`](https://github.com/bhurghundii/vibecheck.dev/blob/main/.env.example) for the full annotated list; the ones you'll actually touch:

!!! note "Renamed from `GRUMPY_*`"
    `VIBECHECK_BASE_URL`, `VIBECHECK_TOKEN` and `VIBECHECK_ALLOWED_REPOS` used to be `GRUMPY_BASE_URL`, `GRUMPY_TOKEN` and `GRUMPY_ALLOWED_REPOS`. The old names still work as a fallback (the new name wins if both are set), but will be removed in a later release.

| Variable | Required | Default | What it does |
|---|---|---|---|
| `DATABASE_URL` | yes | — | Postgres connection string |
| `VIBECHECK_BASE_URL` | yes | — | Public URL used to build session links. Never derived from request headers, so it works correctly behind a proxy |
| `VIBECHECK_TOKEN` | yes | — | Shared bearer secret gating `POST /sessions` and `GET /verdict`. Rejected at startup if it's the placeholder or under 20 characters |
| `FAKE_GRADER` | yes | — | `true` = free/instant grading via a `looks-good` marker string (good for trying vibecheck out); `false` = real grading via `MODEL_PROVIDER` |
| `MODEL_PROVIDER` | no | `anthropic` | `anthropic` = the Anthropic API with `MODEL_API_KEY`; `bedrock` = Amazon Bedrock in your own AWS account (see [Amazon Bedrock](#amazon-bedrock)) |
| `MODEL_API_KEY` | only if `FAKE_GRADER=false` and `MODEL_PROVIDER=anthropic` | — | Anthropic API key used by the real grader |
| `BEDROCK_MODEL_ID` | only if `MODEL_PROVIDER=bedrock` | — | Bedrock model ID, inference profile ID or ARN |
| `AWS_REGION` | only if `MODEL_PROVIDER=bedrock` | — | Region to call Bedrock in. `AWS_DEFAULT_REGION` also works |
| `GITHUB_STATUS_TOKEN` | no | unset | GitHub token with *Commit statuses: Read and write* on the gated repos (see [Blocking PRs](blocking-prs.md#1-create-the-status-token)). vibecheck uses it to post the `vibecheck/verdict` status; unset, it posts nothing and your workflow has to gate on `GET /verdict` itself. A 401 from GitHub in the logs means the token is wrong or expired |
| `VIBECHECK_ALLOWED_REPOS` | no | unset (any repo) | Comma-separated `owner/name` allow-list. Defense-in-depth if `VIBECHECK_TOKEN` ever leaks |
| `EXAM_QUESTION_COUNT` | no | `5` | Maximum questions in the walkthrough, including the fixed high-level first one. The model writes the rest, each anchored to a part of the diff and shown on its own screen. Small changes get fewer: one scoped question per 15 changed lines, so a one-line PR gets only the high-level question. `PASSINGMARKS` is clamped to the sheet length |
| `PASSINGMARKS` | no | `3` | How many screens must be answered correctly to pass. The session fails as soon as that is out of reach. Must be between `1` and `EXAM_QUESTION_COUNT` |
| `MAX_QUESTION_ATTEMPTS` | no | `3` | Tries per question before vibecheck reveals the answer and moves on. A revealed question counts for nothing. `1` = one try, no retries |
| `ALLOW_RESTART` | no | `false` | Show a **Start over** button on failed sessions, which resets them to pending with a fresh set of questions on the same commit and link. Unlimited, so turning it on weakens the gate; off by default. See [Blocking PRs](blocking-prs.md#failing-and-trying-again) |
| `MEANIEMODE` | no | `false` | Failure notes become sarcastic and merciless instead of professional. Doesn't change which answers passed, only tone |
| `EVALUATOR` | no | unset (defaults below) | JSON config for checks that reject a PR before any question is asked: `POST /sessions` returns `422` with the reason, and the `grill` job comments it on the PR and goes red. Today there is one check, `size`: `{"size": {"max_changed_lines": 1000, "exclude": ["*.lock", "package-lock.json", "pnpm-lock.yaml", "go.sum"]}}` (those are the defaults). Changed lines are added + removed lines, not context; files matching an `exclude` glob (path or basename) don't count. `max_changed_lines: 0` turns the check off. Omitted keys keep their defaults; unknown keys or bad values fail startup |
| `MAX_DIFF_BYTES` | no | `400000` | Larger diffs are rejected like an `EVALUATOR` check (`422`, red `grill` job). Bounded by the model's context window, not by Postgres: at ~3–4 bytes per token, 400 KB is ~100k–130k tokens. Raise it much further and you accept diffs that can never be graded |
| `SESSION_TTL_DAYS` | no | `7` | How long a question link stays answerable. After that it shows "This session expired" and the `vibecheck/verdict` status stays pending; pushing a new commit creates a fresh session |

## Amazon Bedrock

Set `MODEL_PROVIDER=bedrock` to grade through [Amazon Bedrock](https://aws.amazon.com/bedrock/) instead of the Anthropic API, so diffs and answers never leave your AWS account and spend lands on your AWS bill. vibecheck calls Bedrock's [Converse API](https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html), so any Converse-capable model works: Claude on Bedrock, Amazon Nova, Llama, Mistral and so on.

```env
FAKE_GRADER=false
MODEL_PROVIDER=bedrock
BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-5-20250929-v1:0
AWS_REGION=us-east-1
```

| Variable | Default | What it does |
|---|---|---|
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_PROFILE` | unset | Credentials, resolved by the standard AWS chain. Inside AWS, leave these unset and give the task, instance or pod a role instead (ECS task role, EC2 instance profile, EKS IRSA) |
| `AWS_BEARER_TOKEN_BEDROCK` | unset | A Bedrock API key. When set, it is sent as a bearer token and the credential chain is not used |
| `BEDROCK_MAX_TOKENS` | `4096` | Output token cap per call. Every reply is short; lower it for a model with a smaller output limit |
| `BEDROCK_ENDPOINT_URL` | `https://bedrock-runtime.<AWS_REGION>.amazonaws.com` | Override for a VPC interface endpoint or a gateway in front of Bedrock |

The identity needs `bedrock:InvokeModel` on the model (and on the underlying foundation models, when `BEDROCK_MODEL_ID` is a cross-region inference profile), and the model must be enabled in that region. A missing permission shows up in the logs as a `403` with AWS's message, and the developer sees "grading failed".

Prompts are tuned and evaluated on Claude. Other models are asked for JSON in the prompt rather than through a structured-output mode, so a weaker model can return unparseable marks more often; run a few real PRs through it before relying on it as a gate.
