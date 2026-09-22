"""Region -> country expansion and free-text location parsing.

Job boards report location as unstructured text ("Bengaluru, Karnataka, India",
"Remote - EMEA", "London, UK"). This module turns that into an ISO-ish country
code so the location filter can be a set membership test.
"""

from __future__ import annotations

import re

EU_EEA_CH = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR",
    "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK",
    "SI", "ES", "SE", "IS", "LI", "NO", "CH",
}

REGIONS: dict[str, set[str]] = {
    "india": {"IN"},
    "europe": EU_EEA_CH,
    "uk": {"GB"},
    "australia": {"AU"},
    "new_zealand": {"NZ"},
}

# Substrings that identify a country in a location string. Order matters:
# longer / more specific keys are checked first.
COUNTRY_HINTS: list[tuple[str, str]] = [
    # Bare "UK", to match the bare "US" further down. The leading-code and
    # trailing-code rules already catch "UK - Remote" and "London, UK", but a
    # location that is *only* the abbreviation has neither a separator nor a
    # comma to anchor them, so it parsed as no country at all — in a target
    # region. Safe as a plain hint because these match on \b boundaries, so it
    # cannot fire inside "Ukraine".
    ("uk", "GB"), ("u.k.", "GB"),
    ("united kingdom", "GB"), ("great britain", "GB"), ("england", "GB"),
    ("scotland", "GB"), ("wales", "GB"), ("northern ireland", "GB"),
    ("new zealand", "NZ"),
    ("netherlands", "NL"), ("holland", "NL"),
    ("switzerland", "CH"), ("czechia", "CZ"), ("czech republic", "CZ"),
    ("republic of ireland", "IE"),
    ("australia", "AU"), ("india", "IN"), ("germany", "DE"), ("france", "FR"),
    ("spain", "ES"), ("portugal", "PT"), ("italy", "IT"), ("poland", "PL"),
    ("sweden", "SE"), ("norway", "NO"), ("denmark", "DK"), ("finland", "FI"),
    ("belgium", "BE"), ("austria", "AT"), ("greece", "GR"), ("romania", "RO"),
    ("bulgaria", "BG"), ("hungary", "HU"), ("croatia", "HR"), ("estonia", "EE"),
    ("latvia", "LV"), ("lithuania", "LT"), ("luxembourg", "LU"), ("malta", "MT"),
    ("slovakia", "SK"), ("slovenia", "SI"), ("cyprus", "CY"), ("iceland", "IS"),
    ("ireland", "IE"),
    # Endonyms and ISO-3 codes, from locations the corpus actually emits.
    ("deutschland", "DE"), ("oesterreich", "AT"), ("österreich", "AT"),
    ("schweiz", "CH"), ("nederland", "NL"), ("españa", "ES"),
    ("sverige", "SE"), ("polska", "PL"), ("brasil", "BR"),
    ("deu", "DE"), ("ksa", "SA"), ("uae", "AE"),
    # Out-of-scope countries are listed too, so a posting that names one is
    # rejected outright rather than falling through to "unparsed, needs
    # scoring" and costing a scoring call.
    ("united states", "US"), ("usa", "US"), ("us only", "US"),
    ("u.s.", "US"), ("u.s.a.", "US"),
    # Bare "US" — safe here because these hints are only ever matched against
    # the short location field, never against description prose.
    ("us", "US"),
    ("canada", "CA"), ("mexico", "MX"), ("brazil", "BR"), ("argentina", "AR"),
    ("chile", "CL"), ("colombia", "CO"), ("costa rica", "CR"),
    ("singapore", "SG"), ("japan", "JP"), ("china", "CN"), ("hong kong", "HK"),
    ("south korea", "KR"), ("taiwan", "TW"), ("thailand", "TH"),
    ("vietnam", "VN"), ("viet nam", "VN"), ("indonesia", "ID"),
    ("malaysia", "MY"), ("philippines", "PH"),
    ("united arab emirates", "AE"), ("saudi arabia", "SA"), ("qatar", "QA"),
    ("israel", "IL"), ("turkey", "TR"), ("türkiye", "TR"),
    ("turkiye", "TR"), ("egypt", "EG"),
    ("south africa", "ZA"), ("nigeria", "NG"), ("kenya", "KE"),
    ("pakistan", "PK"), ("bangladesh", "BD"), ("sri lanka", "LK"),
    ("ukraine", "UA"), ("serbia", "RS"), ("russia", "RU"),
]

