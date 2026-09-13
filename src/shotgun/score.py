"""Two-stage relevance filter.

Stage 1 is free: title regex, location, comp, blocked companies. It throws away
the large majority of postings.

Stage 2 asks Claude to score what survived against the profile. Only jobs that
clear `scoring.minimum_score` get queued for an application.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter

from . import db, llm, visa
from .config import Preferences, matching_companies
from .geo import EU_EEA_CH, detect_countries, expand_regions, location_passes
from .models import JobScore, Stage

log = logging.getLogger(__name__)

SCORING_SYSTEM = """You score job postings for a single candidate whose profile \
is given above. You are a strict filter, not a cheerleader.

Score 0-100 on how well the candidate actually fits, weighing:
- Seniority match. The candidate wants staff-level IC, lead security engineer, \
or security management roles. A posting that is really a mid-level role with an \
inflated title should score low.
- Domain match against the candidate's actual security experience.
- Whether the candidate meets the stated hard requirements.

Calibration: 85+ means apply today. 70-84 means a solid fit worth applying to. \
50-69 means plausible but a stretch or a partial mismatch. Below 50 means don't \
bother. Most postings are not a great fit — use the low end of the range freely.

Set `level` to whichever of staff / lead / manager / other the posting really is, \
based on the responsibilities described rather than the job title.

Separate disqualifying blockers from ordinary friction. This distinction \
matters: anything you put in `dealbreakers` removes the posting from the apply \
queue outright, whatever it scored.

