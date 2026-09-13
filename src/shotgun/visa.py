"""Detect visa sponsorship, relocation support, and employment type from a JD.

This is what lets shotgun rank a US or out-of-region role as worth applying to:
a posting outside the target countries is only interesting if it sponsors a
visa, pays to relocate, or is a contract the candidate can do from where they
already are.

Everything here is a text-signal heuristic over the job description. It is
deliberately conservative about *negative* signals — "no sponsorship" is a
much more reliable string than "sponsorship available", because employers
state the former explicitly and imply the latter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class Support(StrEnum):
    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class Employment(StrEnum):
    PERMANENT = "permanent"
    CONTRACT = "contract"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------- patterns
# Negative first — an explicit refusal always wins over a hopeful phrase.

_NO_SPONSOR = [
    r"\b(?:can|will)\s?not\s+(?:provide|offer|sponsor)\w*\s+(?:visa|sponsorship|immigration)",
    r"\bno\s+(?:visa\s+)?sponsorship\b",
    r"\bnot\s+able\s+to\s+sponsor\b",
    r"\bunable\s+to\s+sponsor\b",
    r"\bdo(?:es)?\s+not\s+sponsor\b",
    r"\bmust\s+be\s+(?:legally\s+)?authoriz\w+\s+to\s+work\b.{0,60}\bwithout\s+sponsorship\b",
    # The forms the corpus actually uses. Every pattern below was missing, and
    # between them they cover ~180 postings that were coming back `unknown`
    # while stating a refusal in plain English. Ordering matters more than it
    # looks: `analyse` checks refusals first and lets them win, so the
    # positives further down can afford to be generous only because these are
    # thorough.
    #
    # "visa sponsorship is not available for this position" (40)
    # "visa sponsorship is not offered for this role" (28)
    # "visa/work permit sponsorship is not available" (21)
    #
    # "not guaranteed" is deliberately absent. A company writing "sponsorship
    # is not guaranteed, it depends on the role" does sponsor, sometimes —
    # calling that a refusal would apply the -50 penalty to exactly the
    # postings this tool exists to surface. It stays `unknown`, which is the
    # honest reading of a hedge.
    r"\b(?:visa|immigration)?[\s/]*(?:work\s+permit)?[\s/]*sponsorship\s+is\s+not\s+"
    r"(?:available|offered|provided|possible)\b",
    # "we do not offer visa sponsorship at this time" (13)
    # "we do not offer visa sponsorship or assistance" (7)
    # "we are unable to offer visa sponsorship for this role" (9)
    r"\b(?:do|does|are|is|am)\s+not\s+(?:currently\s+)?(?:offer|provide)\s+"
    r"(?:any\s+)?(?:visa|immigration|work\s+permit)?\s*sponsorship\b",
    r"\bunable\s+to\s+(?:offer|provide)\s+(?:any\s+)?"
    r"(?:visa|immigration|employment)?\s*sponsorship\b",
    # "we do not currently sponsor immigration visas" (6)
    # "we cannot currently sponsor or support visa transfers" (11)
    r"\b(?:do|does)\s+not\s+currently\s+sponsor\b",
    r"\bcan\s?not\s+(?:currently\s+)?sponsor\b",
    # "this position is generally not eligible for new visa sponsorship" (36)
    r"\bnot\s+eligible\s+for\s+(?:new\s+)?(?:visa\s+|employment\s+)?sponsorship\b",
    # "without the need for current or future employer sponsorship" (30)
    # "work authorized in the united states without the need for new visa
    # sponsorship" (25)
    #
    # "the need for" is load-bearing and must not be relaxed to a bare
    # "without ... sponsorship". 366 postings carry export-control boilerplate
    # reading "export laws without sponsorship for an export license", which
    # is not about the right to work at all.
    r"\bwithout\s+(?:the\s+)?need\s+for\s+[\w\s]{0,30}?sponsorship\b",
    # "will not sponsor individuals for h-1b cap applications" (25)
    r"\bwill\s+not\s+sponsor\b",
    # Negated forms of the "able/willing to sponsor" phrasing the positives
    # below now accept. Both of these were read as offers before they were
    # added, which is the worst direction to be wrong in:
    #   "we are not able to offer visa sponsorship for this role"
    #   "we are no longer able to sponsor new H-1B visa petitions"
    #
    # "aren't" and the other contractions are deliberately not matched. The
    # commonest sponsorship boilerplate in this corpus — 560 postings — reads
    # "we do sponsor visas. However, we aren't able to successfully sponsor
    # visas for every role and every candidate", which is a company that
    # sponsors, qualifying it. Matching the contraction would turn every one
    # of those into a refusal.
    r"\b(?:not|never|no\s+longer)\s+(?:able|willing|prepared|in\s+a\s+position)\s+to\s+"
    r"(?:offer|provide|sponsor)\b",
    r"\bno\s+longer\s+sponsor\w*\b",
]

# Boilerplate that implies existing authorisation without ruling sponsorship
# out. "You must have the right to work in the UK" is printed by plenty of
# employers who hold a sponsor licence, so it loses to any explicit offer and
# is tagged separately in the evidence so a human can tell the two apart.
_WEAK_NO_SPONSOR = [
    r"\bmust\s+(?:already\s+)?(?:have|possess)\s+(?:the\s+)?(?:legal\s+)?right\s+to\s+work\b",
    r"\b(?:eligible|authoriz\w+)\s+to\s+work\s+in\s+the\s+\w+\s+without\b",
]

# Kept apart from _NO_SPONSOR: these say nothing about visas. Folding them in
# meant "We will sponsor a Skilled Worker visa. No relocation package." came
# back as sponsorship=no, which is the exact posting this tool exists to find.
_NO_RELOCATION = [
    r"\bno\s+relocation\b",
    r"\brelocation\s+(?:is\s+)?not\s+(?:provided|offered|available|supported)\b",
    r"\bwe\s+do\s+not\s+(?:offer|provide|cover)\s+relocation\b",
]

_YES_SPONSOR = [
    r"\bvisa\s+sponsorship\s+(?:is\s+)?(?:available|provided|offered)\b",
    r"\b(?:we|company)\s+(?:will\s+)?sponsor\w*\b",
    r"\bsponsorship\s+available\b",
    r"\bwe\s+(?:can|do)\s+sponsor\b",
    r"\bopen\s+to\s+sponsor\w+\b",
    r"\bh-?1b\s+(?:transfer|sponsorship)\b",
    # Offers phrased with the verb first, which the pattern above could not
    # reach because it wanted "available/provided/offered" *after* the noun.
    # "We offer visa sponsorship" and "happy to sponsor a visa" were both
    # coming back unknown, and an unknown offer is a missed role: sponsorship
    # is one of only three signals that gets an out-of-region posting past the
    # location filter at all, and it carries the largest priority bonus.
    #
    # These can be this generous only because a refusal is checked first and
    # wins. "We do not offer visa sponsorship" matches the second pattern
    # here as well, and is still recorded as a refusal.
    r"\b(?:offer|provide)s?\s+(?:visa\s+|work\s+permit\s+|immigration\s+)?sponsorship\b",
    r"\bsponsorship\s+(?:is\s+)?(?:available|provided|offered)\b",
    r"\b(?:happy|willing|able|glad|prepared)\s+to\s+sponsor\b",
    r"\b(?:we|company|business)\s+(?:may|can)\s+sponsor\b",
    r"\bmay\s+sponsor\s+(?:existing\s+)?(?:visa|work\s+permit|employment)\b",
    r"\b(?:can|may|will)\s+support\s+visa\s+transfers?\b",
    r"\bsponsor\s+(?:a\s+|an\s+|your\s+|the\s+)?(?:new\s+)?"
    r"(?:visa|work\s+permit|employment\s+visa)\b",
    r"\b(?:skilled\s+worker|tier\s?2)\s+(?:visa\s+)?(?:sponsor|licence|license)\w*\b",
    r"\bblue\s?card\b",
    r"\bwork\s+permit\s+(?:support|sponsorship|assistance)\b",
    r"\bimmigration\s+(?:support|assistance|sponsorship)\b",
    # "A relocation package with visa support for those who need it" — n26's
    # phrasing, and an unambiguous offer that both the work-permit and the
    # immigration pattern above just miss by a word.
    r"\bvisa\s+(?:support|assistance)\b",
    r"\beligible\s+for\s+sponsorship\b",
    r"\b(?:482|186|189|subclass)\s+visa\b",           # AU
    r"\baccredited\s+employer\s+work\s+visa\b",       # NZ
]

_YES_RELOCATION = [
    r"\brelocation\s+(?:package|assistance|support|bonus|allowance|reimburse\w*)\b",
    r"\bwe(?:'ll| will)?\s+(?:help\s+you\s+)?relocat\w+\b",
    r"\brelocation\s+(?:is\s+)?(?:available|provided|offered|covered)\b",
    r"\bassist\w*\s+with\s+relocation\b",
    r"\bpaid\s+relocation\b",
    r"\bwe\s+(?:pay|cover|fund)\s+(?:for\s+)?relocation\b",
    r"\bwilling\s+to\s+relocate\s+candidates\b",
]

_CONTRACT = [
    r"\b(?:contract|contractor|contracting)\s+(?:role|position|basis|opportunity|engagement)\b",
    r"\bfixed[-\s]?term\b",
    r"\b(?:c2c|corp\s?to\s?corp)\b",
    r"\b(?:w-?2|1099)\b",
    r"\bindependent\s+contractor\b",
    r"\bfreelance\b",
    r"\bstatement\s+of\s+work\b",
    # Durations: "6 month contract", "6-month contract", "3-6 month contract",
    # "12 months contract". The previous pattern required a *range*, so the
    # commonest phrasing of all — a single duration — was missed, and contract
    # is one of only three signals that lets an out-of-region role through.
    r"\b\d+(?:\s*(?:[-–]|to)\s*\d+)?[-\s]*months?\s+(?:contract|engagement|assignment)\b",
    # Weeks, but only with a duration marker that means engagement length.
    # Ubiminds advertises "Senior Security / QA Lead | 12 weeks+", a contract
    # by definition, which read as employment=unknown — and contract is one of
    # only three signals that gets an out-of-region role past the location
    # filter at all.
    #
    # The trailing "+" or an explicit contract word is required, and that is
    # the whole difficulty. Written loosely, this matched "4 weeks of
    # onboarding" and "30 weeks parental leave" — so permanent roles would
    # have collected the contract bonus and slipped through the location
    # filter, which is the expensive direction to be wrong in.
    r"\b\d+(?:\s*(?:[-–]|to)\s*\d+)?\s*weeks?\s*\+",
    r"\b\d+(?:\s*(?:[-–]|to)\s*\d+)?[-\s]*weeks?\s+(?:contract|engagement|assignment)\b",
    r"\bcontract[-\s]to[-\s]hire\b",
    r"\bday\s?rate\b",
]

_PERMANENT = [
    r"\bfull[-\s]?time\s+(?:permanent|employee|role|position)\b",
    r"\bpermanent\s+(?:role|position|contract|employee)\b",
    r"\bfte\b",
]


def _any(patterns: list[str], text: str) -> str | None:
    """Return the first matching pattern's matched text, or None."""
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(0).strip()
    return None


