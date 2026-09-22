"""Location parsing.

These are table tests because the bugs here are table bugs. `US_STATES`
shadowing country codes silently resolved "Berlin, DE" to Delaware and
"Bengaluru, IN" to Indiana — two of the five target regions — and nothing
raised. Every case below is a location string a real job board emits.
"""

from __future__ import annotations

import pytest

from shotgun.geo import (
    detect_countries,
    detect_country,
    expand_regions,
    is_remote,
    location_passes,
    remote_regions_mentioned,
)

REGIONS = ["india", "europe", "uk", "australia", "new_zealand"]
ALLOWED = expand_regions(REGIONS, [])
MOBILITY = {"US", "CA", "SG", "AE"}


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        # -- the ambiguous two-letter codes, which is where this broke --------
        ("Berlin, DE", "DE"),
        ("Munich, DE", "DE"),
        ("Bengaluru, IN", "IN"),
        ("Hyderabad, IN", "IN"),
        ("Pune, Maharashtra, IN", "IN"),
        ("Valletta, MT", "MT"),
        # ...and the US readings of the same codes, decided by the city.
        ("Indianapolis, IN", "US"),
        ("Wilmington, DE", "US"),
        # -- unambiguous country codes ---------------------------------------
        ("Dublin, IE", "IE"),
        ("Amsterdam, NL", "NL"),
        ("London, UK", "GB"),
        # -- spelled-out country names ---------------------------------------
        ("Bengaluru, India", "IN"),
        ("Melbourne, VIC, Australia", "AU"),
        ("Aarhus, Denmark", "DK"),
        # -- US states and cities --------------------------------------------
        ("San Francisco, CA", "US"),
        ("Austin, TX", "US"),
        ("Remote, US", "US"),
        # -- non-US, non-target ----------------------------------------------
        ("Toronto, ON", "CA"),
        # -- city only -------------------------------------------------------
        ("Bangalore", "IN"),
        ("Sydney, NSW", "AU"),
    ],
)
def test_detect_country(location: str, expected: str) -> None:
    assert detect_country(location) == expected


def test_indian_state_code_does_not_lose_the_country() -> None:
    """"Gurugram, HR, IN" — HR is Haryana here, not Croatia. The country must
    still be detected, and must sort first."""
    countries = detect_countries("Gurugram, HR, IN")
    assert countries[0] == "IN"
    assert "IN" in countries


def test_multiple_countries_are_all_returned() -> None:
    """A posting hiring into several countries must not be reduced to one; the
    filter needs the whole set to decide."""
    assert detect_countries("Remote, Canada; Remote, United Kingdom") == ["CA", "GB"]


@pytest.mark.parametrize(
    ("location", "primary", "must_contain"),
    [
        # City inference must never outrank explicit evidence. All four of
        # these came out of replaying the parser over ~900 real board
        # locations, and the first two were regressions caught that way.
        ("Porto Seguro, Bahia, Brazil", "BR", "PT"),   # "Porto" is not Portugal here
        ("Cambridge, MA", "US", "GB"),                 # Cambridge, Massachusetts
        ("Madrid; Milan, Italy; Paris, France", "IT", "ES"),  # Madrid was being lost
        ("Singapore, Sydney", "SG", "AU"),             # Sydney was being lost
    ],
)
def test_city_inference_ranks_below_explicit_evidence(
    location: str, primary: str, must_contain: str
) -> None:
    countries = detect_countries(location)
    assert countries[0] == primary
    assert must_contain in countries, "inferred country still has to reach the filter"


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        # Cities followed by a descriptor or a conjunction, not a separator.
        # Requiring a trailing separator lost all of these.
        ("London Office", "GB"),
        ("Berlin Office", "DE"),
        ("Barcelona Area", "ES"),
        ("Stockholm HQ", "SE"),
        ("London OR Dublin", "GB"),
        ("Remote, London UK", "GB"),
        ("Paris offices", "FR"),
    ],
)
def test_city_without_a_trailing_separator(location: str, expected: str) -> None:
    assert expected in detect_countries(location)


