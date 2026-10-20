"""Grading behind a protocol, over async httpx against the raw Messages API (no SDK),
or against Amazon Bedrock's Converse API for self-hosting inside AWS.

interpret reads the diff blind, once, at session creation, so no answer is in
view to rationalise toward. generate_exam writes the questions, and
grade_answer marks one screen against the stored interpretation.
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

MODEL = "claude-opus-5"
PROMPT_VERSION = "v8"

_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"

# Bedrock serves models with smaller output limits than Claude's, and every
# reply here is short: a few questions, a mark, or two paragraphs.
DEFAULT_BEDROCK_MAX_TOKENS = 4096

# Fixed first question; it covers the whole change, so it has no line anchor.
HIGH_LEVEL_QUESTION = "How does this change work on a high level?"

_MAX_TOKENS = 16000
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504, 529})
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 1.0
_MAX_RETRY_AFTER_SECONDS = 10.0

_INTERPRETATION_SYSTEM_PROMPT = """\
You are analysing a code diff in isolation. You will not see any developer \
answer — do not speculate about one.

List the discrete, verifiable claims about what this diff changes, as a \
short bulleted list. Base every claim strictly on what the diff shows. Do \
not infer intent beyond what the diff makes visible."""

_EXAM_GEN_SYSTEM_PROMPT = """\
You are setting a short, paged exam that checks whether a developer \
understands a code diff they are responsible for. Each question gets its \
own screen, which shows the slice of the diff the question is about.

Write up to {count} questions, fewer if the change does not have that many \
distinct parts worth asking about. Never pad: a small change deserves a \
small exam. Each must be answerable from the diff alone, target a specific \
and different part of the change, and be anchored to the lines it asks \
about. Ask only about the code or content that was actually changed. Never \
ask about git metadata: `diff --git`, `index`, file mode, `new file`, \
`---`/`+++` and `@@` lines are tooling output, not the developer's work. Ask why something is done, what would break if \
it were wrong, or what a changed line now does — not yes/no questions, and \
not ones answered by reading a single line back verbatim. Do not ask a \
broad "what does this change do overall" question: that one is asked \
separately and must not be duplicated here.

The diff below is numbered: every line starts with its 1-based index. For \
each question:
- `start_line` and `end_line` are inclusive and must be indices that \
actually appear in the numbered diff.
- Keep the range tight — the few lines the question is really about, plus \
an unchanged line or two around them if it aids reading.
- Order the questions by where their lines appear in the diff.
Keep each `question` to a single sentence.

Also give each question a `reference_answer`: a short model answer to it, \
one or two sentences in plain language, which is shown to the developer only \
if they run out of attempts on that question. Base it strictly on the diff."""

_EXAM_QUESTIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                    "reference_answer": {"type": "string"},
                },
                "required": ["question", "start_line", "end_line", "reference_answer"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}

_EXPLAIN_SYSTEM_PROMPT = """\
A developer is working through a diff one question at a time and has asked \
for help understanding the part this question is about. Explain, in plain \
teaching language, what that section of the change does and why — enough for \
them to then answer the question in their own words. Base it strictly on \
what the diff shows; do not infer intent beyond it. Do NOT give the answer \
to the question outright — explain the code so they can. Do not be "vibecheck" \
or sarcastic here; this is a teaching moment. Two short paragraphs at most."""

_GRADE_PREAMBLE = """\
You are marking a developer's answer to one question about a code change. \
You are given an independent interpretation of the diff, produced without \
seeing the answer, so it is not biased toward it. Mark the answer against \
that interpretation."""

# A vague-but-correct answer passes; only a wrong or empty one fails.
_GRADE_RULE = """\
Marking rule (decides `passed`, and is not affected by the tone guidance \
below):
- An answer that states something the diff does not do fails.
- An answer that is vague, partial, or high-level still passes, as long as \
it is recognisably about what the question asked and nothing in it is \
wrong. Accept answers at a high level — do not demand completeness or \
precise detail.
- An empty answer, or one with nothing specific to this change in it (e.g. \
"fixes stuff", "see the diff"), fails: there is nothing to check.
- Terse phrasing is fine. Poorly written or non-native English phrasing is \
fine. Wrong content is not."""

