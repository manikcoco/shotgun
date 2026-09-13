"""The title gate, and the any-level escape hatch from it.

The `include` patterns all require a level word or a role shape — "security
... engineer", "head of ... security". Measured against the boards of a dozen
remote-first companies that keeps 31 of 43 security openings and throws away
the other 12, all of them real: "Senior Privacy Engineer", "Principal Security
Researcher", "Detection Engineer". That trade is right across a 6,000-posting
sweep and wrong for a short list of companies you chose on purpose, which is
what `titles.any_level` exists for.
"""

from __future__ import annotations

from shotgun.config import Preferences, matching_companies

PREFS = Preferences(raw={
    "titles": {
        "include": [
            r"\b(security|appsec)\b.*\bengineer\b",
            r"\b(engineering manager|manager|head of|director)\b.*\b(security|appsec)\b",
        ],
        "exclude": [
            r"\b(intern|internship|graduate|new grad)\b",
            r"\bproduct manager\b",
            r"\b(legal counsel|counsel)\b",
        ],
        "any_level": {
            "companies": ["canonical", "duck-duck-go", "duckduckgo"],
            "include": [
                r"\b(security|appsec|infosec|cyber)\b",
                r"\b(privacy|cryptograph)\b",
                r"\b(detection|threat|vulnerabilit|bug bounty)\b",
            ],
        },
    },
})


# ------------------------------------------------------- company matching

def test_matching_is_on_word_boundaries() -> None:
    assert matching_companies("Amazon Web Services (AWS)", ["amazon", "aws"]) \
        == ["amazon", "aws"]
    assert matching_companies("Metabase", ["meta"]) == []
    assert matching_companies("Googler Inc", ["google"]) == []


def test_matching_survives_a_missing_company() -> None:
    assert matching_companies(None, ["canonical"]) == []
    assert matching_companies("", ["canonical"]) == []


def test_board_token_and_display_name_both_match() -> None:
    """Ashby reports the board token, Greenhouse the display name, so both
    spellings have to be listed — and both have to work."""
    assert PREFS.any_level("duck-duck-go")
    assert PREFS.any_level("DuckDuckGo")
    assert not PREFS.any_level("Some Other Co")


# ------------------------------------------------------------ the gate

def test_the_role_that_prompted_this_already_passed() -> None:
    """Worth locking down, because it is the thing easily got wrong: the
    level gate was never what dropped this title."""
    ok, _ = PREFS.title_matches("Senior Web Security Engineer, Browser Platform")
    assert ok


def test_level_words_are_not_what_the_gate_turns_on() -> None:
    for title in ("Staff Security Engineer", "Senior Security Engineer",
                  "Principal Security Engineer", "Security Engineer"):
        assert PREFS.title_matches(title)[0], title


def test_security_work_without_the_word_engineer_is_dropped_by_default() -> None:
    for title in ("Senior Privacy Engineer", "Principal Security Researcher",
                  "Detection Engineer", "Security Operations Analyst"):
        ok, why = PREFS.title_matches(title, "Some Other Co")
        assert not ok, title
        assert why == "title matched no include pattern"


def test_an_any_level_company_keeps_them() -> None:
    for title in ("Senior Privacy Engineer", "Principal Security Researcher",
                  "Detection Engineer", "Threat Intelligence Lead"):
        ok, why = PREFS.title_matches(title, "canonical")
        assert ok, title
        assert why.startswith("any level;")


def test_the_flag_widens_it_for_any_company() -> None:
    """What `sweep --any-level` uses. The question a hand-picked list of
    companies asks is what they have open, not what a big sweep should keep."""
    assert not PREFS.title_matches("Detection Engineer", "Fastly")[0]
    assert PREFS.title_matches("Detection Engineer", "Fastly", any_level=True)[0]


def test_exclusions_are_never_relaxed() -> None:
    """The point of the escape hatch is level and role shape, not wanting the
    job badly enough to apply for an internship."""
    for title in ("Security Engineering Intern",
                  "Product Manager - Security",
                  "Legal Counsel - Regulatory Compliance, Product and Privacy"):
        for company in ("canonical", "Some Other Co"):
            ok, why = PREFS.title_matches(title, company, any_level=True)
            assert not ok, (title, company)
            assert why.startswith("title excluded by")


def test_a_non_security_role_at_an_any_level_company_is_still_dropped() -> None:
    """Being on the list widens what counts as security, not what counts as
    relevant — Canonical advertises 303 postings and 10 of them are the point."""
    ok, why = PREFS.title_matches("Senior Backend Engineer", "canonical")
    assert not ok
    assert why == "any-level company, but title is not a security role"


def test_no_any_level_config_leaves_the_gate_alone() -> None:
    prefs = Preferences(raw={"titles": {"include": [r"\bsecurity\b.*\bengineer\b"]}})
    assert prefs.any_level_companies == []
    assert not prefs.any_level("canonical")
    ok, why = prefs.title_matches("Detection Engineer", "canonical", any_level=True)
    assert not ok
    assert why == "any-level company, but title is not a security role"