def test_ambiguous_code_with_no_corroborating_city_keeps_both() -> None:
    """With no city to decide it, emit both readings. The filter accepts a
    posting if any detected country is in scope, so this costs one scoring call
    rather than silently dropping a target-region role."""
    countries = detect_countries("Somewheresville, IN")
    assert set(countries) == {"IN", "US"}


@pytest.mark.parametrize(
    "location", ["Remote - EMEA", "Remote (Europe)", "Remote — Anywhere"]
)
def test_unparseable_locations_return_empty(location: str) -> None:
    assert detect_countries(location) == []


def test_detect_country_agrees_with_detect_countries() -> None:
    """These had separate implementations that disagreed on "Berlin, DE"."""
    for location in ["Berlin, DE", "Bengaluru, IN", "Indianapolis, IN", "Dublin, IE"]:
        assert detect_country(location) == detect_countries(location)[0]


def test_no_location_is_not_an_error() -> None:
    assert detect_countries(None) == []
    assert detect_country(None) is None
    assert detect_countries("") == []


# ---------------------------------------------------------------- remote

@pytest.mark.parametrize(
    ("location", "remote"),
    [
        ("Remote - EMEA", True),
        ("Remote, India", True),
        ("Work from home", True),
        ("Anywhere", True),
        ("Berlin, DE", False),
        ("London, UK", False),
    ],
)
def test_is_remote(location: str, remote: bool) -> None:
    assert is_remote(location) is remote


def test_remote_regions_mentioned() -> None:
    assert remote_regions_mentioned("Remote - EMEA") == {"europe"}
    assert remote_regions_mentioned("Remote - APAC") == {"australia"}
    assert remote_regions_mentioned("Remote, India") == {"india"}


# ------------------------------------------------------------ the filter

@pytest.mark.parametrize(
    "location", ["Berlin, DE", "Bengaluru, IN", "London, UK", "Sydney, NSW", "Dublin, IE"]
)
def test_target_regions_pass(location: str) -> None:
    passes, reason = location_passes(location, ALLOWED, REGIONS, True, True, MOBILITY, False)
    assert passes, reason


def test_out_of_region_needs_mobility() -> None:
    passes, reason = location_passes(
        "Remote, US", ALLOWED, REGIONS, True, True, MOBILITY, is_mobility_friendly=False
    )
    assert not passes
    assert "needs visa/reloc/contract" in reason


def test_out_of_region_passes_when_mobility_friendly() -> None:
    passes, reason = location_passes(
        "Remote, US", ALLOWED, REGIONS, True, True, MOBILITY, is_mobility_friendly=True
    )
    assert passes
    assert "supports visa/reloc/contract" in reason


def test_unparsed_location_passes_for_scoring() -> None:
    """Better one scoring call than a missed role — the module's own rule."""
    passes, reason = location_passes(
        "Somewhere odd", ALLOWED, REGIONS, True, True, MOBILITY, False
    )
    assert passes
    assert "unparsed" in reason


def test_remote_covering_a_target_region_passes() -> None:
    passes, reason = location_passes(
        "Remote - EMEA", ALLOWED, REGIONS, True, True, MOBILITY, False
    )
    assert passes
    assert "europe" in reason


# ------------------------------------------------- location-independent

@pytest.mark.parametrize(
    "location",
    ["Anywhere in the World", "Remote, Global", "Worldwide", "Remote (Global)",
     "Anywhere", "Location Independent", "Globally Remote"],
)
def test_global_remote_locations(location: str) -> None:
    from shotgun.geo import is_global_remote
    assert is_global_remote(location) is True


@pytest.mark.parametrize(
    "location",
    ["Berlin, Germany", "Remote - EMEA", "London, UK", "Remote, India",
     "Barcelona", "Stockholm HQ"],
)
def test_region_bound_locations_are_not_global(location: str) -> None:
    from shotgun.geo import is_global_remote
    assert is_global_remote(location) is False


