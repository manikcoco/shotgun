"""shotgun CLI."""

from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import answers as answers_mod
from . import db, pipeline, visa
from . import profile as profile_mod
from .config import Preferences
from .models import Stage

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Find relevant senior security roles, tailor a resume per JD, "
         "queue the application for your approval, and track it.",
)
profile_app = typer.Typer(no_args_is_help=True, help="Manage your candidate profile.")
app.add_typer(profile_app, name="profile")
answers_app = typer.Typer(
    no_args_is_help=True,
    help="The reusable answers every application form asks for.",
)
app.add_typer(answers_app, name="answers")

console = Console()


# ------------------------------------------------------------ presentation

# Stage -> colour. Shared by every view so a stage looks the same everywhere.
STAGE_COLOUR = {
    Stage.OFFER: "bright_green", Stage.INTERVIEW: "green",
    Stage.SCREENING: "green", Stage.SUBMITTED: "cyan",
    Stage.ACKNOWLEDGED: "cyan", Stage.AWAITING_APPROVAL: "yellow",
    Stage.TAILORING: "yellow", Stage.QUEUED: "blue",
    Stage.DISCOVERED: "dim", Stage.SCORED_LOW: "dim",
    Stage.FILTERED_OUT: "dim", Stage.REJECTED: "red",
    Stage.WITHDRAWN: "dim", Stage.FAILED: "bright_red",
}


def _stage_text(stage: str | None) -> Text:
    if not stage:
        return Text("—", style="dim")
    try:
        colour = STAGE_COLOUR.get(Stage(stage), "white")
    except ValueError:
        colour = "white"
    return Text(stage, style=colour)


def _score_text(score: int | None, minimum: int = 70) -> Text:
    if score is None:
        return Text("—", style="dim")
    style = "green" if score >= minimum else "yellow" if score >= 50 else "dim"
    return Text(str(score), style=style)


def _mobility_flags(row) -> Text:
    """Compact visa / relocation / employment markers.

    Never colour alone — each marker carries a letter, so it still reads in a
    pipe or on a monochrome terminal.
    """
    out = Text()
    if (row["visa_sponsorship"] or "") == "yes":
        out.append("visa ", style="green")
    elif (row["visa_sponsorship"] or "") == "no":
        out.append("no-visa ", style="red")
    if (row["relocation_support"] or "") == "yes":
        out.append("reloc ", style="green")
    if (row["employment_type"] or "") == "contract":
        out.append("contract ", style="cyan")
    return out or Text("—", style="dim")


# Stages that mean the application is actually out the door.
APPLIED_STAGES = {
    Stage.SUBMITTED, Stage.ACKNOWLEDGED, Stage.SCREENING,
    Stage.INTERVIEW, Stage.OFFER, Stage.REJECTED,
}


def _reason_gist(rule_reason: str | None) -> str:
    """The rejection reason, collapsed to something groupable.

    The stored reason embeds the specific regex that matched, so 2,000
    rejections read as 2,000 distinct strings. The category is what you want
    when deciding whether the filter is too tight.
    """
    if not rule_reason:
        return "—"
    text = rule_reason.split(";")[0].strip()
    if "title excluded" in text:
        return "title excluded"
    if "no include pattern" in text:
        return "title matched no include pattern"
    if "needs visa/reloc/contract" in text:
        return "out of region, no mobility support"
    if "out of scope" in text:
        return "country out of scope"
    if "below floor" in text:
        return "salary below floor"
    if "dealbreaker" in text:
        return "JD contains a dealbreaker phrase"
    if "blocked list" in text:
        return "company on the blocked list"
    return text[:48]


def _applied_text(row) -> Text:
    """Has this been sent, and when.

    The stage column already encodes this, but not obviously: nothing about
    "awaiting_approval" says "filled in but never sent", which is the state
    that actually matters when you are deciding what to do next.
    """
    try:
        stage = Stage(row["stage"])
    except (ValueError, KeyError):
        return Text("—", style="dim")

    if stage in APPLIED_STAGES:
        when = (row["submitted_at"] or "")[:10]
        return Text(f"yes {when}".strip(), style="green")
    if stage is Stage.AWAITING_APPROVAL:
        return Text("filled", style="yellow")
    if stage is Stage.FAILED:
        return Text("failed", style="red")
    if stage is Stage.WITHDRAWN:
        return Text("dropped", style="dim")
    return Text("no", style="dim")


def _truly_global(row) -> bool:
    """Global-remote per the location text, and not contradicted by the JD.

    Job boards populate their region field carelessly. WeWorkRemotely listed a
    Temporal role as "Anywhere in the World" while the description opened with
    "United States or Canada - Remote Opportunity" — and the scorer had already
    recorded hiring_countries as US/CA. A posting that names the countries it
    hires into is not location-independent, whatever the board's metadata says,
    so the scorer's read wins over the board's.
    """
    from .geo import is_global_remote

    if not is_global_remote(row["location"], row["description"]):
        return False
    return not _json_list(row["hiring_countries"])


def _reachable(row, prefs) -> bool:
    """Could this role be taken without an employer moving you somewhere new?

    True when the location names a country in `locations.regions`, or names
    nowhere at all. A posting whose location is unparseable counts as
    reachable rather than being hidden — the parse is not always right, and
    silently dropping a role is worse than showing one that needs a look.
    """
    from .geo import detect_countries, expand_regions, is_location_neutral

    if is_location_neutral(row["location"]):
        return True
    found = detect_countries(row["location"])
    if not found:
        return True
    return bool(set(found) & expand_regions(prefs.regions, prefs.extra_countries))


def _json_list(raw) -> list[str]:
    import json
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _require_job(conn, job_id: int):
    row = db.application_detail(conn, job_id)
    if row is None:
        console.print(f"[red]No tracked application for job {job_id}.[/]")
        console.print("Run [bold]shotgun list[/] to see what exists.")
        raise typer.Exit(1)
    return row


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=verbose)],
    )
    if not verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)


