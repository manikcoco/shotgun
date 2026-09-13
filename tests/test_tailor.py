"""Fabrication checking, and the schema shape it depends on.

Two bugs are locked down here:

1. `TailoredResume.bullets_by_role` was a `dict[str, list[str]]`. Structured
   outputs compile that to `{"properties": {}, "additionalProperties": false}`
   — a schema admitting only the empty object — so the model could never emit
   a role, every resume silently fell back to untailored profile bullets, and
   the fabrication check's employer half iterated an empty dict forever.

2. The check tested only the skills list, by substring, against a concatenated
   profile blob. That blocked ordinary rephrasings ("gVisor" surfacing as
   "Container isolation (gVisor)") while never looking at bullet text, which is
   where an invented tool or an inflated metric would actually live.
"""

from __future__ import annotations

import pytest
import yaml

from shotgun.models import RoleBullets, TailoredResume
from shotgun.profile import Profile
from shotgun.tailor import verify_no_fabrication

PROFILE_YAML = """
contact: {name: Test Person, email: t@example.com}
summary: Security engineer working on Kubernetes and AWS.
skills: [Kubernetes, AWS, threat modelling, Terraform, gVisor]
certifications: [CISSP]
roles:
  - company: Acme Corp
    title: Staff Security Engineer
    start: '2021'
    bullets:
      - Cut container escape risk 40% by rolling out gVisor across 900 nodes.
      - Built the threat modelling practice adopted by 12 product teams.
      - Migrated IAM policies to Terraform, closing 300 findings.
  - company: Globex
    title: Senior Security Engineer
    start: '2018'
    end: '2021'
    bullets:
      - Automated AWS access review for 50 accounts.
"""


@pytest.fixture
def profile() -> Profile:
    return Profile.model_validate(yaml.safe_load(PROFILE_YAML))


def tailored(bullets: list[str], *, company: str = "Acme Corp",
             skills: list[str] | None = None) -> TailoredResume:
    return TailoredResume(
        headline="Staff Security Engineer",
        summary="Summary.",
        highlighted_skills=skills if skills is not None else ["Kubernetes", "AWS"],
        role_bullets=[RoleBullets(company=company, bullets=bullets)],
    )


# ------------------------------------------------- schema shape regression

def test_role_bullets_compiles_to_a_populatable_schema() -> None:
    """The regression guard. If this ever compiles back to an object with no
    declared properties, the model cannot return a single role and tailoring
    becomes a silent no-op again."""
    transform = pytest.importorskip("anthropic.lib._parse._transform")
    from pydantic import TypeAdapter

    schema = transform.transform_schema(TypeAdapter(TailoredResume).json_schema())
    field = schema["properties"]["role_bullets"]

    assert field["type"] == "array", "role_bullets must be a list, not a map"
    item = field["items"]
    if "$ref" in item:
        ref = item["$ref"].rsplit("/", 1)[-1]
        item = schema.get("$defs", {})[ref]
    assert set(item["properties"]) == {"company", "bullets"}


def test_bullets_for_is_insensitive_to_case_and_padding() -> None:
    """"GitLab Inc." vs "GitLab" must not cost a role its tailored bullets —
    the old exact-key dict lookup silently fell back to profile bullets."""
    resume = tailored(["A bullet."], company="Acme Corp")
    assert resume.bullets_for("acme corp") == ["A bullet."]
    assert resume.bullets_for("  ACME CORP  ") == ["A bullet."]
    assert resume.bullets_for("Nonexistent Ltd") is None


def test_bullets_for_treats_an_empty_list_as_missing() -> None:
    """So the renderer falls back to the profile rather than emitting a role
    with no bullets at all."""
    resume = TailoredResume(
        headline="h", summary="s", highlighted_skills=[],
        role_bullets=[RoleBullets(company="Acme Corp", bullets=[])],
    )
    assert resume.bullets_for("Acme Corp") is None


# ------------------------------------------------------ what must pass

def test_faithful_rephrasing_passes(profile: Profile) -> None:
    report = verify_no_fabrication(
        tailored(["Rolled out gVisor to 900 nodes, cutting container escape risk 40%."]),
        profile,
    )
    assert report.ok
    assert report.blockers == []


def test_merged_bullets_pass(profile: Profile) -> None:
    report = verify_no_fabrication(
        tailored(["Built threat modelling for 12 teams and cut escape risk 40%."]),
        profile,
    )
    assert report.ok


def test_generic_capitals_are_not_fabrications(profile: Profile) -> None:
    """Ordinary resume prose capitalises plenty of non-product words."""
    report = verify_no_fabrication(
        tailored(["Owned Security posture across Production and Cloud estates."]),
        profile,
    )
    assert report.ok, report.blockers


def test_untraceable_skill_warns_but_does_not_block(profile: Profile) -> None:
    """The behaviour change: this used to hard-block into Stage.FAILED, a stage
    with no retry path, on the first real tailoring call."""
    report = verify_no_fabrication(
        tailored(
            ["Rolled out gVisor across 900 nodes, cutting escape risk 40%."],
            skills=["Container isolation (gVisor)", "Cloud security posture"],
        ),
        profile,
    )
    assert report.ok
    assert len(report.warnings) == 2
    assert all("not traceable" in w for w in report.warnings)


# ------------------------------------------------------ what must block

def test_inflated_metric_blocks(profile: Profile) -> None:
    """The profile says 40%. The old check could never catch this."""
    report = verify_no_fabrication(
        tailored(["Cut container escape risk 60% via gVisor across 900 nodes."]),
        profile,
    )
    assert not report.ok
    assert any("60" in b for b in report.blockers)


def test_invented_headcount_blocks(profile: Profile) -> None:
    report = verify_no_fabrication(
        tailored(["Led a team of 25 engineers on threat modelling."]), profile
    )
    assert not report.ok


@pytest.mark.parametrize("tool", ["Falco", "Snyk", "Splunk", "SOC2", "OpenTelemetry"])
def test_invented_tooling_blocks(profile: Profile, tool: str) -> None:
    """Most security tooling is a plain single-capital word, so the entity
    matcher has to catch that shape and not just acronyms."""
    report = verify_no_fabrication(
        tailored([f"Deployed {tool} across 900 nodes."]), profile
    )
    assert not report.ok
    assert any(tool in b for b in report.blockers)


def test_unknown_employer_blocks(profile: Profile) -> None:
    report = verify_no_fabrication(
        tailored(["Did security things."], company="Initech"), profile
    )
    assert not report.ok
    assert any("Initech" in b for b in report.blockers)


def test_known_employer_in_any_casing_is_accepted(profile: Profile) -> None:
    report = verify_no_fabrication(
        tailored(["Automated AWS access review for 50 accounts."], company="globex"),
        profile,
    )
    assert report.ok, report.blockers


def test_repeated_fabrication_is_reported_once(profile: Profile) -> None:
    report = verify_no_fabrication(
        tailored([
            "Deployed Falco across 900 nodes.",
            "Tuned Falco rules for 12 teams.",
        ]),
        profile,
    )
    assert len(report.blockers) == 1


def test_report_summary_is_readable(profile: Profile) -> None:
    clean = verify_no_fabrication(
        tailored(["Migrated IAM policies to Terraform, closing 300 findings."]), profile
    )
    assert clean.summary() == "clean"

    dirty = verify_no_fabrication(tailored(["Deployed Falco everywhere."]), profile)
    assert "blocker" in dirty.summary()