def test_company_boilerplate_is_not_a_remote_claim() -> None:
    """"We are a global leader" and "15 offices worldwide" say nothing about
    where you may work — matching those false-positived a Barcelona-only role
    into the anywhere list."""
    from shotgun.geo import is_global_remote
    boilerplate = (
        "We are a global leader in the experience analytics space, with a "
        "growing presence across 15 offices worldwide."
    )
    assert is_global_remote("Barcelona", boilerplate) is False


@pytest.mark.parametrize(
    "description",
    ["You can work from anywhere.", "We hire from anywhere in the world.",
     "This is a location-independent role.", "Remote from anywhere."],
)
def test_explicit_remote_claims_in_prose_count(description: str) -> None:
    from shotgun.geo import is_global_remote
    assert is_global_remote("Somewhere", description) is True


def test_no_input_is_not_global() -> None:
    from shotgun.geo import is_global_remote
    assert is_global_remote(None) is False
    assert is_global_remote("") is False


# ------------------------------------------------- spelled-out subdivisions

@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("Remote - California", "US"),
        ("Berkeley, California", "US"),
        ("Remote - Washington D.C.; Washington, D.C.", "US"),
        ("Austin, Texas", "US"),
        ("Boston, Massachusetts", "US"),
        ("Ontario - Remote", "CA"),
        ("Remote, British Columbia", "CA"),
    ],
)
def test_a_spelled_out_state_or_province_names_its_country(
    location: str, expected: str
) -> None:
    """The ", XX" rule only ever caught the abbreviation, so these parsed as
    naming no country — which made in_target_region() treat a California-only
    role as in-region and skip the out-of-region penalty altogether."""
    assert expected in detect_countries(location)


def test_ambiguous_subdivision_names_are_left_out() -> None:
    """"Georgia" is a country and "Victoria" is an Australian state. Guessing
    either would be worse than the abbreviation rule that already covers them."""
    assert detect_countries("Atlanta, GA") == ["US"]
    assert "US" not in detect_countries("Tbilisi, Georgia")


# ------------------------------------------------------- location neutrality

@pytest.mark.parametrize(
    "location",
    ["Remote", "Home based - Worldwide", "Remote, Global",
     "Anywhere in the World", "Distributed"],
)
def test_naming_nowhere_is_location_neutral(location: str) -> None:
    from shotgun.geo import is_location_neutral
    assert is_location_neutral(location) is True


@pytest.mark.parametrize(
    "location",
    [
        "Remote (United States)",   # a country
        "Remote UK",
        "Remote India",
        "Remote - California",      # a subdivision
        "Ontario - Remote",
        "Remote - EMEA",            # a region
        "Remote - APAC",
        "Seattle",                  # not remote at all
        "Hybrid",
        "N/A",
        "San Francisco, CA • New York, NY • United States",
    ],
)
def test_naming_somewhere_is_not(location: str) -> None:
    from shotgun.geo import is_location_neutral
    assert is_location_neutral(location) is False


def test_neutrality_ignores_the_description() -> None:
    """The regression this guards. `is_location_neutral` used to take the
    description, and ordinary JD boilerplate says "remote" and "work from
    anywhere" constantly — so Stripe roles in Seattle and Cloudflare roles
    marked "Hybrid" came back location-neutral. The location field is short
    and deliberate; the prose is marketing. It takes one argument now, and
    this asserts the signature rather than the behaviour, on purpose.
    """
    import inspect

    from shotgun.geo import is_location_neutral
    assert list(inspect.signature(is_location_neutral).parameters) == ["location"]


def test_no_location_is_not_neutral() -> None:
    from shotgun.geo import is_location_neutral
    assert is_location_neutral(None) is False
    assert is_location_neutral("") is False


# ------------------------------------------- leading country-code locations

@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("IN-Pune", "IN"),                # Snowflake
        ("PL-Warsaw-Lixa C", "PL"),
        ("US-CA-Menlo Park", "US"),
        ("KR - Seoul", "KR"),
        ("CN - Shanghai", "CN"),
        ("AE-Dubai", "AE"),
        ("JP-Tokyo", "JP"),
        ("AU - Melbourne", "AU"),         # Airwallex
        ("SG - Singapore", "SG"),
        ("IT - Milan", "IT"),
        ("UK - Remote", "GB"),            # UK is not the ISO code
    ],
)
def test_a_leading_country_code_is_read(location: str, expected: str) -> None:
    """Several boards write locations this way and nothing else here could
    read them, so a Seoul role parsed as naming no country — which
    in_target_region() then treats as in-region instead of penalising."""
    assert detect_countries(location) == [expected]