@app.callback()
def main(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    _setup_logging(verbose)
    db.init()


# ------------------------------------------------------------------ profile

@profile_app.command("init")
def profile_init(
    resume: Annotated[Path, typer.Argument(help="Your master resume (PDF, DOCX, or TXT)")],
) -> None:
    """Parse a resume into private/profile.yaml."""
    if not resume.exists():
        console.print(f"[red]No such file:[/] {resume}")
        raise typer.Exit(1)

    console.print(f"Reading {resume.name}…")
    parsed = profile_mod.parse_resume(resume)
    carried, backup = profile_mod.carry_manual_fields(parsed)
    path = profile_mod.save(parsed)

    console.print(f"[green]Wrote[/] {path}")
    console.print(
        f"  {len(parsed.roles)} roles, {len(parsed.skills)} skills, "
        f"{len(parsed.certifications)} certifications"
    )

    if carried:
        console.print(f"  [green]carried over[/] {', '.join(carried)}")
    if backup:
        # The fields survive; the prose does not. `load_yaml` sends this file
        # to the model verbatim, so any notes written into it were part of the
        # scoring prompt and are now only in the backup.
        console.print(
            f"  [dim]previous profile saved to {backup.name} — comments in it "
            f"were part of the scoring prompt and are not carried over[/]"
        )

    missing = [f for f in profile_mod.MANUAL_FIELDS if not getattr(parsed, f, None)]
    if missing:
        console.print(
            "\n[yellow]Now open it and check the parse[/], then fill in the "
            "fields no resume contains:\n  " + "\n  ".join(missing)
        )
    else:
        console.print("\n[yellow]Open it and check the parse.[/]")


@profile_app.command("template")
def profile_template() -> None:
    """Write an empty profile to fill in by hand."""
    path = profile_mod.save(profile_mod.template())
    console.print(f"[green]Wrote[/] {path} — fill it in.")


@profile_app.command("show")
def profile_show() -> None:
    """Print the current profile."""
    prof = profile_mod.load()
    console.print(f"[bold]{prof.contact.name}[/] · {prof.contact.email}")
    if prof.headline:
        console.print(f"  {prof.headline}")
    console.print(f"\n[bold]Roles[/] ({len(prof.roles)})")
    for role in prof.roles:
        console.print(f"  {role.title} @ {role.company} · {role.start}–{role.end or 'present'}")
    console.print(f"\n[bold]Skills[/] {', '.join(prof.skills[:20])}")
    if not prof.work_authorization:
        console.print("\n[yellow]work_authorization is empty[/] — forms will ask about it.")


# ---------------------------------------------------------------- pipeline

@app.command()
def discover(
    source: Annotated[
        list[str] | None,
        typer.Option("--source", "-s", help=f"Limit to one source: {', '.join(pipeline.SOURCES)}"),
    ] = None,
) -> None:
    """Fetch postings from the configured sources."""
    prefs = Preferences.load()
    names = ", ".join(source) if source else "all sources"
    with console.status(f"fetching {names}…", spinner="dots"):
        stats = pipeline.discover(prefs, only=source)
    console.print(
        f"[green]{stats['new']} new[/] · {stats['duplicates']} already known "
        f"· {stats['found']} fetched"
        + (f" · [red]{stats['errors']} errors[/]" if stats.get("errors") else "")
    )
    if stats["new"]:
        console.print("Next: [bold]shotgun rank[/] to score them.")


@app.command()
def sweep(
    company: Annotated[
        list[str] | None,
        typer.Option("--company", "-c",
                     help="Limit to these board tokens, e.g. -c canonical "
                          "-c duck-duck-go. Default: every configured board."),
    ] = None,
    any_level: Annotated[
        bool,
        typer.Option("--any-level/--strict",
                     help="Count any security, privacy or detection title, at any "
                          "level, for every company swept — not only the ones on "
                          "titles.any_level. --strict uses the normal filter."),
    ] = True,
    anywhere: Annotated[
        bool,
        typer.Option("--anywhere",
                     help="Only location-neutral roles: remote, and naming no "
                          "country or region. Drops 'Remote (United States)' "
                          "and 'Remote - EMEA', keeps 'Remote' and 'Worldwide'."),
    ] = False,
    save: Annotated[
        bool,
        typer.Option("--save", help="Also store what was fetched, so `rank` can score it."),
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option("--quiet/--no-quiet",
                     help="List the live boards that have no security role open."),
    ] = False,
) -> None:
    """Ask the ATS boards directly what security roles they have open.

    No scoring and no model calls — this is the short question, answered in
    seconds, about companies you picked deliberately.
    """
    prefs = Preferences.load()
    label = ", ".join(company) if company else "every configured board"
    with console.status(f"sweeping {label}…", spinner="dots"):
        found = pipeline.sweep(prefs, company, any_level=any_level,
                               neutral_only=anywhere, store=save)

    boards = f"{found.boards} board" + ("s" if found.boards != 1 else "")
    tied = (f" · [dim]{found.tied_down} named a country or region[/]"
            if found.tied_down else "")
    if not found.roles:
        console.print(
            f"No {'location-neutral ' if anywhere else ''}security roles open "
            f"across {boards} ({found.postings} postings read).{tied}"
        )
    else:
        table = Table(box=None, pad_edge=False, header_style="bold")
        table.add_column("company", style="bold")
        table.add_column("title")
        table.add_column("location")
        table.add_column("", width=1)
        for role in found.roles:
            # A tick reads better than an R once the list is only ever remote.
            flag = ("[green]✓[/]" if anywhere
                    else Text("R", style="green") if role.remote else "")
            table.add_row(role.company, role.title, role.location or "—", flag)
        console.print(table)
        console.print(
            f"\n[green]{len(found.roles)} "
            f"{'location-neutral ' if anywhere else ''}security roles[/] across "
            f"{boards} · {found.postings} postings read{tied}"
            + ("  [dim](R = remote)[/]"
               if not anywhere and any(r.remote for r in found.roles) else "")
        )

    if quiet and found.quiet:
        console.print(f"\n[dim]live, nothing open: {', '.join(sorted(found.quiet))}[/]")
    if found.failed:
        console.print(
            "\n[red]boards that did not answer[/] — check the token:\n  "
            + "\n  ".join(f"{name}: {err}" for name, err in sorted(found.failed))
        )
    if found.roles and not save:
        console.print("[dim]Add --save to store these, then `shotgun rank`.[/]")


@app.command()
def probe(
    limit: Annotated[
        int,
        typer.Option(help="Max candidate tokens to try. 0 for all."),
    ] = 300,
    token: Annotated[
        list[str] | None,
        typer.Option("--token", "-t", help="Probe these tokens instead of "
                                           "deriving them from stored companies."),
    ] = None,
    security_only: Annotated[
        bool,
        typer.Option("--security-only/--all-boards",
                     help="Only report boards that have a security role open."),
    ] = True,
) -> None:
    """Find ATS boards you don't have yet, from companies already in the corpus.

    Companies advertising on the remote boards are remote-friendly by
    definition — the Supabase and DuckDuckGo profile — and a public ATS board
    beats an aggregator listing every time: full descriptions, real
    locations, a direct apply URL. Their board tokens are never published, so
    this tries the plausible spellings of their names.

    Free, and each candidate costs three HTTP requests. Prints a YAML block
    to paste into `sources.ats_boards`.
    """
    prefs = Preferences.load()

    if token:
        # Not lowercased: board tokens are case-sensitive, and "Ubiminds"
        # is a live Lever board while "ubiminds" is a 404.
        candidates = [t.strip() for t in token if t.strip()]
    else:
        with db.connect() as conn:
            candidates = pipeline.candidate_tokens(conn, prefs)
        console.print(f"[dim]{len(candidates)} candidate tokens derived from "
                      f"companies already seen[/]")
        if limit:
            candidates = candidates[:limit]

    console.print(f"probing {len(candidates)} tokens x 3 boards "
                  f"= {len(candidates) * 3} requests…")
    with console.status("probing…", spinner="dots"):
        found = pipeline.probe_candidates(prefs, candidates)

    interesting = [f for f in found if f.security] if security_only else found
    if not interesting:
        console.print(
            f"No new boards found. [dim]{len(found)} answered but had no "
            f"security role open — --all-boards lists them.[/]"
            if found and security_only else "No live boards among these candidates."
        )
        return

    for f in interesting:
        tag = f"  [green]{f.neutral} location-neutral[/]" if f.neutral else ""
        console.print(f"\n[bold]{f.board}:{f.token}[/] ({f.company}) — "
                      f"{f.security}/{f.postings} security{tag}")
        for title, loc in f.titles:
            console.print(f"    {title[:58]:60} [dim]{loc[:34]}[/]")

    console.print(f"\n[green]{len(interesting)} new board(s)[/] with a security "
                  f"role open, out of {len(found)} live")
    console.print("\n[dim]paste into sources.ats_boards in preferences.yaml:[/]")
    by_board: dict[str, list[str]] = {}
    for f in interesting:
        by_board.setdefault(f.board, []).append(f.token)
    for board, tokens in sorted(by_board.items()):
        console.print(f"    {board}:")
        for t in sorted(tokens):
            console.print(f"      - {t}")


@app.command()
def mobility(
    stance: Annotated[
        str,
        typer.Option("--stance", "-s",
                     help="offers | refuses | silent | unreadable | all. "
                          "Default: offers."),
    ] = "offers",
    in_region: Annotated[
        bool,
        typer.Option("--in-region", help="Only roles in your target regions."),
    ] = False,
    limit: Annotated[int, typer.Option(help="Max rows; 0 for no limit.")] = 0,
) -> None:
    """Which companies say they will sponsor a visa or pay to relocate you.

    Read from each posting's own text, free, and covering every stored
    security role rather than only the scored ones.

    Silence is not refusal, and the summary keeps the two apart. Most
    employers never mention sponsorship at all — reading that as "no" would
    discard most of the market — so `silent` is its own bucket, and
    `unreadable` means the board gave us no description to read, which is a
    gap in our data rather than an answer from them.
    """
    from . import score as score_mod

    prefs = Preferences.load()
    with db.connect() as conn, console.status("reading postings…", spinner="dots"):
        roles = score_mod.mobility_report(conn, prefs)

    counts = Counter(r["stance"] for r in roles)
    summary = Text()
    summary.append(f"{len(roles):,} security roles stored\n", style="bold")
    for name, style, gloss in (
        ("offers", "green", "state a visa or relocation offer"),
        ("refuses", "red", "explicitly rule sponsorship out"),
        ("silent", "yellow", "say nothing — not a refusal"),
        ("unreadable", "dim", "no description published to read"),
    ):
        summary.append(f"  {counts.get(name, 0):>5}  ", style=style)
        summary.append(f"{name:<11}{gloss}\n")
    console.print(summary)

    wanted = [r for r in roles if stance == "all" or r["stance"] == stance]
    if in_region:
        wanted = [r for r in wanted if r["in_region"]]
    if not wanted:
        console.print(f"[dim]nothing matches --stance {stance}"
                      f"{' --in-region' if in_region else ''}[/]")
        return

    companies = {r["company"] for r in wanted}
    mixed = stance == "all"

    # Ordered so the roles that state something come first. Grouping by
    # company alone put the largest employer at the top, which meant `--stance
    # all` opened on twelve Bosch project coordinators while the sponsoring
    # roles sat 300 rows down. Stance leads; company size only breaks ties, so
    # "who will move me" still reads as a question about employers.
    rank_of = {name: i for i, name in enumerate(score_mod.MOBILITY_STANCES)}
    by_company = Counter(r["company"] for r in wanted)
    wanted.sort(key=lambda r: (
        rank_of.get(r["stance"], 9),
        not r["in_region"],
        -by_company[r["company"]],
        r["company"].lower(),
        r["title"].lower(),
    ))

    table = Table(box=None, pad_edge=False, header_style="bold")
    table.add_column("id", justify="right", style="dim")
    if mixed:
        # Without this, `silent` and `unreadable` are both an empty `says`
        # column and indistinguishable — which would undo the whole reason
        # for keeping them apart.
        table.add_column("stance")
    table.add_column("company", style="bold")
    table.add_column("title")
    table.add_column("location")
    table.add_column("says")

    stance_style = {"offers": "green", "refuses": "red",
                    "silent": "yellow", "unreadable": "dim"}

    rows = wanted[:limit] if limit else wanted
    for role in rows:
        says = []
        if role["sponsorship"] is visa.Support.YES:
            says.append("[green]visa[/]")
        if role["sponsorship"] is visa.Support.NO:
            says.append("[red]no visa[/]")
        if role["relocation"] is visa.Support.YES:
            says.append("[green]reloc[/]")
        if role["employment"] is visa.Employment.CONTRACT:
            says.append("[cyan]contract[/]")

        cells = [str(role["job_id"])]
        if mixed:
            cells.append(Text(role["stance"],
                              style=stance_style.get(role["stance"], "white")))
        cells += [
            ("★ " if role["in_region"] else "") + role["company"][:20],
            role["title"][:46],
            (role["location"] or "—")[:28],
            " ".join(says) or "—",
        ]
        table.add_row(*cells)

    console.print(table)
    console.print(
        f"\n[green]{len(wanted)} role(s)[/] across "
        f"[bold]{len(companies)}[/] companies"
        + (f", showing {len(rows)}" if len(rows) < len(wanted) else "")
        + "   [dim]★ = in your target regions[/]"
    )
    hint = "shotgun show <id> for one"
    if not mixed:
        hint += " · --stance all for every bucket in one table"
    console.print(f"[dim]{hint}[/]")


@app.command()
def rank(
    limit: Annotated[int, typer.Option(help="Max postings to score")] = 200,
    rules_only: Annotated[bool, typer.Option(help="Skip Claude, rule filter only")] = False,
    refilter: Annotated[
        bool,
        typer.Option("--refilter",
                     help="Re-run the rule filter over postings it rejected before. "
                          "Use after editing preferences.yaml."),
    ] = False,
    authorised_only: Annotated[
        bool,
        typer.Option("--authorised-only",
                     help="Only score roles in countries you can work in without "
                          "sponsorship, per profile work_authorization."),
    ] = False,
    rebucket: Annotated[
        bool,
        typer.Option("--rebucket",
                     help="Re-apply scoring.minimum_score to scores you already paid "
                          "for. Free — no model calls."),
    ] = False,
    reprioritise: Annotated[
        bool,
        typer.Option("--reprioritise",
                     help="Recompute priority for every scored posting from the "
                          "current priority weights. Use after editing company or "
                          "country bonuses. Free — no model calls."),
    ] = False,
) -> None:
    """Score unscored postings against your profile."""
    prefs = Preferences.load()
    profile_yaml = profile_mod.load_yaml()

    from . import score as score_mod

    if reprioritise:
        with db.connect() as conn:
            moved = score_mod.reprioritise(conn, prefs)
        console.print(
            f"[green]{moved['changed']} reprioritised[/] · "
            f"{moved['unchanged']} unchanged"
            + (f" · {moved['signals_changed']} re-read their visa signals"
               if moved.get("signals_changed") else "")
        )
        if moved["changed"]:
            console.print("Next: [bold]shotgun queue[/] to see the new order")
        return

    if rebucket:
        with db.connect() as conn:
            moved = score_mod.rebucket(conn, prefs)
        console.print(
            f"threshold {prefs.minimum_score}: "
            f"[green]{moved['to_queued']} -> queued[/] · "
            f"{moved['to_low']} -> scored_low · {moved['unchanged']} unchanged"
        )
        if moved["to_queued"]:
            console.print("Next: [bold]shotgun queue[/]")
        return

    if refilter and limit == 200:
        # The rule filter is free; only survivors cost anything. A 200-row cap
        # would leave most of the backlog unexamined.
        limit = 100_000
        console.print("[dim]--refilter: raising the cap to cover the whole backlog "
                      "(the rule pass is free; only survivors reach Claude)[/]")

    from . import score as score_mod

    prof = profile_mod.load()
    if authorised_only:
        allowed = score_mod.authorised_countries(prof)
        console.print(f"[dim]--authorised-only: {len(allowed)} countries "
                      f"({', '.join(sorted(allowed)[:8])}…)[/]")

    with db.connect() as conn:
        tally = score_mod.score_pending(
            conn, prefs, profile_yaml, limit=limit, use_llm=not rules_only,
            profile=prof, refilter=refilter,
            only_countries=(
                score_mod.authorised_countries(prof) if authorised_only else None
            ),
        )

    console.print(
        f"[green]{tally['queued']} queued[/] · {tally['low']} scored low "
        f"· {tally['filtered']} filtered by rules · {tally['errors']} errors"
    )
    if tally.get("out_of_credit"):
        console.print(
            "\n[red]Stopped: no credit left on this provider's account.[/] "
            "Everything scored so far is saved — top up, or switch provider in "
            ".env, and run the same command again to resume.\n"
            "[dim]shotgun rank --rules-only[/] still works and costs nothing."
        )
    if tally["queued"]:
        console.print("Next: [bold]shotgun queue[/] to see them, "
                      "[bold]shotgun show <id>[/] for one.")


@app.command()
def prepare(
    limit: Annotated[int | None, typer.Option(help="Override apply.max_per_run")] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Generate documents, don't open forms"),
    ] = False,
    job_id: Annotated[
        int | None,
        typer.Option("--job-id", help="Prepare exactly one job. Use this to test a filler."),
    ] = None,
    reuse_docs: Annotated[
        bool,
        typer.Option("--reuse-docs",
                     help="Fill the form with documents already on disk. No model calls."),
    ] = False,
) -> None:
    """Tailor resumes and fill forms for queued jobs. Stops before submit."""
    prefs = Preferences.load()
    if reuse_docs and job_id is None:
        console.print("[red]--reuse-docs needs --job-id[/] — it reuses the "
                      "documents recorded for one specific application.")
        raise typer.Exit(1)

    outcomes = pipeline.prepare_queued(
        prefs, limit=limit, dry_run=dry_run, job_id=job_id, reuse_docs=reuse_docs,
    )

    if not outcomes:
        console.print("Nothing queued. Run [bold]shotgun discover[/] then [bold]shotgun rank[/].")
        return

    for outcome in outcomes:
        status = outcome.get("status", "?")
        colour = {
            "awaiting_approval": "green",
            "documents_only": "cyan",
        }.get(status, "red")
        console.print(f"[{colour}]{status}[/] {outcome['label']}")
        if outcome.get("detail"):
            console.print(f"    {outcome['detail']}")
        if outcome.get("unanswered"):
            questions = outcome["unanswered"][:5]
            console.print(f"    [yellow]needs you:[/] {'; '.join(questions)}")