# Two-letter codes that are simultaneously a US state and a country we target.
# These are why "Berlin, DE" used to resolve to Delaware and "Bengaluru, IN" to
# Indiana — both silently dropping a target-region role. Resolved by
# corroboration in detect_countries().
AMBIGUOUS_CODES = {"IN", "DE", "MT"}   # India/Indiana, Germany/Delaware, Malta/Montana

# US state abbreviations, so "San Francisco, CA" resolves rather than falling
# through as unparsed. Kept separate from CITY_HINTS because it matches on the
# trailing ", XX" form only.
US_STATES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc",
}

# City -> country, for postings that name only a city.
CITY_HINTS: dict[str, str] = {
    # India
    "bengaluru": "IN", "bangalore": "IN", "hyderabad": "IN", "pune": "IN",
    "mumbai": "IN", "delhi": "IN", "gurgaon": "IN", "gurugram": "IN",
    "noida": "IN", "chennai": "IN", "kolkata": "IN", "ahmedabad": "IN",
    # UK
    "london": "GB", "manchester": "GB", "edinburgh": "GB", "bristol": "GB",
    "cambridge": "GB", "belfast": "GB", "glasgow": "GB", "leeds": "GB",
    # Europe
    "berlin": "DE", "munich": "DE", "münchen": "DE", "hamburg": "DE",
    "frankfurt": "DE", "cologne": "DE", "köln": "DE", "düsseldorf": "DE",
    "dusseldorf": "DE", "stuttgart": "DE", "leipzig": "DE", "dresden": "DE",
    "nürnberg": "DE", "nuremberg": "DE", "hannover": "DE", "bremen": "DE",
    "dortmund": "DE", "essen": "DE", "karlsruhe": "DE", "mannheim": "DE",
    "bonn": "DE", "bad homburg": "DE", "münster": "DE", "wuppertal": "DE",
    "amsterdam": "NL", "utrecht": "NL",
    "rotterdam": "NL", "paris": "FR", "lyon": "FR", "madrid": "ES",
    "barcelona": "ES", "lisbon": "PT", "porto": "PT", "milan": "IT",
    "rome": "IT", "warsaw": "PL", "kraków": "PL", "krakow": "PL",
    "stockholm": "SE", "gothenburg": "SE", "oslo": "NO", "copenhagen": "DK",
    "helsinki": "FI", "brussels": "BE", "vienna": "AT", "zurich": "CH",
    "zürich": "CH", "geneva": "CH", "dublin": "IE", "prague": "CZ",
    "bucharest": "RO", "sofia": "BG", "budapest": "HU", "tallinn": "EE",
    "riga": "LV", "vilnius": "LT",
    # ANZ
    "sydney": "AU", "melbourne": "AU", "brisbane": "AU", "perth": "AU",
    "adelaide": "AU", "canberra": "AU",
    "auckland": "NZ", "wellington": "NZ", "christchurch": "NZ",
    "valletta": "MT",
    # Major US metros, so a bare city name is rejected instead of burning a
    # scoring call. Only consulted when no country was named.
    "san francisco": "US", "new york": "US", "seattle": "US", "austin": "US",
    "boston": "US", "chicago": "US", "los angeles": "US", "denver": "US",
    "atlanta": "US", "san jose": "US", "palo alto": "US", "mountain view": "US",
    "sunnyvale": "US", "bellevue": "US", "portland": "US", "san diego": "US",
    "washington dc": "US", "philadelphia": "US", "dallas": "US",
    "houston": "US", "miami": "US", "phoenix": "US", "pittsburgh": "US",
    # The largest cities in the ambiguous-code states, so "Indianapolis, IN"
    # resolves to the US rather than tying with India.
    "indianapolis": "US", "fort wayne": "US", "wilmington": "US",
    "billings": "US", "bozeman": "US",
    "toronto": "CA", "vancouver": "CA", "montreal": "CA", "ottawa": "CA",
}