@dataclass
class VisaSignals:
    sponsorship: Support = Support.UNKNOWN
    relocation: Support = Support.UNKNOWN
    employment: Employment = Employment.UNKNOWN
    evidence: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.evidence is None:
            self.evidence = []

    @property
    def mobility_friendly(self) -> bool:
        """Would this role let someone move for it, or work it from abroad?"""
        return (
            self.sponsorship is Support.YES
            or self.relocation is Support.YES
            or self.employment is Employment.CONTRACT
        )

    def summary(self) -> str:
        bits = [f"visa={self.sponsorship}", f"reloc={self.relocation}"]
        if self.employment is not Employment.UNKNOWN:
            bits.append(f"type={self.employment}")
        return " ".join(bits)


def analyse(description: str | None, title: str | None = None) -> VisaSignals:
    """Read visa / relocation / employment-type signals out of a posting."""
    text = " ".join(filter(None, [title, description]))
    if not text.strip():
        return VisaSignals()

    signals = VisaSignals()

    # Sponsorship: an explicit refusal still beats an explicit offer, because
    # refusals are stated precisely and offers are often boilerplate. But both
    # are recorded when they conflict — a multi-country posting can honestly
    # say both — and mere right-to-work boilerplate loses to a real offer.
    refusal = _any(_NO_SPONSOR, text)
    offer = _any(_YES_SPONSOR, text)
    weak_refusal = _any(_WEAK_NO_SPONSOR, text)

    if refusal:
        signals.sponsorship = Support.NO
        signals.evidence.append(f"no-sponsor: {refusal!r}")
        if offer:
            signals.evidence.append(f"conflicting offer: {offer!r}")
    elif offer:
        signals.sponsorship = Support.YES
        signals.evidence.append(f"sponsor: {offer!r}")
    elif weak_refusal:
        signals.sponsorship = Support.NO
        signals.evidence.append(f"weak-no-sponsor: {weak_refusal!r}")

    # Relocation, evaluated independently of sponsorship. Refusal is checked
    # first because "No relocation package is offered" contains the very phrase
    # the positive patterns look for.
    reloc_refusal = _any(_NO_RELOCATION, text)
    reloc_offer = _any(_YES_RELOCATION, text)
    if reloc_refusal:
        signals.relocation = Support.NO
        signals.evidence.append(f"no-reloc: {reloc_refusal!r}")
    elif reloc_offer:
        signals.relocation = Support.YES
        signals.evidence.append(f"reloc: {reloc_offer!r}")

    contract = _any(_CONTRACT, text)
    permanent = _any(_PERMANENT, text)
    if contract:
        signals.employment = Employment.CONTRACT
        signals.evidence.append(f"contract: {contract!r}")
    elif permanent:
        signals.employment = Employment.PERMANENT

    return signals