@app.command()
def run(
    skip_discovery: Annotated[bool, typer.Option(help="Use already-stored postings")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """discover -> rank -> prepare, in one go."""
    prefs = Preferences.load()
    report = pipeline.run(prefs, skip_discovery=skip_discovery, dry_run=dry_run)

    if "discovery" in report:
        d = report["discovery"]
        console.print(f"Discovered {d['new']} new of {d['found']} fetched")

    s = report["scoring"]
    console.print(f"Scored: {s['queued']} queued, {s['low']} low, {s['filtered']} filtered")
    console.print(f"Prepared {len(report['prepared'])} applications")
    console.print("\nRun [bold]shotgun review[/] to approve and submit.")


# ------------------------------------------------------------------ review

@app.command()
def review() -> None:
    """Walk the approval queue: reopen each filled form so you can submit it."""
    from . import apply as apply_mod

    prof = profile_mod.load()

    with db.connect() as conn:
        pending = db.applications_by_stage(conn, Stage.AWAITING_APPROVAL)
        answer_rows = db.list_answers(conn)
        # Everything, for showing you next to the form.
        stored = {r["key"]: r["value"] for r in answer_rows}
        # Only the non-sensitive confirmed ones get typed into a third-party
        # page, matching pipeline._stored_answers(). Recording a salary figure
        # is one decision; replaying it into an arbitrary form is another.
        fillable = {
            r["key"]: r["value"] for r in answer_rows
            if not r["sensitive"] and r["confidence"] == "confirmed"
        }
        unanswered_by_job = {
            row["job_id"]: (row["detail"] or "")
            for row in conn.execute(
                "SELECT job_id, detail FROM events WHERE kind = 'unanswered' "
                "ORDER BY id DESC"
            ).fetchall()
        }

    if not pending:
        console.print("Approval queue is empty.")
        console.print("Run [bold]shotgun prepare[/] to fill forms for queued roles.")
        return

    console.print(f"[bold]{len(pending)} application(s) awaiting approval[/]\n")

    for app_row in pending:
        console.print(f"[bold cyan]{app_row['title']}[/] @ [bold]{app_row['company']}[/]")
        console.print(f"  score {app_row['score']} · {app_row['level']} · {app_row['location']}")
        console.print(f"  resume: {app_row['resume_path'] or '[red]none[/]'}")
        console.print(f"  {app_row['apply_url'] or app_row['url']}")

        # Questions the filler could not place, paired with a stored answer
        # where one matches. Greenhouse names its custom questions
        # `job_application[answers_attributes][0][text_value]`, so there is
        # nothing semantic for a selector to match — but you still need the
        # answer in front of you while you finish the form by hand.
        raw = unanswered_by_job.get(app_row["job_id"], "")
        questions = [q.strip() for q in raw.split(";") if q.strip()]
        if questions:
            console.print(f"\n  [yellow]{len(questions)} question(s) the filler left blank[/]")
            for question in questions[:12]:
                hit = answers_mod.match_for(question, stored)
                if hit:
                    console.print(f"    {question}")
                    console.print(f"      [green]→ {hit[1]}[/]  [dim]({hit[0]})[/]")
                else:
                    console.print(f"    {question}  [dim](no stored answer)[/]")
            if not stored:
                console.print("    [dim]run `shotgun answers init` so these come "
                              "pre-answered next time[/]")

        choice = typer.prompt(
            "\n  [o]pen and submit / [s]kip / [r]eject / [q]uit", default="s"
        ).strip().lower()

        if choice == "q":
            break
        if choice == "r":
            with db.connect() as conn:
                db.set_stage(conn, app_row["job_id"], Stage.WITHDRAWN,
                             note="rejected at review")
            continue
        if choice != "o":
            continue

        # A stage can be moved to awaiting_approval by hand from the web UI,
        # in which case there is no resume on disk and Path(None) would raise.
        if not app_row["resume_path"]:
            console.print("  [red]No resume recorded[/] — run "
                          f"[bold]shotgun prepare --job-id {app_row['job_id']}[/] first.")
            continue
        resume = Path(app_row["resume_path"])
        if not resume.exists():
            console.print(f"  [red]Resume is gone:[/] {resume}")
            continue

        cover = None
        if app_row["cover_path"] and Path(app_row["cover_path"]).exists():
            cover = Path(app_row["cover_path"]).read_text()

        # Refill the form in a visible browser and hand it over. Pass the
        # stored answers — this used to send `{}`, discarding the whole store
        # on the one pass where a human is watching.
        result = apply_mod.prepare(
            app_row["apply_url"] or app_row["url"],
            prof,
            resume,
            cover,
            fillable,
            keep_open=True,
        )

        submitted = typer.confirm("  Did you submit it?", default=False)
        with db.connect() as conn:
            if submitted:
                db.set_stage(conn, app_row["job_id"], Stage.SUBMITTED,
                             note="submitted at review")
                console.print("  [green]marked submitted[/]")
            else:
                db.set_stage(conn, app_row["job_id"], Stage.AWAITING_APPROVAL,
                             note=f"left in queue ({result.summary()})")


# ----------------------------------------------------------------- tracking

@app.command()
def show(
    job_id: Annotated[int, typer.Argument(help="Job id from `shotgun list` or `shotgun queue`")],
    jd: Annotated[bool, typer.Option("--jd", help="Include the full job description")] = False,
) -> None:
    """Everything known about one posting: score, reasoning, docs, history."""
    with db.connect() as conn:
        row = _require_job(conn, job_id)
        events = db.events_for(conn, job_id)
        stored = {r["key"]: r["value"] for r in db.list_answers(conn)}

    prefs = Preferences.load()

    header = Text()
    header.append(f"{row['title']}\n", style="bold cyan")
    header.append(f"{row['company']}", style="bold")
    if row["location"]:
        header.append(f"  ·  {row['location']}")
    if row["remote"]:
        header.append("  ·  remote", style="green")
    console.print(Panel(header, border_style="cyan", padding=(0, 1)))

    facts = Table.grid(padding=(0, 2))
    facts.add_column(style="dim", justify="right")
    facts.add_column()
    facts.add_row("stage", _stage_text(row["stage"]))
    facts.add_row("score", _score_text(row["score"], prefs.minimum_score))
    from . import score as score_mod
    _, boosts = score_mod.preference_bonus(row, prefs)
    prio = str(row["priority"]) if row["priority"] is not None else "—"
    # Say which of the bonuses this posting earned, not just the total it came
    # to — a priority above the score is otherwise unexplained.
    facts.add_row("priority", f"{prio}  ({', '.join(boosts)})" if boosts else prio)
    facts.add_row("level", row["level"] or "—")
    facts.add_row("mobility", _mobility_flags(row))
    countries = _json_list(row["hiring_countries"])
    if countries:
        facts.add_row("hires into", ", ".join(countries))
    facts.add_row("ats", row["ats"] or "unknown")
    facts.add_row("apply url", row["apply_url"] or row["url"])
    if row["posted_at"]:
        facts.add_row("posted", str(row["posted_at"])[:10])
    console.print(facts)

    if row["reasoning"]:
        console.print(Panel(row["reasoning"], title="why this score",
                            border_style="dim", padding=(0, 1)))

    for label, items, style in (
        ("dealbreakers", _json_list(row["dealbreakers"]), "red"),
        ("concerns", _json_list(row["concerns"]), "yellow"),
        ("requirements the resume must hit", _json_list(row["key_requirements"]), "cyan"),
    ):
        if items:
            console.print(f"\n[{style}]{label}[/]")
            for item in items:
                console.print(f"  • {item}")

    if row["rule_reason"]:
        console.print(f"\n[dim]rule filter: {row['rule_reason']}[/]")

    if row["resume_path"] or row["cover_path"]:
        console.print("\n[bold]documents[/]")
        for label, path in (("resume", row["resume_path"]), ("cover", row["cover_path"])):
            if path:
                exists = "" if Path(path).exists() else "  [red](missing)[/]"
                console.print(f"  {label}: {path}{exists}")

    if events:
        console.print("\n[bold]history[/]")
        for event in events[:12]:
            when = (event["at"] or "")[:16].replace("T", " ")
            detail = (event["detail"] or "").replace("\n", " ")[:66]
            line = Text(f"  {when}  ", style="dim")
            line.append(event["kind"])
            if detail:
                line.append(f"  {detail}…", style="dim")
            console.print(line, overflow="ellipsis", no_wrap=True)

    if stored:
        console.print(f"\n[dim]{len(stored)} stored answer(s) available — "
                      f"see [bold]shotgun answers list[/][/]")
    else:
        console.print("\n[yellow]No stored answers.[/] Run "
                      "[bold]shotgun answers init[/] so forms come back mostly filled.")

    if jd and row["description"]:
        console.print(Panel(row["description"][:6000], title="job description",
                            border_style="dim"))
    elif row["description"]:
        console.print(f"\n[dim]{len(row['description'])} chars of JD — "
                      f"add --jd to print it[/]")


@app.command()
def queue(
    limit: Annotated[int, typer.Option(help="Max rows")] = 25,
    anywhere: Annotated[
        bool,
        typer.Option("--anywhere",
                     help="Only roles that claim they can be done from anywhere"),
    ] = False,
) -> None:
    """The apply queue, in the order `shotgun prepare` will work through it."""
    with db.connect() as conn:
        rows = db.applications_by_stage(conn, Stage.QUEUED)
    if anywhere:
        rows = [r for r in rows if _truly_global(r)]
    rows = rows[:limit]

    if not rows:
        if anywhere:
            console.print("No location-independent roles in the queue.")
            console.print("[dim]The remote-first boards are where these live: "
                          "shotgun discover -s remote_boards[/]")
        else:
            console.print("Queue is empty. Run [bold]shotgun discover[/] then "
                          "[bold]shotgun rank[/].")
        return

    prefs = Preferences.load()
    table = Table(title=f"apply queue · {len(rows)} role(s)", title_style="bold")
    table.add_column("id", justify="right", style="dim", no_wrap=True)
    table.add_column("score", justify="right", no_wrap=True)
    table.add_column("pri", justify="right", style="dim", no_wrap=True)
    table.add_column("title", no_wrap=True, overflow="ellipsis")
    table.add_column("company", no_wrap=True, overflow="ellipsis")
    table.add_column("location", no_wrap=True, overflow="ellipsis")
    table.add_column("mobility", no_wrap=True)
    table.add_column("applied", no_wrap=True, min_width=7)
    table.add_column("!", justify="right", style="yellow", no_wrap=True)

    for row in rows:
        concerns = len(_json_list(row["concerns"]))
        table.add_row(
            str(row["job_id"]),
            _score_text(row["score"], prefs.minimum_score),
            str(row["priority"]) if row["priority"] is not None else "—",
            (row["title"] or "")[:42],
            (row["company"] or "")[:16],
            (row["location"] or "—")[:22],
            _mobility_flags(row),
            _applied_text(row),
            str(concerns) if concerns else "",
        )
    console.print(table)
    console.print("[dim]shotgun show <id>  ·  shotgun prepare --job-id <id> --dry-run[/]")


@app.command("open")
def open_job(
    job_id: Annotated[int, typer.Argument(help="Job id")],
    what: Annotated[str, typer.Option(help="posting | resume | cover")] = "posting",
) -> None:
    """Open the posting, the tailored resume, or the cover letter."""
    import webbrowser

    with db.connect() as conn:
        row = _require_job(conn, job_id)

    target = {
        "posting": row["apply_url"] or row["url"],
        "resume": row["resume_path"],
        "cover": row["cover_path"],
    }.get(what)

    if not target:
        console.print(f"[red]No {what} for job {job_id}.[/]")
        if what != "posting":
            console.print("Generate one with "
                          f"[bold]shotgun prepare --job-id {job_id} --dry-run[/]")
        raise typer.Exit(1)

    if what != "posting":
        if not Path(target).exists():
            console.print(f"[red]File is gone:[/] {target}")
            raise typer.Exit(1)
        target = Path(target).resolve().as_uri()

    console.print(f"Opening {what}: {target}")
    webbrowser.open(target)


@app.command()
def draft(
    job_id: Annotated[int, typer.Argument(help="Job id whose form left questions blank")],
    save_all: Annotated[
        bool, typer.Option("--yes", help="Save every draft without asking")
    ] = False,
) -> None:
    """Draft answers to the free-text questions a form left blank.

    These are what stop a form being submittable: "tell us about your
    experience working async/remote" is not in any resume, but it is
    answerable from the profile. Drafts go into the answer store keyed to the
    question, so the filler can place them next run and so the same question
    at another company is already answered.
    """
    from . import tailor as tailor_mod

    with db.connect() as conn:
        row = _require_job(conn, job_id)
        event = conn.execute(
            "SELECT detail FROM events WHERE job_id = ? AND kind = 'unanswered' "
            "ORDER BY id DESC LIMIT 1", (job_id,),
        ).fetchone()
        stored = {r["key"]: r["value"] for r in db.list_answers(conn)}

    questions = [q.strip() for q in (event["detail"] if event else "").split(";") if q.strip()]
    # Only the free-text ones worth drafting. Short labels are plain fields
    # the filler should be placing itself, not essays.
    questions = [q for q in questions if len(q) > 40]
    if not questions:
        console.print("No free-text questions recorded for this job.")
        console.print(f"[dim]Run `shotgun prepare --job-id {job_id} --reuse-docs` first.[/]")
        return

    profile_yaml = profile_mod.load_yaml()
    console.print(f"[bold]{len(questions)} question(s)[/] for "
                  f"{row['title']} @ {row['company']}\n")

    saved = 0
    for index, question in enumerate(questions, 1):
        key = answers_mod.selector_key(
            "q_" + "_".join(question.lower().split()[:4])
        )
        if stored.get(key):
            console.print(f"[dim]{index}. already answered ({key}), skipping[/]")
            continue

        console.print(f"[bold cyan]{index}/{len(questions)}[/] {question}")
        with console.status("drafting…", spinner="dots"):
            try:
                text = tailor_mod.draft_answer(question, row, profile_yaml)
            except Exception as exc:
                console.print(f"   [red]draft failed:[/] {type(exc).__name__}: {exc}")
                continue

        console.print(Panel(text, border_style="cyan", padding=(0, 1)))
        console.print(f"[dim]{len(text.split())} words · would be stored as {key}[/]")

        keep = save_all or typer.confirm("  Save this answer?", default=True)
        if keep:
            with db.connect() as conn:
                db.put_answer(conn, key, question, text)
            saved += 1
            console.print("  [green]saved[/]\n")
        else:
            console.print("  [dim]discarded[/]\n")

    console.print(f"[green]{saved} saved[/] — they will be filled on the next "
                  f"[bold]shotgun prepare --job-id {job_id} --reuse-docs[/]")


@app.command()
def retry(
    job_id: Annotated[list[int] | None, typer.Argument(help="Job ids to requeue")] = None,
    stage: Annotated[
        str | None,
        typer.Option(help="Requeue everything in this stage, e.g. failed, tailoring"),
    ] = None,
) -> None:
    """Put failed or stuck applications back in the queue.

    `failed` and `tailoring` are otherwise dead ends: `prepare` only reads the
    queued stage, so a job that blew up mid-run stays stuck forever.
    """
    if not job_id and not stage:
        console.print("[red]Give job ids or --stage.[/]  e.g. "
                      "[bold]shotgun retry --stage failed[/]")
        raise typer.Exit(1)

    targets = list(job_id or [])
    if stage:
        try:
            target_stage = Stage(stage)
        except ValueError:
            console.print(f"[red]Unknown stage[/] {stage!r}")
            raise typer.Exit(1) from None
        with db.connect() as conn:
            targets += [r["job_id"] for r in db.applications_by_stage(conn, target_stage)]

    targets = list(dict.fromkeys(targets))
    if not targets:
        console.print("Nothing to requeue.")
        return

    with db.connect() as conn:
        for jid in targets:
            if db.job_row(conn, jid) is None:
                console.print(f"[yellow]skipped {jid}[/] — no such job")
                continue
            db.set_stage(conn, jid, Stage.QUEUED, note="requeued via shotgun retry")
            console.print(f"[green]{jid} -> queued[/]")

    console.print(f"\n{len(targets)} requeued. Run [bold]shotgun queue[/] to review.")


@app.command()
def status(
    filtered: Annotated[
        bool,
        typer.Option("--filter", "-f",
                     help="Also run the rule filter over everything stored and "
                          "report how many roles actually match. Free — no model "
                          "calls — but it reads every description, so a few seconds."),
    ] = False,
    why: Annotated[
        bool,
        typer.Option("--why", help="With --filter: break down why the rest were rejected."),
    ] = False,
) -> None:
    """Pipeline counts by stage, and optionally how many roles match your filter."""
    with db.connect() as conn:
        counts = db.stage_counts(conn)

        if not counts:
            console.print("Nothing tracked yet. Start with [bold]shotgun discover[/].")
            return

        table = Table(title="shotgun")
        table.add_column("Stage")
        table.add_column("Count", justify="right")
        for stage in Stage:
            if counts.get(str(stage)):
                table.add_row(str(stage), f"{counts[str(stage)]:,}")
        console.print(table)

        if not filtered:
            console.print("[dim]Add --filter to see how many of these match your "
                          "preferences (free).[/]")
            return

        from . import score as score_mod

        prefs = Preferences.load()
        with console.status("running the rule filter…", spinner="dots"):
            tally = score_mod.filter_tally(conn, prefs)

    console.print(
        f"\nRule filter over [bold]{tally['total']:,}[/] stored postings:\n"
        f"  [green]{tally['passed']:,} match[/] your titles, locations and comp floor\n"
        f"  {tally['rejected']:,} rejected"
    )
    if tally["pending"]:
        console.print(
            f"\n[yellow]{tally['pending']:,} of the matches have never been scored[/] — "
            f"that is what the next [bold]shotgun rank[/] would pay for."
        )
    else:
        console.print("\nEverything that matches has already been scored.")

    if tally["companies"]:
        top = ", ".join(f"{c} ({n})" for c, n in tally["companies"].most_common(8))
        console.print(f"[dim]most matches: {top}[/]")

    if why:
        breakdown = Counter()
        for reason, n in tally["reasons"].items():
            breakdown[_reason_gist(reason)] += n
        rejects = Table(box=None, pad_edge=False, header_style="bold")
        rejects.add_column("rejected because")
        rejects.add_column("count", justify="right")
        for reason, n in breakdown.most_common(12):
            rejects.add_row(reason, f"{n:,}")
        console.print()
        console.print(rejects)


@app.command("list")
def list_applications(
    stage: Annotated[str | None, typer.Option(help="Filter by stage")] = None,
    min_score: Annotated[
        int | None,
        typer.Option("--min-score", help="Only roles Claude scored at least this"),
    ] = None,
    max_score: Annotated[
        int | None,
        typer.Option("--max-score", help="Only roles Claude scored at most this"),
    ] = None,
    anywhere: Annotated[
        bool,
        typer.Option("--anywhere",
                     help="Only roles that claim they can be done from anywhere"),
    ] = False,
    in_region: Annotated[
        bool,
        typer.Option("--in-region",
                     help="Only roles you could actually take: the location names a "
                          "country in locations.regions, or names nowhere at all. "
                          "Drops the US-only roles that only survive on sponsorship."),
    ] = False,
    near_miss: Annotated[
        bool,
        typer.Option("--near-miss",
                     help="Roles that scored below the threshold but within 20 of it"),
    ] = False,
    limit: Annotated[
        int | None,
        typer.Option(help="Max rows; 0 for no limit. Defaults to all shortlisted."),
    ] = None,
    show_all: Annotated[
        bool,
        typer.Option("--all", help="Include postings the rule filter rejected"),
    ] = False,
    why: Annotated[
        bool,
        typer.Option("--why",
                     help="Show why each was filtered, and a breakdown by reason"),
    ] = False,
) -> None:
    """List tracked applications.

    Rule-filtered postings are hidden unless you ask for them. They are the
    overwhelming majority — 5,787 of 5,851 here — and they are things like
    "Enterprise Account Executive, Brazil" and "Lead Product Designer", never
    scored and never candidates. Showing them by default buried the twelve
    roles that matter.

    `--near-miss` is the other useful one: a role two points under
    `scoring.minimum_score` never reaches the queue, and until you can see it
    you cannot judge whether the threshold or the scorer was wrong.
    """
    prefs = Preferences.load()
    if near_miss:
        floor = prefs.minimum_score
        min_score = max(0, floor - 20) if min_score is None else min_score
        max_score = floor - 1 if max_score is None else max_score

    with db.connect() as conn:
        rows = (
            db.applications_by_stage(conn, Stage(stage))
            if stage
            else db.all_applications(conn)
        )

    if not show_all and not stage:
        # Hide both the rule-filtered and the not-yet-ranked. A `discovered`
        # row carries no judgement at all — it has been fetched and nothing
        # more — so listing 429 of them buries the roles that were actually
        # assessed, which is the same way filtered_out used to.
        hidden = {str(Stage.FILTERED_OUT), str(Stage.DISCOVERED)}
        rows = [r for r in rows if r["stage"] not in hidden]

    if anywhere:
        rows = [r for r in rows if _truly_global(r)]

    if in_region:
        # The point of this filter is a queue you can read after
        # `rank --rules-only`. Without a score, `priority` is nothing but the
        # visa/company/country bonuses, so a US role that sponsors sorts level
        # with a local one and one generous company fills the screen. Location
        # is the one judgement the free pass can still make well.
        rows = [r for r in rows if _reachable(r, prefs)]

    if min_score is not None or max_score is not None:
        rows = [
            r for r in rows
            if r["score"] is not None
            and (min_score is None or r["score"] >= min_score)
            and (max_score is None or r["score"] <= max_score)
        ]
        rows.sort(key=lambda r: r["score"], reverse=True)

    # No cap on the shortlist — it is dozens of rows, and "show me everything
    # I am tracking" is the common question. --all pulls in the ~5,800
    # rule-filtered rows, so that stays capped unless asked otherwise.
    if limit is None:
        limit = 200 if (show_all or stage) else 0

    total = len(rows)
    rows = rows if limit == 0 else rows[:limit]

    if not rows:
        console.print("Nothing to show.")
        return

    table = Table()
    table.add_column("id", justify="right", style="dim", no_wrap=True, min_width=4)
    table.add_column("Stage", no_wrap=True, min_width=10)
    table.add_column("Score", justify="right", no_wrap=True, min_width=5)
    table.add_column("Title", no_wrap=True, overflow="ellipsis", max_width=46)
    table.add_column("Company", no_wrap=True, overflow="ellipsis", max_width=16)
    table.add_column("Location", no_wrap=True, overflow="ellipsis", max_width=22)
    if why:
        # The reason is the whole point when looking at rejects; mobility and
        # applied state are noise there.
        table.add_column("Why", no_wrap=True, overflow="ellipsis")
    else:
        table.add_column("Mobility", no_wrap=True)
        table.add_column("Applied", no_wrap=True, min_width=7)
    for row in rows:
        table.add_row(
            str(row["job_id"]),
            _stage_text(row["stage"]),
            _score_text(row["score"], prefs.minimum_score),
            row["title"] or "",
            row["company"] or "",
            row["location"] or "—",
            *(
                [Text(_reason_gist(row["rule_reason"]), style="dim")] if why
                else [_mobility_flags(row), _applied_text(row)]
            ),
        )
    console.print(table)

    applied = sum(1 for r in rows if Stage(r["stage"]) in APPLIED_STAGES
                  if r["stage"] in {str(x) for x in Stage})
    filled = sum(1 for r in rows if r["stage"] == str(Stage.AWAITING_APPROVAL))
    console.print(
        f"[bold]{len(rows)}[/] shown · [green]{applied} applied[/] · "
        f"[yellow]{filled} filled, not sent[/] · {len(rows) - applied - filled} not started"
    )
    if why:
        import collections
        counts = collections.Counter(_reason_gist(r["rule_reason"]) for r in rows)
        console.print("\n[bold]why these were filtered[/]")
        for reason, count in counts.most_common(12):
            console.print(f"  {count:5}  {reason}")

    if total > len(rows):
        console.print(f"[dim]showing {len(rows)} of {total} — --limit 0 for all[/]")
    if not show_all and not stage:
        console.print("[dim]rule-filtered and not-yet-ranked postings hidden — "
                      "--all includes them, or `shotgun rank` to assess them[/]")
    console.print("[dim]shotgun show <id> · shotgun mark <id> submitted[/]")


@app.command()
def mark(
    job_id: Annotated[int, typer.Argument(help="Job id from `shotgun list`")],
    stage: Annotated[str, typer.Argument(help="New stage, e.g. interview, rejected, offer")],
    note: Annotated[str | None, typer.Option(help="Free-text note")] = None,
) -> None:
    """Update an application's stage as you hear back."""
    try:
        target = Stage(stage)
    except ValueError:
        console.print(f"[red]Unknown stage[/] {stage!r}. Valid: {', '.join(s for s in Stage)}")
        raise typer.Exit(1) from None

    with db.connect() as conn:
        if not db.job_row(conn, job_id):
            console.print(f"[red]No job with id {job_id}[/]")
            raise typer.Exit(1)
        db.set_stage(conn, job_id, target, note=note)

    console.print(f"[green]{job_id} -> {target}[/]")


# ----------------------------------------------------------------- answers

def _answer_state(conn) -> tuple[list, dict[str, str], frozenset[str]]:
    """(rows, key -> value, deliberately-skipped keys)."""
    rows = db.list_answers(conn)
    stored = {r["key"]: r["value"] for r in rows}
    skipped = frozenset(
        r["key"] for r in rows if r["confidence"] == answers_mod.SKIPPED
    )
    return rows, stored, skipped


@answers_app.command("list")
def answers_list() -> None:
    """Show the stored answers, and what is still missing."""
    with db.connect() as conn:
        rows, stored, skipped = _answer_state(conn)

    if rows:
        table = Table(title=f"stored answers · {len(rows)}", title_style="bold")
        table.add_column("key", style="cyan")
        table.add_column("value")
        table.add_column("auto-fill", justify="center")
        table.add_column("updated", style="dim")
        for row in rows:
            if row["confidence"] == answers_mod.SKIPPED:
                state = Text("blank", style="dim")
                value = Text("(left blank on purpose)", style="dim")
            elif row["sensitive"]:
                state = Text("held", style="yellow")
                value = Text((row["value"] or "")[:56])
            else:
                state = Text("yes", style="green")
                value = Text((row["value"] or "")[:56])
            table.add_row(row["key"], value, state, (row["updated_at"] or "")[:10])
        console.print(table)
        console.print("[dim]'held' is recorded but never auto-filled — sensitive "
                      "answers are yours to paste. 'blank' is skipped on purpose.[/]")
    else:
        console.print("[yellow]No answers stored.[/] Nothing will be auto-filled.")

    missing = answers_mod.gaps(stored, skipped)
    if missing:
        console.print(f"\n[yellow]{len(missing)} standard question(s) unanswered:[/] "
                      + ", ".join(q.key for q in missing))
        console.print("Run [bold]shotgun answers init[/] to fill them in, or "
                      "[bold]shotgun answers skip <key>[/] to leave one blank.")
    else:
        console.print("\n[green]Every standard question is answered or "
                      "deliberately blank.[/]")


@answers_app.command("skip")
def answers_skip(
    key: Annotated[str, typer.Argument(help="Answer key to leave blank on purpose")],
) -> None:
    """Mark a question as deliberately blank, so nothing asks for it again."""
    clean = answers_mod.selector_key(key)
    known = answers_mod.BY_KEY.get(clean)
    with db.connect() as conn:
        db.put_answer(
            conn, clean,
            known.question if known else clean,
            "",
            confidence=answers_mod.SKIPPED,
        )
    console.print(f"[green]{clean}[/] left blank on purpose — never auto-filled, "
                  "and `answers init` will not ask again.")
    console.print("[dim]Revisit it with `shotgun answers init --all`.[/]")


@answers_app.command("set")
def answers_set(
    key: Annotated[str, typer.Argument(help="Answer key, e.g. notice_period")],
    value: Annotated[str, typer.Argument(help="The answer")],
    question: Annotated[str | None, typer.Option(help="The question this answers")] = None,
    sensitive: Annotated[
        bool, typer.Option("--sensitive", help="Record it but never auto-fill it")
    ] = False,
) -> None:
    """Record or update one answer."""
    clean = answers_mod.selector_key(key)
    if not clean:
        console.print(f"[red]Unusable key[/] {key!r} — use lowercase letters, "
                      "digits and underscores.")
        raise typer.Exit(1)
    if clean != key:
        console.print(f"[dim]key normalised to {clean!r}[/]")

    known = answers_mod.BY_KEY.get(clean)
    with db.connect() as conn:
        db.put_answer(
            conn, clean,
            question or (known.question if known else clean),
            value,
            sensitive=sensitive or bool(known and known.sensitive),
        )
    console.print(f"[green]{clean}[/] = {value}")

    warning = answers_mod.numeric_warning(clean, value)
    if warning:
        console.print(f"[yellow]![/] {warning}")


@answers_app.command("rm")
def answers_rm(
    key: Annotated[str, typer.Argument(help="Answer key to delete")],
) -> None:
    """Delete one answer."""
    with db.connect() as conn:
        if db.delete_answer(conn, answers_mod.selector_key(key)):
            console.print(f"[green]deleted[/] {key}")
        else:
            console.print(f"[yellow]nothing stored under[/] {key}")


@answers_app.command("init")
def answers_init(
    all_questions: Annotated[
        bool, typer.Option("--all", help="Revisit answers you have already given")
    ] = False,
) -> None:
    """Walk the standard questions, pre-filled from your profile.

    This is mostly confirmation rather than typing: anything the profile
    already knows is offered as the default, so you press Enter through it.
    """
    try:
        prof = profile_mod.load()
    except FileNotFoundError:
        console.print("[red]No profile yet.[/] Run "
                      "[bold]shotgun profile init <resume.pdf>[/] first.")
        raise typer.Exit(1) from None

    suggestions = answers_mod.suggest(prof)

    with db.connect() as conn:
        _, stored, skipped = _answer_state(conn)

    questions = (
        answers_mod.CATALOGUE if all_questions
        else answers_mod.gaps(stored, skipped)
    )
    if not questions:
        console.print("[green]Every standard question is answered or "
                      "deliberately blank.[/] Use --all to revisit them.")
        return

    console.print(Panel(
        "Enter to accept the default, '-' to leave blank, ctrl-c to stop.\n"
        "Leaving one blank is remembered, so you are not asked again — "
        "revisit with --all.",
        title=f"{len(questions)} question(s)", border_style="cyan", padding=(0, 1),
    ))

    saved = left_blank = 0
    for index, question in enumerate(questions, 1):
        default = stored.get(question.key) or suggestions.get(question.key) or ""
        console.print(f"\n[bold cyan]{index}/{len(questions)}[/] {question.question}")
        if question.hint:
            console.print(f"  [dim]{question.hint}[/]")
        if question.sensitive:
            console.print("  [yellow]sensitive — stored but never auto-filled[/]")

        try:
            reply = typer.prompt(f"  {question.key}", default=default or "-",
                                 show_default=bool(default))
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]stopped[/]")
            break

        reply = reply.strip()
        if not reply or reply == "-":
            # Recorded as deliberately blank rather than just passed over, so
            # the next run doesn't ask again. Nothing auto-fills it: only
            # confidence 'confirmed' reaches a form.
            with db.connect() as conn:
                db.put_answer(conn, question.key, question.question, "",
                              confidence=answers_mod.SKIPPED)
            left_blank += 1
            continue

        warning = answers_mod.numeric_warning(question.key, reply)
        if warning:
            console.print(f"  [yellow]![/] {warning}")

        with db.connect() as conn:
            db.put_answer(conn, question.key, question.question, reply,
                          sensitive=question.sensitive)
        saved += 1

    console.print(f"\n[green]{saved} saved[/] · {left_blank} left blank")
    if saved:
        console.print("These now auto-fill on every form that names its fields "
                      "semantically, and print in [bold]shotgun review[/] for the rest.")


@app.command()
def ui(
    port: Annotated[int, typer.Option(help="Port to bind")] = 8765,
    open_browser: Annotated[bool, typer.Option("--open/--no-open")] = True,
) -> None:
    """Launch the local web UI for reviewing and tracking applications."""
    from .web.app import serve

    url = f"http://127.0.0.1:{port}"
    console.print(f"[green]shotgun UI[/] → {url}   (ctrl-c to stop)")
    if open_browser:
        import threading
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    serve(port=port)


@app.command("browser-login")
def browser_login() -> None:
    """Open a browser so you can log into job sites once.

    The session persists in SHOTGUN_BROWSER_PROFILE, so fillers reuse it
    instead of ever handling your credentials.
    """
    from .apply.base import browser_session

    console.print("Opening a browser. Log into whatever you need, then close it.")
    with browser_session(headless=False) as context:
        page = context.new_page()
        page.goto("https://www.linkedin.com/login")
        input("Press Enter when you're done logging in… ")


if __name__ == "__main__":
    app()