# Spelled-out US states and Canadian provinces. The trailing ", XX" rule below
# only catches the abbreviated form, so "Remote - California", "Ontario -
# Remote" and "Remote - Washington D.C." all parsed as naming no country at
# all — which meant in_target_region() treated them as in-region and they
# escaped the out-of-region penalty entirely.
#
# Deliberately incomplete. "Georgia" is a country as well as a state and
# "Victoria" is an Australian state as well as a Canadian city, so both are
# left out rather than guessed at; the abbreviated form still catches them.
SUBDIVISION_HINTS: dict[str, str] = {
    "california": "US", "washington state": "US", "washington d.c.": "US",
    "washington, d.c.": "US", "new york state": "US", "texas": "US",
    "massachusetts": "US", "colorado": "US", "illinois": "US", "oregon": "US",
    "utah": "US", "virginia": "US", "pennsylvania": "US", "arizona": "US",
    "florida": "US", "nevada": "US", "minnesota": "US", "north carolina": "US",
    "south carolina": "US", "new jersey": "US", "maryland": "US",
    "michigan": "US", "ohio": "US", "tennessee": "US", "missouri": "US",
    "wisconsin": "US", "connecticut": "US", "oklahoma": "US", "kansas": "US",
    "kentucky": "US", "alabama": "US", "louisiana": "US", "nebraska": "US",
    "idaho": "US", "iowa": "US", "arkansas": "US", "mississippi": "US",
    "new hampshire": "US", "rhode island": "US", "new mexico": "US",
    "ontario": "CA", "british columbia": "CA", "quebec": "CA", "québec": "CA",
    "alberta": "CA", "nova scotia": "CA", "manitoba": "CA",
    "saskatchewan": "CA", "newfoundland": "CA",
}

# One table for "a place name we recognise", so city and subdivision hints
# cannot drift apart in how they are matched or ranked.
PLACE_HINTS: dict[str, str] = CITY_HINTS | SUBDIVISION_HINTS

# Every country code this module knows about, for validating a bare code that
# no name or city corroborates. Derived rather than typed out, so adding a
# country to the tables above cannot leave this behind.
KNOWN_CODES: set[str] = (
    {code for _, code in COUNTRY_HINTS} | set(PLACE_HINTS.values()) | EU_EEA_CH
)

# Broad remote-region markers. Mapped to the region keys above.
REMOTE_REGION_HINTS: list[tuple[str, str]] = [
    ("emea", "europe"), ("europe", "europe"), ("eu remote", "europe"),
    ("apac", "australia"), ("anz", "australia"),
    ("india", "india"), ("uk", "uk"),
]

_REMOTE_RE = re.compile(r"\b(remote|work from home|wfh|distributed|anywhere)\b", re.I)

# Postings that claim no geographic requirement at all. Worth singling out:
# a location-independent role sidesteps sponsorship and relocation entirely,
# which is the whole problem this tool is built around.
#
# Treat the claim with suspicion, though — "Anywhere in the World" on a job
# board frequently means "anywhere we already have an employing entity", and
# the scorer flags that. It is a signal to look, not a guarantee.
# Matched against the location field, which is short and deliberate.
_GLOBAL_LOCATION_RE = re.compile(
    r"anywhere\s+in\s+the\s+world|\banywhere\b|world\s?wide|"
    r"remote\s*[-,:(]?\s*global|global(?:ly)?\s+remote|"
    r"location\s+independent|\bany\s+location\b",
    re.I,
)

# Matched against description prose, so it has to be far stricter. The loose
# pattern above finds "we are a global leader" and "15 offices worldwide" in
# ordinary company boilerplate, which says nothing about where you may work —
# that false-positived a Barcelona-only role into the anywhere list.
_GLOBAL_DESCRIPTION_RE = re.compile(
    r"work(?:ing)?\s+from\s+anywhere|anywhere\s+in\s+the\s+world|"
    r"hire\s+from\s+anywhere|location[-\s]independent|"
    r"fully\s+remote\s+(?:company|team|globally)|remote\s+from\s+anywhere",
    re.I,
)