@pytest.mark.parametrize("location", ["In-Office", "Hybrid; In-Office", "On-site"])
def test_a_title_case_word_is_not_a_country_code(location: str) -> None:
    """The reason the match is case-sensitive. "In-Office" is in this corpus,
    and read case-insensitively it becomes India — which would put a US-only
    office role in the in-region queue."""
    assert detect_countries(location) == []


def test_an_unknown_leading_code_is_ignored() -> None:
    """Only codes this module actually knows are accepted, so a two-letter
    prefix that means something else does not invent a country."""
    assert detect_countries("XX - Nowhere") == []


def test_a_leading_code_does_not_displace_explicit_evidence() -> None:
    """"US-CA-Menlo Park" and "Berlin, Germany" must not start disagreeing."""
    assert detect_countries("Berlin, Germany") == ["DE"]
    assert detect_countries("London, UK") == ["GB"]


@pytest.mark.parametrize(
    "location",
    ["Any location, United States", "Any location, Germany",
     "Any location, Canada", "Any location, United Kingdom"],
)
def test_a_named_country_beats_anywhere_wording(location: str) -> None:
    """Coalition's phrasing, and the bug it exposed. `is_global_remote`
    matches the "any location" half and used to return True before the
    country was looked at, so seven country-locked roles were reported as
    location-neutral. The country is the whole meaning of the string."""
    from shotgun.geo import is_location_neutral

    assert is_location_neutral(location) is False


def test_worldwide_wording_with_no_country_is_still_neutral() -> None:
    """The country check must not break the cases it sits in front of."""
    from shotgun.geo import is_location_neutral

    for location in ("Home based - Worldwide", "Remote, Global",
                     "Anywhere in the World", "Remote", "Distributed"):
        assert is_location_neutral(location) is True, location


# ------------------------------------------- continents are not "anywhere"

@pytest.mark.parametrize(
    "location",
    ["Remote (North America)", "Remote EU", "Remote - LATAM", "Remote EMEA",
     "Remote - APAC", "Remote, Americas", "Remote Europe", "AMER",
     "East Coast", "International"],
)
def test_a_named_multi_country_area_is_not_location_neutral(location) -> None:
    """No country appears in any of these, and the substring-based region
    hints do not cover continents — so a continent-locked role read as
    location-neutral, which is the one claim this filter may not get wrong."""
    from shotgun.geo import is_location_neutral

    assert is_location_neutral(location) is False


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("Leipzig, Remote", "DE"), ("Bad Homburg, Remote", "DE"),
        ("Düsseldorf", "DE"), ("Stuttgart", "DE"), ("in Deutschland", "DE"),
        ("Brasil", "BR"), ("UAE", "AE"), ("KSA", "SA"),
    ],
)
def test_places_the_corpus_emits_are_recognised(location, expected) -> None:
    """Each of these was reported as location-neutral because the place name
    was missing from the tables — found by auditing what the filter kept."""
    from shotgun.geo import is_location_neutral

    assert expected in detect_countries(location)
    assert is_location_neutral(location) is False


@pytest.mark.parametrize("location", ["UK", "U.K.", "Remote, UK", "UK - Remote"])
def test_bare_uk_resolves_like_bare_us(location) -> None:
    """The leading- and trailing-code rules already catch "UK - Remote" and
    "London, UK", but a location that is *only* the abbreviation has neither a
    separator nor a comma to anchor them, so it parsed as no country at all."""
    assert detect_country(location) == "GB"


@pytest.mark.parametrize("location", ["Ukraine", "Kyiv, Ukraine"])
def test_the_uk_hint_does_not_fire_inside_ukraine(location) -> None:
    assert detect_country(location) == "UA"
