"""Visa, relocation and employment-type signals.

The bug these lock down: relocation-refusal patterns lived in the sponsorship
refusal list, and a refusal short-circuited the sponsorship offer check. So
"We will sponsor a Skilled Worker visa. No relocation package is offered."
came back as sponsorship=no — and for an out-of-region role that is a -50
priority penalty, i.e. the exact posting this tool exists to surface, buried.
"""

from __future__ import annotations

import pytest

from shotgun.visa import Employment, Support, analyse, priority

# ------------------------------------------------------------ sponsorship

def test_offer_survives_a_relocation_refusal() -> None:
    """The regression. These two facts are independent."""
    signals = analyse(
        "We will sponsor a Skilled Worker visa. No relocation package is offered."
    )
    assert signals.sponsorship is Support.YES
    assert signals.relocation is Support.NO
    assert signals.mobility_friendly is True


@pytest.mark.parametrize(
    "text",
    [
        "We are unable to sponsor visas for this role.",
        "No visa sponsorship is available.",
        "We cannot provide visa sponsorship.",
        "This role does not sponsor.",
        "Candidates must be legally authorized to work in the US without sponsorship.",
    ],
)
def test_explicit_refusals(text: str) -> None:
    assert analyse(text).sponsorship is Support.NO


@pytest.mark.parametrize(
    "text",
    [
        "Visa sponsorship is available for this position.",
        "We will sponsor the right candidate.",
        "Sponsorship available.",
        "We hold a Skilled Worker sponsor licence.",
        "Immigration support provided.",
        "Eligible for sponsorship.",
    ],
)
def test_explicit_offers(text: str) -> None:
    assert analyse(text).sponsorship is Support.YES


def test_right_to_work_boilerplate_loses_to_an_explicit_offer() -> None:
    """Plenty of employers print both. The offer is the specific claim."""
    signals = analyse("You must have the right to work in the UK, or we can sponsor.")
    assert signals.sponsorship is Support.YES


def test_right_to_work_boilerplate_alone_reads_as_no() -> None:
    """But on its own it does imply existing authorisation. Tagged distinctly
    so a human can tell it from an explicit refusal."""
    signals = analyse("You must have the right to work in the UK.")
    assert signals.sponsorship is Support.NO
    assert any("weak-no-sponsor" in e for e in signals.evidence)


def test_conflicting_signals_are_both_recorded() -> None:
    """A multi-country posting can honestly say both. Stay conservative, but
    leave the conflict visible rather than hiding it."""
    signals = analyse(
        "We cannot provide visa sponsorship in the US. In the UK we will sponsor."
    )
    assert signals.sponsorship is Support.NO
    assert any("conflicting offer" in e for e in signals.evidence)


def test_silence_is_unknown_not_no() -> None:
    signals = analyse("Great security engineering role based in Berlin.")
    assert signals.sponsorship is Support.UNKNOWN
    assert signals.relocation is Support.UNKNOWN
    assert signals.mobility_friendly is False


# ------------------------------------------------------------ relocation

@pytest.mark.parametrize(
    "text",
    [
        "Relocation package included.",
        "We offer relocation assistance.",
        "Paid relocation for the right candidate.",
        "We will help you relocate.",
    ],
)
def test_relocation_offers(text: str) -> None:
    assert analyse(text).relocation is Support.YES


@pytest.mark.parametrize(
    "text",
    [
        "This role offers no relocation.",
        "Relocation is not provided.",
        "No relocation package is offered.",
        "We do not offer relocation.",
    ],
)
def test_relocation_refusals(text: str) -> None:
    """Refusal is checked first: "No relocation package" contains the very
    phrase the positive patterns look for."""
    assert analyse(text).relocation is Support.NO


# ----------------------------------------------------------- employment

@pytest.mark.parametrize(
    "text",
    [
        "This is a 6 month contract.",
        "Contract role, competitive day rate.",
        "Independent contractor engagement.",
        "Fixed-term position.",
        "Freelance opportunity.",
    ],
)
def test_contract_detection(text: str) -> None:
    assert analyse(text).employment is Employment.CONTRACT


def test_contract_alone_makes_a_role_mobility_friendly() -> None:
    """A contract is workable from where the candidate already is."""
    assert analyse("6 month contract, day rate negotiable.").mobility_friendly is True


def test_permanent_detection() -> None:
    assert analyse("Full-time permanent role.").employment is Employment.PERMANENT


def test_empty_input() -> None:
    signals = analyse(None)
    assert signals.sponsorship is Support.UNKNOWN
    assert signals.evidence == []


# ------------------------------------------------------------- priority

