"""Profile loading.

The bug: a hand-edited `profile.yaml` with an unquoted colon in a list item —
`- Microsoft Certified: Azure Fundamentals (AZ-900)` — is a YAML *mapping*,
not a string. `Profile.load()` then died with `certifications.8 / Input should
be a valid string`, which names neither the line nor the fix. Hit on the first
real `shotgun prepare` run, and the README explicitly tells you to open this
file and edit it by hand.
"""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from shotgun import profile
from shotgun.profile import Profile, load

MINIMAL = {
    "contact": {"name": "Test Person", "email": "t@example.com"},
    "roles": [{"company": "Acme", "title": "Staff Engineer", "start": "2021",
               "bullets": ["Did a thing."]}],
    "skills": ["Kubernetes"],
    "certifications": ["CISSP"],
}


def write(tmp_path, data):
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    return path


def test_minimal_profile_loads(tmp_path) -> None:
    profile = load(write(tmp_path, MINIMAL))
    assert profile.contact.name == "Test Person"
    assert profile.certifications == ["CISSP"]


def test_colon_in_a_certification_is_repaired(tmp_path) -> None:
    path = tmp_path / "profile.yaml"
    path.write_text(
        "contact: {name: Test Person, email: t@example.com}\n"
        "roles:\n"
        "  - company: Acme\n"
        "    title: Staff Engineer\n"
        "    start: '2021'\n"
        "    bullets: ['Did a thing.']\n"
        "certifications:\n"
        "  - CISSP\n"
        "  - Microsoft Certified: Azure Fundamentals (AZ-900)\n"
    )
    profile = load(path)
    assert profile.certifications == [
        "CISSP",
        "Microsoft Certified: Azure Fundamentals (AZ-900)",
    ]


def test_colon_in_a_bullet_is_repaired(tmp_path) -> None:
    path = tmp_path / "profile.yaml"
    path.write_text(
        "contact: {name: T, email: t@example.com}\n"
        "roles:\n"
        "  - company: Acme\n"
        "    title: Staff Engineer\n"
        "    start: '2021'\n"
        "    bullets:\n"
        "      - Outcome: shipped the thing\n"
    )
    assert load(path).roles[0].bullets == ["Outcome: shipped the thing"]


def test_colon_in_a_skill_is_repaired(tmp_path) -> None:
    path = tmp_path / "profile.yaml"
    path.write_text(
        "contact: {name: T, email: t@example.com}\n"
        "roles: []\n"
        "skills:\n"
        "  - 'Cloud: AWS'\n"
        "  - IaC: Terraform\n"
    )
    assert load(path).skills == ["Cloud: AWS", "IaC: Terraform"]


def test_a_genuinely_malformed_profile_still_raises(tmp_path) -> None:
    """The repair is narrow: only single-key string mappings. A multi-key map
    is a real structural mistake and must not be silently flattened."""
    path = tmp_path / "profile.yaml"
    path.write_text(
        "contact: {name: T, email: t@example.com}\n"
        "roles: []\n"
        "certifications:\n"
        "  - {a: 1, b: 2}\n"
    )
    with pytest.raises(ValidationError):
        load(path)


def test_missing_profile_says_what_to_run(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match="profile init"):
        load(tmp_path / "nope.yaml")


def test_round_trip_through_save_is_stable(tmp_path) -> None:
    """`yaml.safe_dump` quotes colons correctly, so a profile written by
    `shotgun profile init` reloads unchanged. This guards the writer, not the
    reader — the bug only appears in hand-edited files."""
    from shotgun.profile import save

    data = dict(MINIMAL)
    data["certifications"] = ["Microsoft Certified: Azure Fundamentals (AZ-900)"]
    original = Profile.model_validate(data)

    path = tmp_path / "profile.yaml"
    save(original, path)
    assert load(path).certifications == original.certifications


# ------------------------------------------- re-parsing an updated resume

def test_manual_fields_survive_a_reparse(tmp_path) -> None:
    """`profile init` writes the whole file, so re-parsing an updated resume
    used to wipe the three things no resume states. work_authorization is the
    costly one: `authorised_countries()` reads it, and empty it makes
    `rank --authorised-only` match nothing at all."""
    path = tmp_path / "profile.yaml"
    existing = profile.template()
    existing.contact.name = "Ada Lovelace"
    existing.work_authorization = {"IN": "citizen", "DE": "work permit"}
    existing.salary_expectation = {"EUR": "120000"}
    existing.notice_period = "2 months"
    profile.save(existing, path)

    fresh = profile.template()
    fresh.contact.name = "Ada Lovelace"
    carried, backup = profile.carry_manual_fields(fresh, path)

    assert set(carried) == set(profile.MANUAL_FIELDS)
    assert fresh.work_authorization == {"IN": "citizen", "DE": "work permit"}
    assert fresh.salary_expectation == {"EUR": "120000"}
    assert fresh.notice_period == "2 months"
    assert backup is not None and backup.exists()


def test_the_old_profile_is_backed_up_with_its_comments(tmp_path) -> None:
    """`load_yaml` hands the raw file to the model, so notes written into it
    were part of the scoring prompt. `yaml.safe_dump` cannot keep them, so
    the least this can do is not lose them."""
    path = tmp_path / "profile.yaml"
    profile.save(profile.template(), path)
    path.write_text("# why the work permit matters\n" + path.read_text())

    _, backup = profile.carry_manual_fields(profile.template(), path)

    assert "# why the work permit matters" in backup.read_text()


def test_a_first_run_has_nothing_to_carry(tmp_path) -> None:
    carried, backup = profile.carry_manual_fields(
        profile.template(), tmp_path / "nope.yaml",
    )
    assert carried == []
    assert backup is None


def test_an_unreadable_profile_is_backed_up_rather_than_crashing(tmp_path) -> None:
    """A hand-edited file too broken to parse must not block a re-parse — but
    it still has to be recoverable."""
    path = tmp_path / "profile.yaml"
    path.write_text("this: is: not: valid: yaml:\n  - [")

    carried, backup = profile.carry_manual_fields(profile.template(), path)

    assert carried == []
    assert backup.exists()


def test_empty_manual_fields_are_not_carried(tmp_path) -> None:
    """Carrying an empty dict over a fresh parse would be a no-op that still
    reported itself as having done something."""
    path = tmp_path / "profile.yaml"
    existing = profile.template()
    existing.notice_period = "1 month"
    profile.save(existing, path)

    fresh = profile.template()
    carried, _ = profile.carry_manual_fields(fresh, path)

    assert carried == ["notice_period"]