COUNTRY_NAMES = {
    "IN": "India", "DE": "Germany", "GB": "United Kingdom", "AU": "Australia",
    "NZ": "New Zealand", "IE": "Ireland", "NL": "Netherlands", "FR": "France",
    "ES": "Spain", "PT": "Portugal", "IT": "Italy", "PL": "Poland",
    "SE": "Sweden", "NO": "Norway", "DK": "Denmark", "FI": "Finland",
    "BE": "Belgium", "AT": "Austria", "CH": "Switzerland", "CZ": "Czechia",
    "RO": "Romania", "BG": "Bulgaria", "HU": "Hungary", "GR": "Greece",
    "HR": "Croatia", "EE": "Estonia", "LV": "Latvia", "LT": "Lithuania",
    "LU": "Luxembourg", "MT": "Malta", "SK": "Slovakia", "SI": "Slovenia",
    "CY": "Cyprus", "IS": "Iceland", "US": "United States", "CA": "Canada",
    "SG": "Singapore", "AE": "United Arab Emirates",
}


def country_name(location: str | None) -> str | None:
    """The country a location sits in, spelled out.

    A "Country of Residence" field wants "Germany", not "Berlin, Germany" —
    which is what filling it from contact.location produced on a real Ashby
    form.
    """
    codes = detect_countries(location)
    return COUNTRY_NAMES.get(codes[0]) if codes else None


def is_global_remote(location: str | None, description: str | None = None) -> bool:
    """Does this posting claim it can be done from anywhere?

    Treat a hit as a reason to look, not a guarantee: "Anywhere in the World"
    on a job board frequently means "anywhere we already have an employing
    entity", and the scorer flags that separately.
    """
    if location and _GLOBAL_LOCATION_RE.search(location):
        return True
    return bool(description) and bool(_GLOBAL_DESCRIPTION_RE.search(description[:2000]))


def expand_regions(regions: list[str], extra_countries: list[str]) -> set[str]:
    """Turn preference region names into a set of country codes."""
    out: set[str] = set()
    for name in regions:
        out |= REGIONS.get(name.strip().lower(), set())
    out |= {c.strip().upper() for c in extra_countries if c.strip()}
    return out


def is_remote(location: str | None, extra: str | None = None) -> bool:
    blob = " ".join(filter(None, [location, extra]))
    return bool(_REMOTE_RE.search(blob))


def _city_hits(text: str) -> list[tuple[int, str]]:
    """(position, country) for every known place named in a location string.

    Cities and spelled-out states/provinces, from one table.

    Left as a plain word-boundary match on purpose. Requiring the city to sit
    at the end of a location component removes the "Porto" in "Porto Seguro,
    Bahia, Brazil" — but measured against a real corpus it also lost "London
    Office", "Barcelona Area", "Stockholm HQ" and "London OR Dublin", eleven
    target-region locations to save one false positive. City inference is
    instead ranked below explicit evidence in detect_countries(), so a
    misfire costs a scoring call rather than a wrong country.
    """
    hits: list[tuple[int, str]] = []
    for place, code in PLACE_HINTS.items():
        # A trailing "." is not a word character, so \b after it never matches
        # — "washington d.c." needs the suffix dropped.
        suffix = r"\b" if place[-1].isalnum() else ""
        match = re.search(rf"\b{re.escape(place)}{suffix}", text)
        if match:
            hits.append((match.start(), code))
    return hits


