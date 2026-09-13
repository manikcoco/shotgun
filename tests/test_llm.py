"""Provider selection and the shared structured-output call.

The providers differ in ways that matter: Anthropic takes `output_format` and
explicit `cache_control` breakpoints, OpenAI takes `text_format` and an
automatic cache keyed by `prompt_cache_key`. `parse_structured()` hides that,
and these tests pin the dispatch and the caching contract without making a
network call.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from shotgun import config, llm


class Verdict(BaseModel):
    score: int


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Env.load() reads .env, which pins keys and SHOTGUN_MODEL. Isolate."""
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                "SHOTGUN_PROVIDER", "SHOTGUN_MODEL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(config, "_load_dotenv", lambda *a, **k: None)
    llm.env.cache_clear()
    llm.model.cache_clear()
    yield
    llm.env.cache_clear()
    llm.model.cache_clear()


# --------------------------------------------------- provider selection

def test_anthropic_key_alone_selects_anthropic(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    env = config.Env.load()
    assert env.provider == "anthropic"
    assert env.model == "claude-opus-5"


def test_openai_key_alone_selects_openai(monkeypatch) -> None:
    """Dropping an OPENAI_API_KEY into .env should be enough to switch."""
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    env = config.Env.load()
    assert env.provider == "openai"
    assert env.model == "gpt-5"


def test_anthropic_wins_when_both_keys_are_present(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    assert config.Env.load().provider == "anthropic"


def test_explicit_provider_overrides_the_keys(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    monkeypatch.setenv("SHOTGUN_PROVIDER", "openai")
    assert config.Env.load().provider == "openai"


def test_an_unknown_provider_falls_back_rather_than_crashing(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.setenv("SHOTGUN_PROVIDER", "wat")
    assert config.Env.load().provider == "anthropic"


# ------------------------------------------------------ model / provider

@pytest.mark.parametrize(
    ("model", "expected"),
    [("claude-opus-5", "anthropic"), ("gpt-5", "openai"),
     ("gpt-5-mini", "openai"), ("o3", "openai"), ("custom-deploy", None)],
)
def test_model_provider_detection(model: str, expected) -> None:
    assert config._model_provider(model) == expected


def test_a_leftover_model_name_is_discarded_on_switch(monkeypatch) -> None:
    """.env pins SHOTGUN_MODEL, so switching provider would otherwise send
    "claude-opus-5" to OpenAI and fail as an unknown model."""
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    monkeypatch.setenv("SHOTGUN_PROVIDER", "openai")
    monkeypatch.setenv("SHOTGUN_MODEL", "claude-opus-5")
    assert config.Env.load().model == "gpt-5"


def test_a_matching_model_name_is_kept(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    monkeypatch.setenv("SHOTGUN_PROVIDER", "openai")
    monkeypatch.setenv("SHOTGUN_MODEL", "gpt-5-mini")
    assert config.Env.load().model == "gpt-5-mini"


def test_an_unrecognised_model_name_is_left_alone(monkeypatch) -> None:
    """A custom deployment name is the user's call, not ours to override."""
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    monkeypatch.setenv("SHOTGUN_PROVIDER", "openai")
    monkeypatch.setenv("SHOTGUN_MODEL", "my-deploy")
    assert config.Env.load().model == "my-deploy"


# --------------------------------------------------------- cache contract

def test_the_profile_block_is_the_cacheable_prefix() -> None:
    blocks = llm.system_blocks("profile-yaml", "instructions")
    assert "profile-yaml" in blocks[0]["text"]
    assert blocks[0]["cache_control"]["ttl"] == "1h"


def test_the_last_instruction_block_is_also_cached() -> None:
    """A scoring run makes one call per posting, so leaving the task prompt
    uncached re-billed it every time."""
    blocks = llm.system_blocks("p", "first", "second")
    assert "cache_control" not in blocks[1]
    assert "cache_control" in blocks[2]


def test_empty_instructions_are_dropped() -> None:
    assert len(llm.system_blocks("p", "only", "")) == 2


def test_profile_block_carries_nothing_volatile() -> None:
    """Anything per-call in here invalidates the prefix for every later block
    and silently doubles the bill."""
    first = llm.profile_block("same bytes")
    second = llm.profile_block("same bytes")
    assert first == second


# -------------------------------------------------------------- dispatch

def test_parse_structured_routes_to_the_configured_provider(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    monkeypatch.setenv("SHOTGUN_PROVIDER", "openai")
    llm.env.cache_clear()

    called = {}

    def fake(system, user, output_format, max_tokens, effort, label):
        called.update(system=system, user=user, label=label, effort=effort)
        return Verdict(score=7)

    monkeypatch.setattr(llm, "_parse_openai", fake)
    monkeypatch.setattr(llm, "_parse_anthropic",
                        lambda *a: pytest.fail("wrong provider"))

    got = llm.parse_structured(
        ["profile", "instructions"], "posting", Verdict,
        max_tokens=100, label="score", effort="low",
    )
    assert got.score == 7
    assert called["system"] == ["profile", "instructions"]
    assert called["label"] == "score"


def test_parse_structured_defaults_to_anthropic(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    llm.env.cache_clear()
    monkeypatch.setattr(llm, "_parse_anthropic", lambda *a: Verdict(score=1))
    monkeypatch.setattr(llm, "_parse_openai",
                        lambda *a: pytest.fail("wrong provider"))
    assert llm.parse_structured(["p"], "u", Verdict,
                                max_tokens=10, label="x").score == 1


# ------------------------------------------------------- retry decisions

class FakeStatusError(Exception):
    def __init__(self, status_code):
        self.status_code = status_code


def test_rate_limits_and_server_errors_are_retryable() -> None:
    for name in ("RateLimitError", "APIConnectionError", "APITimeoutError",
                 "InternalServerError"):
        exc = type(name, (Exception,), {})()
        assert llm.is_retryable(exc), name


def test_truncation_is_retryable() -> None:
    assert llm.is_retryable(llm.EmptyResponse("hit the cap"))


def test_a_client_error_is_not_retryable() -> None:
    """A 400 will fail identically next run; leaving it unscored forever is
    worse than recording the failure."""
    exc = FakeStatusError(400)
    exc.__class__.__name__ = "APIStatusError"
    assert llm.is_retryable(exc) is False


def test_a_server_status_error_is_retryable() -> None:
    exc = FakeStatusError(503)
    exc.__class__.__name__ = "APIStatusError"
    assert llm.is_retryable(exc) is True


def test_an_unrelated_exception_is_not_retryable() -> None:
    assert llm.is_retryable(ValueError("bug in our code")) is False


# --------------------------------------------------- usage across providers

def usage_anthropic(**over):
    from types import SimpleNamespace
    return SimpleNamespace(**({
        "input_tokens": 136, "cache_creation_input_tokens": 10153,
        "cache_read_input_tokens": 0, "output_tokens": 502,
    } | over))


def usage_openai(**details):
    from types import SimpleNamespace
    return SimpleNamespace(
        input_tokens=136, output_tokens=502,
        input_tokens_details=SimpleNamespace(**({
            "cached_tokens": 9000, "cache_write_tokens": 1153,
        } | details)),
    )


def test_a_cache_read_of_zero_is_logged_as_zero(caplog) -> None:
    """The bug this guards, and it hid the one number that matters. The
    counters were read with `or`, so a genuine 0 was falsy and fell through
    to "?" — while the docstring's whole promise is that a cache_read stuck
    at 0 tells you the prefix is being invalidated.
    """
    from shotgun.llm import log_usage

    with caplog.at_level("DEBUG"):
        log_usage("score", usage_anthropic())
    assert "cache_read=0" in caplog.text
    assert "cache_read=?" not in caplog.text


def test_both_cache_halves_are_read_on_openai(caplog) -> None:
    """OpenAI reports both under input_tokens_details, not at the top level."""
    from shotgun.llm import log_usage

    with caplog.at_level("DEBUG"):
        log_usage("score", usage_openai())
    assert "cache_write=1153" in caplog.text
    assert "cache_read=9000" in caplog.text


def test_a_counter_no_provider_reported_is_unknown(caplog) -> None:
    """Distinct from zero: "?" means nobody said, 0 means nobody cached."""
    from types import SimpleNamespace

    from shotgun.llm import log_usage

    with caplog.at_level("DEBUG"):
        log_usage("score", SimpleNamespace(input_tokens=10, output_tokens=20))
    assert "cache_read=?" in caplog.text
    assert "cache_write=?" in caplog.text


# ------------------------------------------------- a spent account vs a busy one

class RateLimitError(Exception):
    """Stands in for the real thing. Named exactly, because is_retryable
    dispatches on the exception's class name so the handler chain does not
    have to import either provider's SDK."""
    def __init__(self, message, code=None, body=None):
        super().__init__(message)
        self.status_code = 429
        self.code = code
        self.body = body


def test_no_credit_is_not_retryable() -> None:
    """Both arrive as HTTP 429, and the difference matters: a rate limit
    clears itself, an empty balance does not. Observed live on the first
    OpenAI call — treating it as retryable means 441 requests that cannot
    succeed before the run says anything useful."""
    from shotgun.llm import is_out_of_credit, is_retryable

    exc = RateLimitError(
        "Error code: 429 - {'error': {'message': 'You have no credits "
        "remaining. Add credits to continue using the API at ...', "
        "'type': 'insufficient_quota', 'code': 'credit_balance_exhausted'}}",
        code="credit_balance_exhausted",
    )
    assert is_out_of_credit(exc)
    assert not is_retryable(exc)


def test_anthropics_wording_is_caught_too() -> None:
    from shotgun.llm import is_out_of_credit

    assert is_out_of_credit(Exception(
        "Your credit balance is too low to access the Anthropic API"))


def test_a_real_rate_limit_is_still_retryable() -> None:
    """The whole point of matching on the provider's markers rather than on
    429: a busy account has to keep being retried."""
    from shotgun.llm import is_out_of_credit, is_retryable

    exc = RateLimitError("Error code: 429 - rate_limit_error: slow down")
    assert not is_out_of_credit(exc)
    assert is_retryable(exc)


def test_unrelated_failures_are_not_mistaken_for_a_spent_account() -> None:
    from shotgun.llm import is_out_of_credit

    for exc in (ValueError("bad json"),
                Exception("400 invalid_request_error: unknown model"),
                Exception("connection reset")):
        assert not is_out_of_credit(exc)