_GRADE_TONE_MEAN = """\
You are "vibecheck" — live up to the name in `note`, but only there; `passed` \
is decided purely by the marking rule above.
- If passed: one plain clause confirming what they got right. No praise.
- If failed: be scathing and specific — name exactly which claim from the \
interpretation the answer contradicted or ignored, so the insult and the \
explanation are the same sentence.

Respond with your mark."""

_GRADE_TONE_PROFESSIONAL = """\
Keep `note` direct, specific, and professional — no sarcasm, mockery, or \
personal remarks, regardless of the mark.
- If passed: one plain clause confirming what they got right. No praise.
- If failed: state plainly what was wrong or missing, naming the claim \
from the interpretation it contradicted or ignored, like a terse \
code-review comment.

Respond with your mark."""


def _grade_system_prompt(*, meaniemode: bool) -> str:
    tone = _GRADE_TONE_MEAN if meaniemode else _GRADE_TONE_PROFESSIONAL
    return f"{_GRADE_PREAMBLE}\n\n{_GRADE_RULE}\n\n{tone}"


_MARK_SCHEMA = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "note": {"type": "string"},
    },
    "required": ["passed", "note"],
    "additionalProperties": False,
}

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


@dataclass
class ExamQuestion:
    """One screen's question. start_line/end_line are an inclusive 1-based range
    of the diff, or None for the high-level question."""

    question: str
    start_line: int | None = None
    end_line: int | None = None
    # Shown only if the developer runs out of attempts on this question.
    reference_answer: str = ""


@dataclass
class QuestionMark:
    """One answer's result: whether it passed and a short note on why."""

    passed: bool
    note: str
    model: str | None = None
    prompt_version: str | None = None


class Grader(Protocol):
    async def interpret(self, diff: str) -> str: ...

    async def generate_exam(
        self, diff: str, count: int, avoid: list[str] | None = None
    ) -> list[ExamQuestion]: ...

    async def grade_answer(
        self, interpretation: str, question: str, answer: str
    ) -> QuestionMark: ...

    async def explain(self, interpretation: str, question: str) -> str: ...


class GradingError(Exception):
    """The grader could not produce a result; treat the screen as unanswered."""