def test_in_region_gets_the_mobility_bonus() -> None:
    signals = analyse("Visa sponsorship is available.")
    assert priority(70, signals, in_target_region=True) == 85


def test_out_of_region_without_mobility_is_penalised_not_dropped() -> None:
    signals = analyse("Nothing said about visas.")
    assert priority(80, signals, in_target_region=False) == 55


def test_out_of_region_with_explicit_refusal_sinks() -> None:
    signals = analyse("We cannot provide visa sponsorship.")
    assert priority(80, signals, in_target_region=False) == 30


def test_out_of_region_with_sponsorship_keeps_its_score() -> None:
    signals = analyse("Visa sponsorship is available and we pay relocation.")
    assert priority(70, signals, in_target_region=False) == 95


def test_priority_never_goes_negative() -> None:
    generous = analyse("Visa sponsorship available, relocation package, 6 month contract.")
    assert priority(95, generous, in_target_region=True) >= 0
    assert priority(None, generous, in_target_region=False) >= 0
    # The floor is the part that has to hold: an unscored, out-of-region,
    # explicitly-unsponsored posting would otherwise go below zero and sort
    # above rows whose priority is NULL.
    refused = analyse("We are unable to provide visa sponsorship.")
    assert priority(None, refused, in_target_region=False) == 0


def test_priority_is_not_capped_at_one_hundred() -> None:
    """Saturating at 100 handed the ordering to the score tiebreak.

    A strong posting that also sponsors, is at a company you want and in a
    country hiring hard must stay above a bare score-100 posting. Under the
    old `min(100, ...)` both came to exactly 100 and the plain role won on
    score, which is precisely backwards.
    """
    sponsored = analyse("We sponsor visas.")
    boosted = priority(95, sponsored, in_target_region=True, preference_bonus=20)
    plain = priority(100, analyse("Nothing relevant."), in_target_region=True)
    assert boosted == 130
    assert boosted > plain


# --------------------------------------------------------- preference bonus

def test_preference_bonus_adds_on_the_actionable_path() -> None:
    quiet = analyse("Nothing about visas.")
    assert priority(70, quiet, in_target_region=True, preference_bonus=12) == 82


def test_preference_bonus_does_not_rescue_an_unreachable_role() -> None:
    """A company bonus is not a reason to apply somewhere you cannot work.

    Out of region with no mobility signal keeps its penalty whoever posted it;
    that penalty is the only reason the posting is still in the list at all.
    """
    quiet = analyse("Nothing about visas.")
    assert priority(80, quiet, in_target_region=False, preference_bonus=12) == 55

    refused = analyse("We cannot provide visa sponsorship.")
    assert priority(80, refused, in_target_region=False, preference_bonus=12) == 30


def test_preference_bonus_stacks_with_mobility_bonuses() -> None:
    """Out of region but sponsoring is the actionable path, so it applies."""
    signals = analyse("Visa sponsorship is available.")
    assert priority(70, signals, in_target_region=False, preference_bonus=8) == 93


# ------------------------------------------- phrasings the corpus really uses
#
# Every string below was mined from the 15,076 stored job descriptions, with
# the count of postings carrying it. Guessing at phrasings is how the original
# patterns ended up missing the commonest refusal in the corpus, so these are
# quoted rather than invented.

@pytest.mark.parametrize(
    "text",
    [
        "Visa sponsorship is not available for this position.",       # 40
        "Visa sponsorship is not offered for this role.",              # 28
        "Visa/work permit sponsorship is not available.",              # 21
        "This position is generally not eligible for new visa sponsorship.",   # 36
        "You must be work authorized in the United States "
        "without the need for new visa sponsorship.",                  # 25
        "without the need for current or future employer sponsorship",  # 30
        "We do not offer visa sponsorship at this time.",               # 13
        "We do not offer visa sponsorship or assistance.",              # 7
        "We are unable to offer visa sponsorship for this role "
        "in any of the listed locations.",                              # 9
        "Please note we cannot currently sponsor or support "
        "visa transfers at this time.",                                 # 11
        "We do not currently sponsor immigration visas.",               # 6
        "We do not sponsor visas.",                                     # 11
        "The company can support visa transfers but will not sponsor "
        "individuals for H-1B cap applications.",                       # 25
        "We are not able to offer visa sponsorship for this role.",
        "At this time we are no longer able to sponsor new H-1B visa petitions.",
    ],
)
def test_a_stated_refusal_is_read_as_one(text: str) -> None:
    assert analyse(text).sponsorship is Support.NO