`dealbreakers` — only genuine disqualifiers:
- an active security clearance or citizenship requirement
- the posting explicitly rules out visa sponsorship AND hires only into \
countries where the candidate has no authorisation
- a stated hard requirement the candidate plainly lacks (years in a language \
or domain they have never worked in, a mandatory certification they don't hold)
- people-management experience required where the candidate has none

`concerns` — everything else worth flagging, which does NOT disqualify:
- relocating to another country inside the candidate's target list. The \
candidate is actively looking to move between those countries; a role in one \
of them is the goal, not an obstacle. Never call this a dealbreaker.
- onsite or hybrid working, commute, time zone
- a seniority stretch in either direction, or missing preferred-but-optional skills

`key_requirements` should name the 5-8 things a tailored resume must speak to.

Mobility matters a great deal to this candidate, so read the posting carefully \
for it:
- `visa_sponsorship`: yes only if the posting says or clearly implies it will \
sponsor. no if it requires existing authorisation or rules sponsorship out. \
unknown if silent — do not guess optimistically.
- `relocation_support`: yes only if relocation assistance is actually mentioned.
- `employment_type`: contract for fixed-term, contractor, C2C, W2, 1099, \
freelance or day-rate arrangements. permanent for standard FTE roles.
- `hiring_countries`: ISO codes the role will genuinely hire into. A posting \
listing an office city implies that country. "Remote - US" means US only."""


def authorised_countries(profile) -> set[str]:
    """Countries the candidate can work in without an employer sponsoring them.

    Read from `profile.work_authorization`: any entry whose status does not
    call for sponsorship counts, and an "EU" entry expands to the whole
    EU/EEA because that is what Blue Card intra-EU mobility means in practice.

    Exists so a large sweep can be spent where it pays. LinkedIn searches by
    title, so 68% of its results clear the rule filter — good for coverage,
    but scoring all of them at once is expensive, and roughly a third were UK
    postings needing a Skilled Worker visa.
    """
    from .answers import _needs_sponsorship

    auth = {k: v for k, v in (getattr(profile, "work_authorization", None) or {}).items() if v}
    out: set[str] = set()
    for country, status in auth.items():
        if _needs_sponsorship(status):
            continue
        code = country.strip().upper()
        if code == "EU":
            out |= EU_EEA_CH
        else:
            out.add(code)
    return out


def candidate_context(prefs: Preferences, profile=None) -> str:
    """The search criteria the model was previously never told.

    `SCORING_SYSTEM` asks it to flag "relocation outside the candidate's
    regions" as a blocker, but nothing in the request said what those regions
    were — so it inferred them from the address on the profile and flagged
    every intra-European move. Stockholm and a remote EMEA role, the two
    highest-scoring postings of the first real run, both died that way.
    """
    allowed = sorted(expand_regions(prefs.regions, prefs.extra_countries))
    mobility = sorted(prefs.mobility_friendly_countries)

    lines = [
        "<search_criteria>",
        "Target countries. The candidate is actively looking to work in any of",
        "these and is willing to relocate between them. A role in any of them",
        "is in scope — that is the goal, not a relocation obstacle:",
        "  " + ", ".join(allowed),
    ]
    if mobility:
        lines += [
            "",
            "These countries are out of scope UNLESS the posting sponsors a visa,",
            "pays relocation, or is contract work: " + ", ".join(mobility),
        ]

    auth = {k: v for k, v in (getattr(profile, "work_authorization", None) or {}).items() if v}
    if auth:
        lines += ["", "Existing work authorisation (country -> status):"]
        lines += [f"  {country}: {status}" for country, status in sorted(auth.items())]
        lines.append(
            "For a target country where the candidate lacks authorisation, note it"
        )
        lines.append(
            "in `concerns`. Make it a dealbreaker only if the posting explicitly"
        )
        lines.append("rules sponsorship out.")
    else:
        lines += [
            "",
            "Work authorisation is not recorded in the profile. Do not invent",
            "work-permit dealbreakers — if authorisation is genuinely unclear for",
            "a target country, say so in `concerns` and score on the actual fit.",
        ]

    languages = getattr(profile, "languages", None) or []
    if languages:
        lines += ["", "Languages: " + "; ".join(languages)]

    lines.append("</search_criteria>")
    return "\n".join(lines)


def rule_filter(
    row: sqlite3.Row | dict,
    prefs: Preferences,
) -> tuple[bool, str, visa.VisaSignals]:
    """The cheap pass. Returns (passes, reason, visa_signals).

    Mobility signals are read only once a posting has cleared the title
    check, and that ordering is the difference between a filter pass costing
    24 seconds and costing two. `visa.analyse` regexes the whole job
    description; the title check is a handful of regexes over a short string
    and rejects about 94% of a real corpus. Computing signals first meant
    doing the expensive half of the work for 9,000 postings in order to
    throw it away.

    A posting rejected on its title or its company therefore comes back with
    UNKNOWN signals rather than analysed ones. That is also the more honest
    answer: its mobility was never assessed, and nothing downstream reads
    those fields for a row rejected this early.
    """
    title = row["title"] or ""

    # Word-boundary matched, not an exact string compare. A block list is a
    # "never apply here" instruction — usually a current employer — and the
    # boards do not agree on a company's name: the ATS says "acme"
    # where LinkedIn says "Acme GmbH". An exact compare honours the
    # rule on one source and silently ignores it on the other.
    blocked = matching_companies(row["company"], prefs.blocked_companies)
    if blocked:
        return False, f"{blocked[0]} is on the blocked list", visa.VisaSignals()

    ok, reason = prefs.title_matches(title, row["company"])
    if not ok:
        return False, reason, visa.VisaSignals()

    # Read before the location check, so an out-of-region role that sponsors
    # a visa can still get through it.
    signals = visa.analyse(row["description"], title)

    allowed = expand_regions(prefs.regions, prefs.extra_countries)
    loc_ok, loc_reason = location_passes(
        row["location"], allowed, prefs.regions,
        prefs.accept_remote, prefs.reject_other_countries,
        mobility_countries=prefs.mobility_friendly_countries,
        is_mobility_friendly=signals.mobility_friendly,
    )
    if not loc_ok:
        return False, loc_reason, signals

    # Comp check only bites when the posting published a number.
    floor = prefs.salary_minimums.get((row["salary_currency"] or "").upper())
    if floor and row["salary_max"]:
        if float(row["salary_max"]) < float(floor):
            return False, (
                f"max {row['salary_currency']} {row['salary_max']:,.0f} "
                f"below floor {floor:,.0f}"
            ), signals

    description = row["description"] or ""
    for pattern in prefs.dealbreaker_patterns:
        if pattern.search(description):
            return False, f"JD contains dealbreaker {pattern.pattern!r}", signals

    return True, f"{reason}; {loc_reason}; {signals.summary()}", signals


def in_target_region(location: str | None, prefs: Preferences) -> bool:
    allowed = expand_regions(prefs.regions, prefs.extra_countries)
    countries = detect_countries(location)
    if not countries:
        return True   # unparsed: treat as in-region so it isn't penalised twice
    return any(c in allowed for c in countries)


def preference_bonus(
    row: sqlite3.Row | dict, prefs: Preferences
) -> tuple[int, list[str]]:
    """The company and country nudges, resolved for one posting.

    Returns the bonus and a human-readable breakdown, so `shotgun show` can
    say *why* something is near the top of the queue rather than only that it
    is. Company and country add together — they are separate reasons to want
    the role — but two matches within either dimension take the larger of the
    two, so "Amazon Web Services (AWS)" matching both `amazon` and `aws`
    counts once.
    """
    bonuses = prefs.company_bonuses
    company_hit = max(
        (
            (bonuses[name], name)
            for name in matching_companies(row["company"], bonuses)
            if bonuses[name]
        ),
        default=None,
    )

    by_country = prefs.country_bonuses
    country_hit = max(
        (
            (by_country[code], code)
            for code in detect_countries(row["location"])
            if by_country.get(code)
        ),
        default=None,
    )

    bonus, why = 0, []
    for hit, label in ((company_hit, "company"), (country_hit, "country")):
        if hit:
            amount, name = hit
            bonus += amount
            why.append(f"{label} {name} +{amount}")
    return bonus, why


def score_with_claude(
    row: sqlite3.Row | dict, profile_yaml: str, context: str = ""
) -> JobScore:
    """One posting, one structured verdict."""
    posting = "\n".join([
        f"Company: {row['company']}",
        f"Title: {row['title']}",
        f"Location: {row['location'] or 'unstated'}",
        f"URL: {row['url']}",
        "",
        "Description:",
        (row["description"]
         or "(no description captured — score on title and company only)")[:20000],
    ])

    return llm.parse_structured(
        [profile_yaml, SCORING_SYSTEM, context],
        f"<posting>\n{posting}\n</posting>",
        JobScore,
        max_tokens=8000,
        label="score",
    )


def _score_one(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    prefs: Preferences,
    profile_yaml: str,
    weights: dict[str, int],
    *,
    use_llm: bool,
    tally: dict[str, int],
    context: str = "",
) -> None:
    """Both stages for a single posting. Raises; the caller decides what that
    means for the transaction."""
    job_id = row["id"]
    passes, reason, signals = rule_filter(row, prefs)

    if not passes:
        db.save_score(conn, job_id, score=None, level=None,
                      reasoning=None, rule_reason=reason,
                      visa_sponsorship=str(signals.sponsorship),
                      relocation_support=str(signals.relocation),
                      employment_type=str(signals.employment))
        db.set_stage(conn, job_id, Stage.FILTERED_OUT, note=reason)
        tally["filtered"] += 1
        return

    regional = in_target_region(row["location"], prefs)
    pref_bonus, pref_why = preference_bonus(row, prefs)

    if not use_llm:
        prio = visa.priority(
            None, signals, in_target_region=regional,
            visa_weight=weights["visa_sponsorship_bonus"],
            relocation_weight=weights["relocation_bonus"],
            contract_weight=weights["contract_bonus"],
            preference_bonus=pref_bonus,
        )
        db.save_score(conn, job_id, score=None, level=None,
                      reasoning="rule filter only", rule_reason=reason,
                      visa_sponsorship=str(signals.sponsorship),
                      relocation_support=str(signals.relocation),
                      employment_type=str(signals.employment),
                      priority=prio)
        db.set_stage(conn, job_id, Stage.QUEUED, note=reason)
        tally["queued"] += 1
        return

    verdict = score_with_claude(row, profile_yaml, context)

    merged = _merge_signals(signals, verdict)
    prio = visa.priority(
        verdict.score, merged, in_target_region=regional,
        visa_weight=weights["visa_sponsorship_bonus"],
        relocation_weight=weights["relocation_bonus"],
        contract_weight=weights["contract_bonus"],
        preference_bonus=pref_bonus,
    )

    db.save_score(
        conn, job_id,
        score=verdict.score,
        level=verdict.level,
        reasoning=verdict.reasoning,
        dealbreakers=verdict.dealbreakers,
        concerns=verdict.concerns,
        key_requirements=verdict.key_requirements,
        rule_reason=reason,
        visa_sponsorship=str(merged.sponsorship),
        relocation_support=str(merged.relocation),
        employment_type=str(merged.employment),
        hiring_countries=verdict.hiring_countries,
        priority=prio,
    )
    tally["scored"] += 1
    if merged.mobility_friendly:
        tally["mobility"] += 1

    facts = f"priority {prio}, {merged.summary()}"
    if pref_why:
        facts += f", {', '.join(pref_why)}"
    note = f"score {verdict.score} ({facts}): {verdict.reasoning}"
    if verdict.concerns:
        note += f" | concerns: {'; '.join(verdict.concerns)}"

    if verdict.dealbreakers:
        db.set_stage(conn, job_id, Stage.SCORED_LOW,
                     note=f"dealbreakers: {'; '.join(verdict.dealbreakers)}")
        tally["low"] += 1
    elif verdict.score >= prefs.minimum_score:
        db.set_stage(conn, job_id, Stage.QUEUED, note=note)
        tally["queued"] += 1
    else:
        db.set_stage(conn, job_id, Stage.SCORED_LOW, note=note)
        tally["low"] += 1


def rebucket(conn: sqlite3.Connection, prefs: Preferences) -> dict[str, int]:
    """Re-apply `scoring.minimum_score` to scores already paid for.

    The threshold is applied when a posting is scored, so lowering it in
    preferences left every previously-scored role sitting in the bucket the
    old threshold put it in. Re-deciding from the stored score costs nothing —
    no model call — and is the difference between editing the threshold and
    having the edit mean anything.

    Only queued and scored_low are touched. A submitted or interviewing
    application is not re-bucketed by a config edit.
    """
    moved = {"to_queued": 0, "to_low": 0, "unchanged": 0}

    rows = conn.execute(
        """SELECT s.job_id, s.score, s.dealbreakers, a.stage
             FROM scores s JOIN applications a ON a.job_id = s.job_id
            WHERE s.score IS NOT NULL
              AND a.stage IN (?, ?)""",
        (str(Stage.QUEUED), str(Stage.SCORED_LOW)),
    ).fetchall()

    for row in rows:
        blockers = json.loads(row["dealbreakers"] or "[]")
        target = (
            Stage.SCORED_LOW
            if blockers or row["score"] < prefs.minimum_score
            else Stage.QUEUED
        )
        if row["stage"] == str(target):
            moved["unchanged"] += 1
            continue
        db.set_stage(
            conn, row["job_id"], target,
            note=f"re-bucketed at threshold {prefs.minimum_score} (score {row['score']})",
        )
        moved["to_queued" if target is Stage.QUEUED else "to_low"] += 1

    conn.commit()
    return moved


def filter_tally(conn: sqlite3.Connection, prefs: Preferences) -> dict:
    """How many stored postings the current rule filter keeps. Free.

    `status` reports stages, which cannot answer "how many roles are actually
    open for me" once there is a backlog: 9,573 postings sitting at
    `discovered` have not been through the filter yet, so they are neither
    kept nor rejected. This runs the filter — no model calls — and says.

    Reported against what is stored now rather than re-fetching, so it
    answers for the corpus you have. `pending` is the number that matters
    before a `rank`: postings that pass and have not been scored, i.e. what
    the next run would spend money on.
    """
    rows = conn.execute(
        """SELECT j.*, s.score FROM jobs j
                LEFT JOIN scores s ON s.job_id = j.id"""
    ).fetchall()

    out = {
        "total": len(rows), "passed": 0, "rejected": 0, "pending": 0,
        "reasons": Counter(), "companies": Counter(),
    }

    for row in rows:
        ok, reason, _ = rule_filter(row, prefs)
        if not ok:
            out["rejected"] += 1
            out["reasons"][reason] += 1
            continue
        out["passed"] += 1
        out["companies"][row["company"]] += 1
        # Judged on whether a score exists, not on the stage. A posting an
        # older, narrower filter rejected sits at `filtered_out` with no
        # score — it passes now, so it is still a bill waiting to happen.
        if row["score"] is None:
            out["pending"] += 1

    return out


#: What a posting's own text says about moving for it. `SILENT` and
#: `UNREADABLE` are kept apart on purpose — most employers simply never
#: mention sponsorship, and reading that as a refusal would throw away the
#: majority of the market. `UNREADABLE` is a gap in our data, not in theirs.
MOBILITY_STANCES = ("offers", "refuses", "silent", "unreadable")


def mobility_report(conn: sqlite3.Connection, prefs: Preferences) -> list[dict]:
    """Every stored security role, with its own stance on visa and relocation.

    Read from the description each time rather than from `scores`, so this
    covers postings that were never scored — which is most of them — and
    picks up improvements to the visa patterns without a rescore. No model
    calls.

    Titles are matched with the any-level patterns, because the question here
    is "who will move me" rather than "what would the pipeline queue", and a
    company that sponsors is worth knowing about whatever it calls the role.
    """
    allowed = expand_regions(prefs.regions, prefs.extra_countries)
    rows = conn.execute(
        """SELECT j.id, j.company, j.title, j.location, j.url, j.apply_url,
                  j.description, j.source
             FROM jobs j"""
    ).fetchall()

    out: list[dict] = []
    for row in rows:
        if not prefs.title_matches(row["title"], row["company"], any_level=True)[0]:
            continue

        readable = bool(row["description"]) and len(row["description"]) > 200
        signals = (visa.analyse(row["description"], row["title"])
                   if readable else visa.VisaSignals())

        if not readable:
            stance = "unreadable"
        elif (signals.sponsorship is visa.Support.YES
              or signals.relocation is visa.Support.YES):
            stance = "offers"
        elif signals.sponsorship is visa.Support.NO:
            stance = "refuses"
        else:
            stance = "silent"

        found = detect_countries(row["location"])
        out.append({
            "job_id": row["id"],
            "company": row["company"],
            "title": row["title"],
            "location": row["location"],
            "url": row["apply_url"] or row["url"],
            "source": row["source"],
            "stance": stance,
            "sponsorship": signals.sponsorship,
            "relocation": signals.relocation,
            "employment": signals.employment,
            "evidence": signals.evidence,
            "in_region": bool(set(found) & allowed),
        })
    return out


def _stored_signals(row: sqlite3.Row) -> visa.VisaSignals:
    """Rebuild the mobility signals a previous run wrote to `scores`.

    Falls back to UNKNOWN on anything unrecognised rather than raising — an
    old row written before a value existed should not break a reprioritise.
    """
    def support(raw: str | None) -> visa.Support:
        try:
            return visa.Support(raw or "unknown")
        except ValueError:
            return visa.Support.UNKNOWN

    try:
        employment = visa.Employment(row["employment_type"] or "unknown")
    except ValueError:
        employment = visa.Employment.UNKNOWN

    return visa.VisaSignals(
        sponsorship=support(row["visa_sponsorship"]),
        relocation=support(row["relocation_support"]),
        employment=employment,
    )


def _refresh_signals(
    fresh: visa.VisaSignals, row: sqlite3.Row
) -> visa.VisaSignals:
    """A re-read of the description, merged over what is already stored.

    Same precedence as `_merge_signals`, and for the same reason. What is
    stored is not the regex's answer — it is the regex merged with Claude's,
    and Claude read the whole posting. So the regex wins wherever it actually
    fired, and where it found nothing the stored value stands.

    Overwriting instead of merging silently threw that away. n26's postings
    say "a relocation package with visa support for those who need it", which
    Claude had read as sponsorship and no pattern caught; re-reading dropped
    them from priority 113 to 98 and moved them down the queue. A free pass
    over derived data must not be able to lose paid-for information.
    """
    stored = _stored_signals(row)

    def pick(a: visa.Support, b: visa.Support) -> visa.Support:
        return a if a is not visa.Support.UNKNOWN else b

    return visa.VisaSignals(
        sponsorship=pick(fresh.sponsorship, stored.sponsorship),
        relocation=pick(fresh.relocation, stored.relocation),
        employment=(
            fresh.employment
            if fresh.employment is not visa.Employment.UNKNOWN
            else stored.employment
        ),
        evidence=list(fresh.evidence),
    )


def reprioritise(conn: sqlite3.Connection, prefs: Preferences) -> dict[str, int]:
    """Recompute `priority` for every scored posting, free.

    Same argument as `rebucket`: priority is written when a posting is scored,
    so adding a company or country bonus to preferences.yaml would otherwise
    only affect postings discovered afterwards, leaving thousands of already
    paid-for scores ordered by the old weights. Everything priority needs is
    already in the database — the score, the mobility signals, the company and
    the location — so this re-derives it without a single model call.

    The mobility signals are re-read from the stored description rather than
    trusted as stored, and written back. They are derived data and deriving
    them is free, so trusting the old strings would mean a fix to the visa
    patterns never reached a posting already in the database — and improving
    those patterns is precisely when this wants running. The last such fix
    changed the reading of 333 postings, 24 of them from "sponsors" to
    "explicitly does not".

    Stages are untouched. This changes the order of the queue, not its
    membership; `rebucket` is what decides membership.
    """
    changed = {"changed": 0, "unchanged": 0, "signals_changed": 0}
    weights = prefs.priority_weights

    rows = conn.execute(
        """SELECT s.job_id, s.score, s.priority, s.visa_sponsorship,
                  s.relocation_support, s.employment_type,
                  j.company, j.location, j.title, j.description
             FROM scores s JOIN jobs j ON j.id = s.job_id
            WHERE s.score IS NOT NULL"""
    ).fetchall()

    for row in rows:
        bonus, _ = preference_bonus(row, prefs)

        # A posting with no description stored cannot be re-read, so keep
        # whatever the run that scored it recorded.
        if row["description"]:
            signals = _refresh_signals(
                visa.analyse(row["description"], row["title"]), row,
            )
        else:
            signals = _stored_signals(row)

        stale = (
            str(signals.sponsorship) != (row["visa_sponsorship"] or "unknown")
            or str(signals.relocation) != (row["relocation_support"] or "unknown")
            or str(signals.employment) != (row["employment_type"] or "unknown")
        )

        prio = visa.priority(
            row["score"], signals,
            in_target_region=in_target_region(row["location"], prefs),
            visa_weight=weights["visa_sponsorship_bonus"],
            relocation_weight=weights["relocation_bonus"],
            contract_weight=weights["contract_bonus"],
            preference_bonus=bonus,
        )

        if not stale and prio == row["priority"]:
            changed["unchanged"] += 1
            continue

        conn.execute(
            """UPDATE scores
                  SET priority = ?, visa_sponsorship = ?,
                      relocation_support = ?, employment_type = ?
                WHERE job_id = ?""",
            (prio, str(signals.sponsorship), str(signals.relocation),
             str(signals.employment), row["job_id"]),
        )
        changed["changed"] += 1
        if stale:
            changed["signals_changed"] += 1

    conn.commit()
    return changed


def score_pending(
    conn: sqlite3.Connection,
    prefs: Preferences,
    profile_yaml: str,
    *,
    limit: int = 200,
    use_llm: bool = True,
    profile=None,
    refilter: bool = False,
    only_countries: set[str] | None = None,
) -> dict[str, int]:
    """Run both stages over everything unscored. Returns a tally.

    Each posting is committed on its own. db.connect() only commits on a clean
    exit, so a single bad response used to unwind the whole run — score 150
    postings, hit one connection error, lose all 150. A posting that fails is
    rolled back and left unscored, which means the next run picks it up again.
    """
    tally = {
        "filtered": 0, "scored": 0, "queued": 0, "low": 0,
        "errors": 0, "mobility": 0,
    }

    weights = prefs.priority_weights
    # Built once: it must be byte-stable across the run or it breaks the
    # prompt cache for every call after the first.
    context = candidate_context(prefs, profile)

    for row in db.unscored_jobs(conn, limit=limit,
                                include_rule_filtered=refilter):
        if only_countries is not None:
            found = detect_countries(row["location"])
            if not found or not (set(found) & only_countries):
                # Left unscored rather than rejected, so a later run without
                # the gate still picks it up.
                tally["skipped_country"] = tally.get("skipped_country", 0) + 1
                continue

        label = f"{row['title']} @ {row['company']}"
        try:
            _score_one(conn, row, prefs, profile_yaml, weights,
                       use_llm=use_llm, tally=tally, context=context)
        except Exception as exc:
            conn.rollback()
            tally["errors"] += 1
            # An exhausted balance stops the run rather than repeating itself
            # 441 times. Every remaining posting would fail the same way, so
            # continuing costs minutes and teaches nothing; the postings
            # already scored are committed and a later run resumes from here.
            if llm.is_out_of_credit(exc):
                tally["out_of_credit"] = 1
                log.error("%s: %s", label, exc)
                log.error("stopping: no credit left on the %s account. "
                          "Everything scored so far is saved.", llm.provider())
                break
            # Retryable failures leave the posting unscored so the next run
            # picks it up; anything else is logged with a traceback. The
            # classification lives in llm so this does not have to know which
            # provider raised.
            if llm.is_retryable(exc):
                log.warning("%s: %s — left unscored for the next run", label, exc)
            else:
                log.exception("%s: unexpected failure while scoring", label)
            continue

        conn.commit()

    return tally


def _merge_signals(signals: visa.VisaSignals, verdict: JobScore) -> visa.VisaSignals:
    """Combine the regex read with Claude's read.

    The regexes only fire on explicit statements, so where they found nothing
    Claude's judgement is used. Where the regex found an explicit refusal it
    wins — that string is unambiguous and Claude occasionally reads a generic
    "we welcome applicants worldwide" as sponsorship.
    """
    merged = visa.VisaSignals(
        sponsorship=signals.sponsorship,
        relocation=signals.relocation,
        employment=signals.employment,
        evidence=list(signals.evidence),
    )

    if merged.sponsorship is visa.Support.UNKNOWN:
        try:
            merged.sponsorship = visa.Support(verdict.visa_sponsorship)
            merged.evidence.append(f"claude: sponsorship={verdict.visa_sponsorship}")
        except ValueError:
            pass

    if merged.relocation is visa.Support.UNKNOWN:
        try:
            merged.relocation = visa.Support(verdict.relocation_support)
        except ValueError:
            pass

    if merged.employment is visa.Employment.UNKNOWN:
        try:
            merged.employment = visa.Employment(verdict.employment_type)
        except ValueError:
            pass

    return merged