def detect_countries(location: str | None) -> list[str]:
    """Every country a location string mentions, in order of appearance.

    Postings routinely list several ("Remote, Canada; Remote, United Kingdom"),
    and picking one arbitrarily by table order sent a UK-eligible GitLab role
    to the wrong bucket. The filter wants the whole set.

    A trailing two-letter code is ambiguous when it names both a US state and a
    country we target — DE, IN, MT. A city in the same string decides it; with
    no corroborating city we emit *both* candidates, because the filter accepts
    a posting if any detected country is in scope, and spending one scoring
    call beats silently dropping a Berlin or Bengaluru role.
    """
    if not location:
        return []
    text = location.lower()

    found: list[tuple[int, str]] = []

    # A leading uppercase country code, as in "IN-Pune", "KR - Seoul",
    # "US-CA-Menlo Park" and "UK - Remote". Snowflake, Airwallex and others
    # write locations this way and none of the rules below could read them,
    # so a Seoul role parsed as naming no country at all — which
    # `in_target_region` then treats as in-region rather than penalising.
    #
    # Matched case-sensitively against the raw string, and that is the whole
    # trick: "In-Office" and "Hybrid; In-Office" are also in this corpus, and
    # a case-insensitive match reads them as India.
    lead = re.match(r"([A-Z]{2})\s*-\s*\w", location)
    if lead:
        code = lead.group(1)
        code = "GB" if code == "UK" else code
        if code in KNOWN_CODES:
            found.append((0, code))
    for hint, code in COUNTRY_HINTS:
        suffix = r"\b" if hint[-1].isalnum() else ""
        match = re.search(rf"\b{re.escape(hint)}{suffix}", text)
        if match:
            found.append((match.start(), code))

    cities = _city_hits(text)
    city_codes = {code for _, code in cities}

    for match in re.finditer(r",\s*([a-z]{2})\.?(?=\s*(?:[;,•]|$))", text):
        raw = match.group(1)
        upper = raw.upper()
        at = match.start()

        if upper == "UK":
            found.append((at, "GB"))
            continue

        is_state = raw in US_STATES
        # Any code this module knows, not just the target regions. SmartRecruiters
        # writes "Hanoi, vn" and "Yokohama, Kanagawa, jp", and while the target
        # set was the only thing checked here those parsed as naming no country
        # at all — which `location_passes` treats as unparsed and keeps, so a
        # Bosch board of 4,835 postings sent its Vietnamese and Japanese roles
        # to the scorer to be paid for and rejected there.
        is_country = upper in KNOWN_CODES

        if is_state and is_country:
            if upper in city_codes:
                found.append((at, upper))          # "Berlin, DE" -> Germany
            elif "US" in city_codes:
                found.append((at, "US"))           # "Indianapolis, IN" -> US
            else:
                found.append((at, upper))          # no signal: keep both and
                found.append((at, "US"))           # let scoring adjudicate
        elif is_state:
            found.append((at, "US"))
        elif is_country:
            found.append((at, upper))

    # Two tiers. Explicitly named countries and country codes come first, in
    # the order they appear; countries merely *inferred* from a city name are
    # appended after. Cities always contribute — that is what lets a known city
    # correct an ambiguous state code, and what recovers the target country
    # from "Madrid; Milan, Italy" — but they never outrank explicit evidence,
    # so "Porto Seguro, Bahia, Brazil" still reports Brazil first and
    # "Cambridge, MA" still reports the US.
    explicit: list[str] = []
    for _, code in sorted(found, key=lambda pair: pair[0]):
        if code not in explicit:
            explicit.append(code)

    inferred: list[str] = []
    for _, code in sorted(cities, key=lambda pair: pair[0]):
        if code not in explicit and code not in inferred:
            inferred.append(code)

    # A city that corroborates one of the *explicit* codes promotes it to
    # primary. "Gurugram, HR, IN" is City, Haryana, India — HR appears first
    # and looks like Croatia, but the city settles which code is the country.
    # Only explicit codes are eligible, so a city that merely disagrees with
    # the evidence (Cambridge in "Cambridge, MA") cannot hijack the result.
    for _, code in sorted(cities, key=lambda pair: pair[0]):
        if code in explicit:
            explicit.remove(code)
            explicit.insert(0, code)
            break

    return explicit + inferred


def detect_country(location: str | None) -> str | None:
    """Best-effort single country code from a free-text location.

    Deliberately a thin wrapper over detect_countries(): this used to carry its
    own copy of the table-walking logic, and the copy resolved "Berlin, DE" to
    the US while the other one did not. One implementation, one behaviour.
    """
    countries = detect_countries(location)
    return countries[0] if countries else None