def priority(
    score: int | None,
    signals: VisaSignals,
    *,
    in_target_region: bool,
    visa_weight: int = 15,
    relocation_weight: int = 10,
    contract_weight: int = 8,
    preference_bonus: int = 0,
) -> int:
    """Rank ordering for the apply queue.

    Roles that solve the mobility problem sort above roles that don't, so
    `shotgun prepare` spends its budget on applications that can actually
    result in a move. A hard "no sponsorship" on an out-of-region role is
    pushed to the bottom rather than dropped, because the location parse may
    have been wrong.

    `preference_bonus` carries the nudges that have nothing to do with
    mobility — a company or a country you want to reach first. The caller
    resolves it, so this function does not have to know what a company group
    is. It is deliberately added only on the actionable path: a role that is
    out of region with no way to move for it stays sunk whoever posted it,
    because the penalty is the whole reason that role is still in the list.

    The result is an ordering key, not a percentage, and is not capped at 100.
    It used to be. That cap was harmless while the bonuses could only add 33,
    but with company and country bonuses stacking on top it started returning
    exactly 100 for everything strong and handing the real ordering to the
    `score DESC` tiebreak — which sorts a plain score-100 role above a
    visa-sponsoring FAANG role in London. Only the floor at 0 is load-bearing.
    """
    base = score if score is not None else 0

    bonus = preference_bonus
    if signals.sponsorship is Support.YES:
        bonus += visa_weight
    if signals.relocation is Support.YES:
        bonus += relocation_weight
    if signals.employment is Employment.CONTRACT:
        bonus += contract_weight

    if not in_target_region:
        if signals.sponsorship is Support.NO:
            return max(0, base - 50)
        if not signals.mobility_friendly:
            # Out of region with no stated mobility support: keep it, but well
            # below anything actionable.
            return max(0, base - 25)

    return max(0, base + bonus)
