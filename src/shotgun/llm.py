"""Model plumbing, behind one provider-agnostic call.

Everything that needs a model wants the same thing: some stable system text, a
per-job user message, and a Pydantic type back. `parse_structured()` is that,
and each provider implements it its own way.

The two differ in ways that matter here:

* **Structured output.** Anthropic takes `output_format=Model` on
  `messages.parse`; OpenAI takes `text_format=Model` on `responses.parse` and
  returns `.output_parsed`. Both validate against the schema.
* **Caching.** The candidate profile is resent on every call, so it dominates
  input cost. Anthropic needs explicit `cache_control` breakpoints; OpenAI
  caches automatically and only wants a stable `prompt_cache_key`. That is why
  the system prompt is passed as a *list* of blocks rather than one string —
  the Anthropic path needs the seams, the OpenAI path joins them.
* **Truncation.** Anthropic signals it with `stop_reason`, OpenAI with
  `status`/`incomplete_details`. Either way the parsed value comes back None,
  which is what `EmptyResponse` exists to catch.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import TypeVar

from pydantic import BaseModel

from .config import Env

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class MissingCredentials(RuntimeError):
    pass


class EmptyResponse(RuntimeError):
    """The model returned nothing parsable against the requested schema."""


# Cache key for OpenAI's automatic prefix caching. Must be stable across a
# run and must not embed anything per-job, for the same reason the Anthropic
# cache block must not: it is the prefix identity.
_CACHE_KEY = "shotgun-profile-v1"


@lru_cache(maxsize=1)
def env() -> Env:
    return Env.load()


def provider() -> str:
    return env().provider


@lru_cache(maxsize=1)
def model() -> str:
    return env().model


# ----------------------------------------------------------- anthropic

@lru_cache(maxsize=1)
def client():
    """The Anthropic client. Kept for callers that want it directly."""
    import anthropic

    settings = env()
    if not settings.anthropic_api_key:
        # A bare Anthropic() still works off an `ant auth login` profile, so
        # try it before giving up.
        try:
            return anthropic.Anthropic()
        except Exception as exc:
            raise MissingCredentials(
                "No Anthropic credentials. Set ANTHROPIC_API_KEY in .env, "
                "or run `ant auth login`. To use OpenAI instead, set "
                "OPENAI_API_KEY and SHOTGUN_PROVIDER=openai."
            ) from exc
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def profile_block(profile_yaml: str) -> dict:
    """A cacheable system block holding the candidate profile.

    Must contain nothing volatile — no timestamps, no per-job text, no run IDs.
    Anything that changes between calls invalidates the cache for every
    subsequent block and silently doubles the bill.
    """
    return {
        "type": "text",
        "text": f"<candidate_profile>\n{profile_yaml}\n</candidate_profile>",
        "cache_control": {"type": "ephemeral", "ttl": "1h"},
    }


def system_blocks(profile_yaml: str, *instructions: str) -> list[dict]:
    """Profile plus task instructions, with two cache breakpoints.

    The first covers the profile alone, which is byte-identical across scoring,
    tailoring and cover letters, so all three share it. The second covers the
    task instructions too — those are stable for a whole run, and a scoring run
    makes one call per posting, so leaving them uncached re-billed them every
    time. Four breakpoints are allowed per request; this uses two.
    """
    blocks: list[dict] = [profile_block(profile_yaml)]
    texts = [t for t in instructions if t]
    for index, text in enumerate(texts):
        block: dict = {"type": "text", "text": text}
        if index == len(texts) - 1:
            block["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
        blocks.append(block)
    return blocks


def _parse_anthropic(
    system: list[str], user: str, output_format: type[T],
    max_tokens: int, effort: str | None, label: str,
) -> T:
    kwargs: dict = {}
    if effort:
        kwargs["output_config"] = {"effort": effort}

    profile_yaml, *rest = system
    response = client().messages.parse(
        model=model(),
        max_tokens=max_tokens,
        system=system_blocks(profile_yaml, *rest),
        messages=[{"role": "user", "content": user}],
        output_format=output_format,
        **kwargs,
    )
    log_usage(label, response.usage)

    if response.parsed_output is None:
        raise EmptyResponse(
            f"{label}: no structured output (stop_reason="
            f"{response.stop_reason!r}). If it is 'max_tokens', raise "
            "max_tokens or lower effort."
        )
    return response.parsed_output


# -------------------------------------------------------------- openai

@lru_cache(maxsize=1)
def openai_client():
    import openai

    settings = env()
    if not settings.openai_api_key:
        raise MissingCredentials(
            "No OpenAI credentials. Set OPENAI_API_KEY in .env, or switch back "
            "with SHOTGUN_PROVIDER=anthropic."
        )
    return openai.OpenAI(api_key=settings.openai_api_key)


def _parse_openai(
    system: list[str], user: str, output_format: type[T],
    max_tokens: int, effort: str | None, label: str,
) -> T:
    kwargs: dict = {}
    if effort:
        # OpenAI's knob is reasoning effort; "xhigh"/"max" have no equivalent.
        kwargs["reasoning"] = {"effort": "low" if effort == "low" else "medium"}

    response = openai_client().responses.parse(
        model=model(),
        # Joined, because OpenAI has no per-block cache breakpoints. The order
        # still matters: profile first keeps the cacheable prefix identical
        # across scoring, tailoring and cover letters.
        instructions="\n\n".join(t for t in system if t),
        input=user,
        text_format=output_format,
        max_output_tokens=max_tokens,
        prompt_cache_key=_CACHE_KEY,
        **kwargs,
    )
    log_usage(label, response.usage)

    parsed = response.output_parsed
    if parsed is None:
        detail = getattr(response, "incomplete_details", None)
        raise EmptyResponse(
            f"{label}: no structured output (status={response.status!r}, "
            f"incomplete={detail!r}). If it is a length limit, raise "
            "max_tokens or lower effort."
        )
    return parsed


# ------------------------------------------------------------ dispatch

def parse_structured(
    system: list[str],
    user: str,
    output_format: type[T],
    *,
    max_tokens: int,
    label: str,
    effort: str | None = None,
) -> T:
    """One structured-output call, on whichever provider is configured.

    `system` is a list so the Anthropic path can place cache breakpoints
    between the blocks; the first element must be the candidate profile,
    because that is the shared cacheable prefix.
    """
    fn = _parse_openai if provider() == "openai" else _parse_anthropic
    return fn(system, user, output_format, max_tokens, effort, label)


def is_out_of_credit(exc: BaseException) -> bool:
    """Is this a spent account rather than a busy one?

    Both arrive as HTTP 429, which is why this is worth separating. A rate
    limit clears on its own; an empty balance does not, and waiting does not
    help. Observed on the first live OpenAI call here:

        429 insufficient_quota / credit_balance_exhausted
        "You have no credits remaining."

    Treated as retryable it is close to the worst case — a run over 441
    postings makes 441 requests that cannot succeed, logs 441 warnings and
    takes as long as the real thing before saying anything useful. The caller
    should stop the run and say so once.

    Matched on the provider's own markers rather than the status code, so a
    genuine rate limit keeps being retried. Anthropic's equivalent is a 400
    `credit balance is too low`, which also lands here.
    """
    haystack = " ".join(filter(None, [
        str(exc),
        str(getattr(exc, "code", "") or ""),
        str(getattr(getattr(exc, "body", None), "get", lambda *_: "")("type") or ""),
    ])).lower()
    return any(marker in haystack for marker in (
        "insufficient_quota",
        "credit_balance_exhausted",
        "no credits remaining",
        "credit balance is too low",
        "billing_not_active",
        "exceeded your current quota",
    ))


def is_retryable(exc: BaseException) -> bool:
    """Would this failure plausibly succeed on a later run?

    Rate limits, connection drops and truncation are worth leaving unscored
    and retrying. A 400 is not. Kept here rather than in score.py so the
    handler chain does not have to know which provider raised.
    """
    if is_out_of_credit(exc):
        return False
    name = type(exc).__name__
    if name in {
        "RateLimitError", "APIConnectionError", "APITimeoutError",
        "InternalServerError", "APIStatusError",
    }:
        status = getattr(exc, "status_code", None)
        if name == "APIStatusError" and isinstance(status, int):
            return status == 429 or status >= 500
        return True
    return isinstance(exc, EmptyResponse)


def _tokens(usage, anthropic_field: str, openai_field: str):
    """One usage counter, from whichever provider's field name carries it.

    Anthropic puts the cache counters at the top level of `usage`; OpenAI
    puts both halves under `input_tokens_details`. Falls back to "?" only
    when neither provider reported the number at all.

    Explicitly None-checked rather than written with `or`, which is not a
    nitpick: a genuine count of 0 is falsy, so `or` fell through and logged
    "?" instead. Zero is the single value this function exists to surface —
    "if the cache-read counter stays 0, the prefix is being invalidated" —
    and it was the one value being hidden.
    """
    value = getattr(usage, anthropic_field, None)
    if value is not None:
        return value
    value = getattr(getattr(usage, "input_tokens_details", None), openai_field, None)
    return value if value is not None else "?"


def log_usage(label: str, usage) -> None:
    """Surface cache effectiveness. If the cache-read counter stays 0 across a
    run, the profile prefix is being invalidated somewhere."""
    log.debug(
        "%s: in=%s cache_write=%s cache_read=%s out=%s",
        label,
        getattr(usage, "input_tokens", "?"),
        _tokens(usage, "cache_creation_input_tokens", "cache_write_tokens"),
        _tokens(usage, "cache_read_input_tokens", "cached_tokens"),
        getattr(usage, "output_tokens", "?"),
    )