@pytest.mark.parametrize(
    "text",
    [
        "Visa sponsorship: we do sponsor visas.",                       # 560
        "We can sponsor visas to Germany; for any other country, "
        "you need to have existing right to work.",                     # 46
        "At Render's discretion, the business may sponsor "
        "existing visa transfers.",                                     # 36
        "We can sponsor visas.",                                        # 16
        "We are able to offer visa sponsorship for this position.",
        "We offer visa sponsorship for this role.",
        "We are happy to sponsor a visa for the right candidate.",
        "Sponsorship is available.",
        "We provide immigration sponsorship.",
        "We will sponsor a Skilled Worker visa.",
    ],
)
def test_a_stated_offer_is_read_as_one(text: str) -> None:
    assert analyse(text).sponsorship is Support.YES


@pytest.mark.parametrize(
    "text",
    [
        # "sponsor" in the corpus is mostly not about immigration at all.
        "Align with executives on business challenges and gain sponsorship "
        "for enterprise wide deployments.",
        "Professional development budget, conference sponsorship, "
        "and book reimbursement.",
        "Eligibility for company-sponsored health benefits is limited to "
        "team members based in the United States.",
        "Stay active: on-site yoga and a co-sponsored multisport card.",
        "Amazing office space at our HQ, sponsored co-working hubs.",
        "Build deep relationships with customer stakeholders and executive sponsors.",
        # 366 postings. Export-control boilerplate, not the right to work —
        # which is why the "without ... sponsorship" pattern has to require
        # "the need for" and cannot be relaxed.
        "compliance with export laws without sponsorship for an export license",
    ],
)
def test_sponsorship_that_is_not_about_visas_is_ignored(text: str) -> None:
    assert analyse(text).sponsorship is Support.UNKNOWN


def test_a_hedged_offer_stays_an_offer() -> None:
    """The single commonest sponsorship boilerplate in the corpus — 560
    postings — states an offer and then qualifies it. Reading the
    qualification as a refusal would bury 560 postings from a company that
    does sponsor, so the contraction in "aren't able to" is deliberately not
    matched by any refusal pattern.
    """
    text = ("Visa sponsorship: we do sponsor visas. However, we aren't able to "
            "successfully sponsor visas for every role and every candidate.")
    signals = analyse(text)
    assert signals.sponsorship is Support.YES


def test_not_guaranteed_is_a_hedge_rather_than_a_refusal() -> None:
    """A company saying this sponsors sometimes. Calling it a refusal would
    apply the -50 out-of-region penalty to exactly the postings worth
    finding, so it stays unknown."""
    text = ("Sponsorship for engineering and product roles is not guaranteed, "
            "but is instead based on the business needs for that specific role.")
    assert analyse(text).sponsorship is Support.UNKNOWN


@pytest.mark.parametrize(
    "text",
    [
        # Both read as offers before the refusals were tightened, because the
        # offer patterns match a substring of the refusal.
        "Please note this role is not eligible for sponsorship.",
        "Visa/work permit sponsorship is not available.",
    ],
)
def test_a_refusal_containing_an_offer_phrase_is_still_a_refusal(text: str) -> None:
    signals = analyse(text)
    assert signals.sponsorship is Support.NO
    # The conflict is recorded rather than hidden, so a human can check.
    assert any("conflicting offer" in e for e in signals.evidence)


def test_visa_support_is_an_offer() -> None:
    """n26's phrasing, on 69 postings. "work permit support" and "immigration
    support" were both already offers; "visa support" missed by one word."""
    text = "A relocation package with visa support for those who need it."
    signals = analyse(text)
    assert signals.sponsorship is Support.YES
    assert signals.relocation is Support.YES


# ------------------------------------------------ contracts counted in weeks

def test_a_weeks_denominated_engagement_is_a_contract() -> None:
    """Agencies title these in weeks, not months. Ubiminds advertises
    "Senior Security / QA Lead | 12 weeks+", which read as unknown — and
    contract is one of only three signals that gets an out-of-region role
    past the location filter."""
    assert analyse("A contract engagement.",
                   "Senior Security / QA Lead | 12 weeks+ (569)").employment \
        is Employment.CONTRACT
    assert analyse("This is a 12 week contract.").employment is Employment.CONTRACT


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("Permanent full-time role, 4 weeks of onboarding provided.",
         Employment.PERMANENT),
        ("You get 30 weeks parental leave and 6 weeks holiday.",
         Employment.UNKNOWN),
        ("We offer 26 weeks of paid maternity leave.", Employment.UNKNOWN),
    ],
)
def test_weeks_in_a_benefit_is_not_a_contract(description, expected) -> None:
    """Why the pattern requires a trailing "+" or an explicit contract word.
    Written loosely it matched onboarding and parental leave, so permanent
    roles collected the contract bonus and slipped through the location
    filter — the expensive direction to be wrong in."""
    assert analyse(description).employment is expected