class FakeGrader:
    """Passes an answer only if it contains MARKER; used when FAKE_GRADER=true."""

    MARKER = "looks-good"

    async def interpret(self, diff: str) -> str:
        return "FAKE interpretation"

    async def generate_exam(
        self, diff: str, count: int, avoid: list[str] | None = None
    ) -> list[ExamQuestion]:
        """High-level question, then scoped questions over equal slices of the diff."""
        questions = [
            ExamQuestion(question=HIGH_LEVEL_QUESTION, reference_answer="FAKE reference answer 1")
        ]
        scoped = max(count - 1, 0)
        if scoped == 0:
            return questions
        total = max(len(diff.splitlines()), 1)
        size = max(total // scoped, 1)
        for n in range(scoped):
            start = n * size + 1
            end = total if n == scoped - 1 else min((n + 1) * size, total)
            if start > total:
                start = end = None
            questions.append(
                ExamQuestion(
                    question=f"FAKE scoped question {n + 2}?",
                    start_line=start,
                    end_line=end,
                    reference_answer=f"FAKE reference answer {n + 2}",
                )
            )
        return questions

    async def grade_answer(self, interpretation: str, question: str, answer: str) -> QuestionMark:
        if self.MARKER in answer:
            return QuestionMark(passed=True, note=f"contains '{self.MARKER}'")
        return QuestionMark(passed=False, note=f"missing '{self.MARKER}'")

    async def explain(self, interpretation: str, question: str) -> str:
        return f"FAKE explanation for: {question}"


class RealGrader:
    """Grader over the Anthropic Messages API. client_factory lets evals and
    tests replay recorded responses."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 120.0,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        meaniemode: bool = False,
    ) -> None:
        self._api_key = api_key
        self._model = MODEL
        self._client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=timeout))
        self._meaniemode = meaniemode

    async def interpret(self, diff: str) -> str:
        try:
            async with self._client_factory() as client:
                return await self._interpret(client, diff)
        except GradingError:
            raise
        except Exception as exc:  # httpx errors, timeouts, anything unexpected
            raise GradingError(f"interpretation call failed: {exc}") from exc

    async def generate_exam(
        self, diff: str, count: int, avoid: list[str] | None = None
    ) -> list[ExamQuestion]:
        """HIGH_LEVEL_QUESTION plus up to count-1 anchored questions; may be fewer.
        `avoid` lists questions from an earlier attempt, so a restart asks fresh ones."""
        scoped_wanted = max(count - 1, 0)
        if scoped_wanted == 0:
            return [ExamQuestion(question=HIGH_LEVEL_QUESTION)]
        line_count = len(diff.splitlines())
        try:
            async with self._client_factory() as client:
                data = await self._call(
                    client,
                    system=_EXAM_GEN_SYSTEM_PROMPT.format(count=scoped_wanted),
                    user_content=_exam_user_content(diff, avoid),
                    output_schema=_EXAM_QUESTIONS_SCHEMA,
                )
                _check_stop_reason(data)
                scoped = _parse_questions(_extract_text(data), line_count)
        except GradingError:
            raise
        except Exception as exc:  # httpx errors, timeouts, anything unexpected
            raise GradingError(f"exam generation call failed: {exc}") from exc
        return [ExamQuestion(question=HIGH_LEVEL_QUESTION), *scoped[:scoped_wanted]]

    async def grade_answer(self, interpretation: str, question: str, answer: str) -> QuestionMark:
        try:
            async with self._client_factory() as client:
                user_content = (
                    f"Interpretation of the change:\n{interpretation}\n\n"
                    f"Question:\n{question}\n\n"
                    f"Developer's answer:\n{answer}\n\n"
                    "Mark the answer per the marking rule above."
                )
                data = await self._call(
                    client,
                    system=_grade_system_prompt(meaniemode=self._meaniemode),
                    user_content=user_content,
                    output_schema=_MARK_SCHEMA,
                )
                _check_stop_reason(data)
                mark = _parse_mark(_extract_text(data))
        except GradingError:
            raise
        except Exception as exc:  # httpx errors, timeouts, anything unexpected
            raise GradingError(f"grading call failed: {exc}") from exc
        return QuestionMark(
            passed=mark["passed"],
            note=mark["note"],
            model=self._model,
            prompt_version=PROMPT_VERSION,
        )

    async def explain(self, interpretation: str, question: str) -> str:
        try:
            async with self._client_factory() as client:
                data = await self._call(
                    client,
                    system=_EXPLAIN_SYSTEM_PROMPT,
                    user_content=(
                        f"Interpretation of the change:\n{interpretation}\n\n"
                        f"The question the developer is stuck on:\n{question}"
                    ),
                )
                _check_stop_reason(data)
                text = _extract_text(data).strip()
        except GradingError:
            raise
        except Exception as exc:  # httpx errors, timeouts, anything unexpected
            raise GradingError(f"explanation call failed: {exc}") from exc
        if not text:
            raise GradingError("model returned an empty explanation")
        return text

    async def _interpret(self, client: httpx.AsyncClient, diff: str) -> str:
        data = await self._call(
            client, system=_INTERPRETATION_SYSTEM_PROMPT, user_content=f"Diff:\n\n{diff}"
        )
        _check_stop_reason(data)
        text = _extract_text(data).strip()
        if not text:
            raise GradingError("model returned an empty interpretation")
        return text

    async def _call(
        self,
        client: httpx.AsyncClient,
        *,
        system: str,
        user_content: str,
        output_schema: dict | None = None,
    ) -> dict:
        body: dict = {
            "model": MODEL,
            "max_tokens": _MAX_TOKENS,
            "system": system,
            "messages": [{"role": "user", "content": user_content}],
        }
        if output_schema is not None:
            body["output_config"] = {"format": {"type": "json_schema", "schema": output_schema}}

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        }
        return await _post_with_retries(client, _API_URL, headers, json.dumps(body).encode())


class BedrockGrader(RealGrader):
    """RealGrader over Amazon Bedrock's Converse API, for self-hosting inside
    AWS with any Bedrock model (Claude, Nova, Llama, Mistral...).

    Requests are SigV4-signed with the standard AWS credential chain (env vars,
    shared config/profile, ECS task role, EC2 instance profile, IRSA), or sent
    with a Bedrock API key as a bearer token when one is given. Converse has no
    structured-output mode that every model honours, so a JSON reply is asked
    for in the system prompt and parsed as defensively as the Anthropic one.
    """

    def __init__(
        self,
        model_id: str,
        region: str,
        *,
        bearer_token: str | None = None,
        max_tokens: int = DEFAULT_BEDROCK_MAX_TOKENS,
        endpoint_url: str | None = None,
        timeout: float = 120.0,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        meaniemode: bool = False,
        credentials_provider: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__(
            api_key="", timeout=timeout, client_factory=client_factory, meaniemode=meaniemode
        )
        self._model = model_id
        self._region = region
        self._bearer_token = bearer_token or None
        self._max_tokens = max_tokens
        base = (endpoint_url or f"https://bedrock-runtime.{region}.amazonaws.com").rstrip("/")
        # A model ID can be an ARN, so escape its '/' and ':' too.
        self._url = f"{base}/model/{urllib.parse.quote(model_id, safe='')}/converse"
        self._credentials_provider = credentials_provider or _default_aws_credentials
        self._credentials: Any = None

    async def _call(
        self,
        client: httpx.AsyncClient,
        *,
        system: str,
        user_content: str,
        output_schema: dict | None = None,
    ) -> dict:
        if output_schema is not None:
            system = (
                f"{system}\n\nRespond with only a JSON object, with no prose before or "
                "after it and no code fences, that matches this JSON Schema:\n"
                f"{json.dumps(output_schema)}"
            )
        body = {
            "system": [{"text": system}],
            "messages": [{"role": "user", "content": [{"text": user_content}]}],
            "inferenceConfig": {"maxTokens": self._max_tokens},
        }
        content = json.dumps(body).encode()
        headers = await self._auth_headers(content)
        data = await _post_with_retries(client, self._url, headers, content)
        return self._to_messages_shape(data)

    async def _auth_headers(self, content: bytes) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self._bearer_token:
            headers["authorization"] = f"Bearer {self._bearer_token}"
            return headers

        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        # Resolving can hit the instance metadata service, and a refresh can
        # too, so neither runs on the event loop.
        if self._credentials is None:
            self._credentials = await asyncio.to_thread(self._credentials_provider)
        if self._credentials is None:
            raise GradingError(
                "no AWS credentials found for Bedrock: set AWS_ACCESS_KEY_ID/"
                "AWS_SECRET_ACCESS_KEY, AWS_PROFILE, an instance or task role, "
                "or AWS_BEARER_TOKEN_BEDROCK"
            )
        frozen = await asyncio.to_thread(self._credentials.get_frozen_credentials)
        request = AWSRequest(method="POST", url=self._url, data=content, headers=headers)
        SigV4Auth(frozen, "bedrock", self._region).add_auth(request)
        return dict(request.headers.items())

    def _to_messages_shape(self, data: dict) -> dict:
        """Reshape a Converse reply into the Messages API shape the shared
        parsing expects. Reasoning models put reasoningContent blocks before
        the text, so take the text blocks only."""
        stop_reason = data.get("stopReason")
        if stop_reason in ("guardrail_intervened", "content_filtered"):
            raise GradingError(f"model declined to respond ({stop_reason})")
        if stop_reason == "max_tokens":
            raise GradingError(f"model hit the {self._max_tokens}-token cap before finishing")
        blocks = (data.get("output") or {}).get("message", {}).get("content", [])
        texts = [
            b["text"] for b in blocks if isinstance(b, dict) and isinstance(b.get("text"), str)
        ]
        if not texts:
            raise GradingError(f"model response contained no text block: {data!r}")
        return {"content": [{"type": "text", "text": "".join(texts)}], "stop_reason": stop_reason}


def _default_aws_credentials() -> Any:
    """The standard AWS credential chain, as the AWS CLI and SDKs resolve it."""
    import botocore.session

    return botocore.session.get_session().get_credentials()


async def _post_with_retries(
    client: httpx.AsyncClient, url: str, headers: dict[str, str], content: bytes
) -> dict:
    """POST content, retrying a transient 429/5xx; grading runs inside the
    developer's request. content is pre-serialised so a signature over it holds."""
    last_exc: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            response = await client.post(url, headers=headers, content=content)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in _RETRYABLE_STATUS:
                raise GradingError(
                    f"model API returned {exc.response.status_code}: {exc.response.text[:500]}"
                ) from exc
            last_exc = exc
            delay = _retry_delay(exc.response, attempt)
        except httpx.TransportError as exc:
            last_exc = exc
            delay = _backoff_delay(attempt)

        if attempt == _MAX_ATTEMPTS - 1:
            break
        await asyncio.sleep(delay)

    raise GradingError(
        f"model API call failed after {_MAX_ATTEMPTS} attempts: {last_exc}"
    ) from last_exc


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff: 1s, then 2s. `attempt` is 0-based."""
    return _BACKOFF_BASE_SECONDS * (2**attempt)


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    """Honour Retry-After (delta-seconds only), capped at _MAX_RETRY_AFTER_SECONDS."""
    raw = response.headers.get("retry-after")
    if raw is not None:
        try:
            return min(max(float(raw), 0.0), _MAX_RETRY_AFTER_SECONDS)
        except ValueError:
            pass
    return _backoff_delay(attempt)


def _check_stop_reason(data: dict) -> None:
    if data.get("stop_reason") == "refusal":
        raise GradingError("model declined to respond (safety refusal)")
    # Name the token cap; a truncated reply would otherwise look like bad output.
    if data.get("stop_reason") == "max_tokens":
        raise GradingError(f"model hit the {_MAX_TOKENS}-token cap before finishing")


def _extract_text(data: dict) -> str:
    for block in data.get("content", []):
        if block.get("type") == "text":
            return block.get("text", "")
    raise GradingError(f"model response contained no text block: {data!r}")


def _exam_user_content(diff: str, avoid: list[str] | None) -> str:
    content = f"Numbered diff:\n\n{_number_diff(diff)}"
    if avoid:
        asked = "\n".join(f"- {q}" for q in avoid)
        content += (
            "\n\nThe developer has already seen these questions, and their answers were "
            "revealed. Do not repeat them: ask about other parts of the change, or about "
            f"the same parts from a different angle.\n{asked}"
        )
    return content


def _number_diff(diff: str) -> str:
    """Prefix each line with its 1-based index so the model can anchor to ranges."""
    return "\n".join(f"{n}\t{line}" for n, line in enumerate(diff.splitlines(), start=1))


def _clamp_anchor(start: Any, end: Any, line_count: int) -> tuple[int | None, int | None]:
    """A usable (start, end) within 1..line_count, or (None, None) when the
    anchor can't be salvaged. bool is an int subclass, so reject it first."""
    if isinstance(start, bool) or isinstance(end, bool):
        return None, None
    if not (isinstance(start, int) and isinstance(end, int)):
        return None, None
    if start > end:
        start, end = end, start
    start, end = max(start, 1), min(end, line_count)
    if start > end:  # entirely outside the diff
        return None, None
    return start, end


def _extract_json_object(text: str) -> dict:
    """Strip code fences, take the outermost {...}, and parse it as a JSON object."""
    cleaned = _FENCE_RE.sub("", text).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise GradingError(f"could not find a JSON object in model output: {text!r}")
    try:
        parsed = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise GradingError(f"model output was not valid JSON: {text!r}") from exc
    if not isinstance(parsed, dict):
        raise GradingError(f"model output was not a JSON object: {parsed!r}")
    return parsed


def _parse_questions(text: str, line_count: int) -> list[ExamQuestion]:
    """Scoped questions with clamped anchors. Entries without a question are
    dropped; a bad anchor only loses the slice."""
    parsed = _extract_json_object(text)
    raw = parsed.get("questions")
    if not isinstance(raw, list):
        raise GradingError(f"model output missing a 'questions' list: {parsed!r}")
    out: list[ExamQuestion] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        question = item.get("question")
        if not isinstance(question, str) or not question.strip():
            continue
        start, end = _clamp_anchor(item.get("start_line"), item.get("end_line"), line_count)
        reference = item.get("reference_answer")
        out.append(
            ExamQuestion(
                question=question.strip(),
                start_line=start,
                end_line=end,
                reference_answer=reference.strip() if isinstance(reference, str) else "",
            )
        )
    return out


def _parse_mark(text: str) -> dict:
    """{"passed": bool, "note": str}; raises on a missing or mistyped field."""
    parsed = _extract_json_object(text)
    if not isinstance(parsed.get("passed"), bool):
        raise GradingError(f"model output missing boolean 'passed': {parsed!r}")
    if not isinstance(parsed.get("note"), str):
        raise GradingError(f"model output missing string 'note': {parsed!r}")
    return {"passed": parsed["passed"], "note": parsed["note"].strip()}
