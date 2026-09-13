"""The reusable answer store.

Context: the `answers` table shipped in the first commit and nothing ever
wrote to it, so `pipeline._stored_answers()` returned `{}` forever and every
application form came back with the same dozen questions blank.
"""

from __future__ import annotations

import pytest
import yaml

from shotgun import answers as answers_mod
from shotgun.profile import Profile

PROFILE_YAML = """
contact:
  name: Test Person
  email: t@example.com
  location: Berlin, Germany
  linkedin: linkedin.com/in/test
  github: github.com/test
roles:
  - company: Acme Corp
    title: Senior Cloud Security Engineer
    start: '2023-04'
    bullets: ['Did a thing.']
  - company: Nucleus
    title: Associate Consultant
    start: '2017-03'
    end: '2020-03'
    bullets: ['Did an older thing.']
skills: [Kubernetes]
languages: ['English — Professional', 'German — A2']
work_authorization:
  IN: citizen
  DE: work permit — unrestricted right to work in Germany
  EU: intra-EU mobility — local permit required but no full visa sponsorship
  GB: needs sponsorship (Skilled Worker visa)
  US: needs sponsorship
salary_expectation:
  EUR: '130000'
notice_period: 3 months
"""


@pytest.fixture
def profile() -> Profile:
    return Profile.model_validate(yaml.safe_load(PROFILE_YAML))


# ------------------------------------------------------ selector safety

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("notice_period", "notice_period"),
        ("Notice Period", "noticeperiod"),
        ("salary", "salary"),
        ("work-authorization", "workauthorization"),
        # The injection case: fillers build `input[name*='{key}' i]`.
        ("x' i],[name*='password", "xinamepassword"),
        ('a"b', "ab"),
        ("", ""),
        ("!!!", ""),
    ],
)
def test_selector_key_sanitises(raw: str, expected: str) -> None:
    assert answers_mod.selector_key(raw) == expected


def test_selector_key_output_is_always_selector_safe() -> None:
    for raw in ("a'b", 'c"d', "e]f", "g[h", "i j", "K/L"):
        assert not set(answers_mod.selector_key(raw)) & set("'\"[] /")


# ------------------------------------------------- sponsorship reading

@pytest.mark.parametrize(
    ("status", "needs"),
    [
        ("citizen", False),
        ("work permit holder — unrestricted right to work in Germany", False),
        # The trap: this says sponsorship is NOT needed, but contains
        # "sponsorship". A substring test read it backwards.
        ("local permit required but no full visa sponsorship", False),
        ("permanent resident", False),
        ("needs sponsorship (Skilled Worker visa)", True),
        ("needs sponsorship", True),
        ("requires visa sponsorship", True),
    ],
)
def test_needs_sponsorship(status: str, needs: bool) -> None:
    assert answers_mod._needs_sponsorship(status) is needs


# ------------------------------------------------------------ seeding

def test_suggest_reads_the_profile(profile: Profile) -> None:
    s = answers_mod.suggest(profile)
    assert s["notice_period"] == "3 months"
    assert s["salary"] == "EUR 130000"
    assert s["location"] == "Berlin, Germany"
    assert s["linkedin"] == "linkedin.com/in/test"
    assert s["current_company"] == "Acme Corp"
    assert s["current_title"] == "Senior Cloud Security Engineer"
    assert "German — A2" in s["languages"]


def test_suggest_splits_authorised_from_sponsorship_needed(profile: Profile) -> None:
    s = answers_mod.suggest(profile)
    assert "DE" in s["work_authorization"]
    assert "EU" in s["work_authorization"], "intra-EU mobility is not sponsorship"
    assert "IN" in s["work_authorization"]
    assert "GB" in s["sponsorship"]
    assert "US" in s["sponsorship"]
    assert "DE" not in s["sponsorship"]


def test_years_of_experience_comes_from_the_earliest_role(profile: Profile) -> None:
    from datetime import date
    expected = str(date.today().year - 2017)
    assert answers_mod.suggest(profile)["years_experience"] == expected


def test_suggest_on_an_empty_profile_is_empty_not_an_error() -> None:
    bare = Profile.model_validate(
        {"contact": {"name": "X", "email": "x@example.com"}, "roles": []}
    )
    assert answers_mod.suggest(bare) == {}


# ------------------------------------------------------------- gaps