def is_location_neutral(location: str | None) -> bool:
    """Is this remote *without* pinning you to a country or a region?

    Stricter than `is_global_remote`, and needed because the boards mostly do
    not say "worldwide" even when they mean it. DuckDuckGo advertises its
    security roles as plain "Remote"; that trips no global pattern, but a
    remote posting whose location names no country and no single region is
    exactly the thing being looked for.

    So: an explicit worldwide claim counts, and otherwise the posting has to
    be remote and say nothing about where. "Remote (United States)" names a
    country and "Remote - EMEA" names a region; both are remote jobs you can
    only hold from somewhere specific, which is the distinction that matters.

    Judged on the location field alone, never the description. That is not a
    shortcut — consulting the prose made this useless. Ordinary JD boilerplate
    says "remote" and "work from anywhere" constantly, so Stripe roles in
    Seattle and Cloudflare roles marked "Hybrid" came back location-neutral.
    The location field is short and deliberate; the description is marketing.
    """
    # A named country beats any "anywhere" wording, and this test has to come
    # first. Coalition advertises "Any location, United States" and "Any
    # location, Germany"; `is_global_remote` matches the "any location" half
    # and used to return True before the country was ever looked at, so seven
    # country-locked roles were reported as location-neutral. The qualifier is
    # the whole meaning of those strings.
    if detect_countries(location):
        return False
    # A named multi-country area disqualifies just as a country does, and for
    # the same reason. "Remote (North America)", "Remote EU" and "Remote -
    # LATAM" were all reading as location-neutral, because no country appears
    # in them and the substring-based region hints do not cover continents.
    if location and _REGION_MARKER_RE.search(location):
        return False
    if is_global_remote(location):
        return True
    if not is_remote(location):
        return False
    return not remote_regions_mentioned(location)


# Multi-country areas a remote posting can be pinned to. Word-boundary
# matched, unlike REMOTE_REGION_HINTS, which is a substring test and cannot be
# extended safely — a bare "eu" hint there would fire on "Deutschland".
#
# These are what separate "Remote" from "Remote (North America)". Without
# them a continent-locked role reads as location-neutral, which is the one
# claim this filter is not allowed to get wrong.
_REGION_MARKER_RE = re.compile(
    r"\b(?:emea|apac|amer|latam|anz|dach|benelux|nordics?|international|"
    r"east\s+coast|west\s+coast|"
    r"north\s+america|south\s+america|latin\s+america|americas?|"
    r"europe|european\s+union|eu|uk|asia|africa|middle\s+east|oceania)\b",
    re.I,
)


def remote_regions_mentioned(location: str | None) -> set[str]:
    """Region keys implied by a remote posting's stated coverage area."""
    if not location:
        return set()
    text = location.lower()
    return {region for hint, region in REMOTE_REGION_HINTS if hint in text}


def location_passes(
    location: str | None,
    allowed_countries: set[str],
    allowed_regions: list[str],
    accept_remote: bool,
    reject_other_countries: bool = True,
    mobility_countries: set[str] | None = None,
    is_mobility_friendly: bool = False,
) -> tuple[bool, str]:
    """Decide whether a posting's location is in scope.

    Returns (passes, reason). Unknown locations pass with a flag rather than
    being dropped — better to spend one scoring call than miss a real role.

    `mobility_countries` are countries outside the target regions that are
    still acceptable when `is_mobility_friendly` is set — i.e. the posting
    sponsors a visa, pays relocation, or is a contract. That is what lets a US
    role through without opening the floodgates to every US posting.
    """
    countries = detect_countries(location)
    mobility_countries = mobility_countries or set()

    in_scope = [c for c in countries if c in allowed_countries]
    if in_scope:
        return True, f"country {'/'.join(in_scope)} in scope"

    if countries:
        # A role based elsewhere but hiring remotely into one of our regions
        # is still in scope.
        if accept_remote and is_remote(location):
            overlap = remote_regions_mentioned(location) & set(allowed_regions)
            if overlap:
                return True, f"remote covering {sorted(overlap)}"

        mobility_hits = [c for c in countries if c in mobility_countries]
        if mobility_hits:
            if is_mobility_friendly:
                return True, f"{'/'.join(mobility_hits)} accepted — supports visa/reloc/contract"
            return False, f"{'/'.join(mobility_hits)} needs visa/reloc/contract; none stated"

        if reject_other_countries:
            return False, f"country {'/'.join(countries)} out of scope"
        return True, f"country {'/'.join(countries)} out of scope but rejection disabled"

    if is_remote(location):
        if not accept_remote:
            return False, "remote roles disabled"
        overlap = remote_regions_mentioned(location) & set(allowed_regions)
        if overlap:
            return True, f"remote covering {sorted(overlap)}"
        return True, "remote, region unstated — needs scoring"

    return True, f"location unparsed ({location!r}) — needs scoring"