def test_gaps_lists_unanswered_catalogue_entries() -> None:
    assert len(answers_mod.gaps({})) == len(answers_mod.CATALOGUE)
    partial = {"notice_period": "3 months", "salary": "EUR 130000"}
    keys = {q.key for q in answers_mod.gaps(partial)}
    assert "notice_period" not in keys
    assert "salary" not in keys
    assert "sponsorship" in keys


def test_blank_values_still_count_as_gaps() -> None:
    keys = {q.key for q in answers_mod.gaps({"notice_period": "   "})}
    assert "notice_period" in keys


def test_salary_is_sensitive_by_default() -> None:
    """So it is recorded but never typed into a third-party form."""
    assert answers_mod.BY_KEY["salary"].sensitive is True
    assert answers_mod.BY_KEY["notice_period"].sensitive is False


# -------------------------------------------- matching form questions

@pytest.mark.parametrize(
    ("question", "expected_key"),
    [
        ("Do you require sponsorship to work in the UK?", "sponsorship"),
        ("Are you legally authorised to work in Spain?", "work_authorization"),
        ("What is your notice period?", "notice_period"),
        ("Expected salary", "salary"),
        ("Are you willing to relocate?", "relocation"),
        ("How did you hear about this role?", "referral"),
        ("LinkedIn Profile", "linkedin"),
        ("What are your pronouns?", "pronouns"),
    ],
)
def test_match_for_finds_the_right_answer(question: str, expected_key: str) -> None:
    stored = {q.key: f"value-for-{q.key}" for q in answers_mod.CATALOGUE}
    hit = answers_mod.match_for(question, stored)
    assert hit is not None
    assert hit[0] == expected_key


def test_match_for_returns_none_when_nothing_is_stored() -> None:
    assert answers_mod.match_for("What is your notice period?", {}) is None


def test_match_for_ignores_unrelated_questions() -> None:
    stored = {"notice_period": "3 months"}
    assert answers_mod.match_for("Describe your proudest achievement", stored) is None


# --------------------------------------------- numeric-field safety

@pytest.mark.parametrize(
    ("key", "value"),
    [
        # Measured in a real browser: Playwright raises "Cannot type text into
        # input[type=number]" for all of these, and try_fill swallows it, so
        # the field is left blank with no indication why.
        ("salary", "EUR 135,000 minimum"),
        ("salary", "135,000"),
        ("salary", "135k"),
        ("years_experience", "9 years"),
        ("years_experience", "9+"),
    ],
)
def test_numeric_warning_flags_unfillable_values(key: str, value: str) -> None:
    warning = answers_mod.numeric_warning(key, value)
    assert warning is not None
    assert "silently" in warning


@pytest.mark.parametrize(("key", "value"), [("salary", "120000"),
                                            ("years_experience", "9")])
def test_bare_numbers_pass(key: str, value: str) -> None:
    assert answers_mod.numeric_warning(key, value) is None


def test_numeric_warning_suggests_the_digits() -> None:
    warning = answers_mod.numeric_warning("salary", "EUR 120,000 minimum")
    assert "'120000'" in warning


@pytest.mark.parametrize("key", ["notice_period", "relocation", "location", "pronouns"])
def test_free_text_keys_are_never_flagged(key: str) -> None:
    assert answers_mod.numeric_warning(key, "2 months, negotiable") is None


def test_unknown_keys_are_never_flagged() -> None:
    assert answers_mod.numeric_warning("something_custom", "anything at all") is None


# ----------------------------------------------- deliberately blank

def test_a_skipped_key_is_not_a_gap() -> None:
    """Otherwise `answers list` reports it missing forever and `init` keeps
    asking — 'left blank on purpose' is a different state from 'not yet'."""
    keys = {q.key for q in answers_mod.gaps({"referral": ""}, {"referral"})}
    assert "referral" not in keys


def test_an_unskipped_blank_is_still_a_gap() -> None:
    keys = {q.key for q in answers_mod.gaps({"referral": ""}, set())}
    assert "referral" in keys


def test_skipping_does_not_hide_other_gaps() -> None:
    keys = {q.key for q in answers_mod.gaps({}, {"referral"})}
    assert "referral" not in keys
    assert "salary" in keys
    assert len(keys) == len(answers_mod.CATALOGUE) - 1


def test_gaps_defaults_to_nothing_skipped() -> None:
    assert len(answers_mod.gaps({})) == len(answers_mod.CATALOGUE)


def test_skipped_marker_is_not_a_confidence_that_auto_fills() -> None:
    """pipeline._stored_answers() only selects confidence='confirmed', so the
    marker keeps a skipped answer out of every form by construction."""
    assert answers_mod.SKIPPED != "confirmed"
