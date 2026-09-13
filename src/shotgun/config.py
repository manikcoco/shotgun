"""Load preferences.yaml and environment settings."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PREFS = REPO_ROOT / "config" / "preferences.yaml"
# Your own preferences file is gitignored, so a fresh clone has only the
# example. Falling back to it means `uv sync && uv run shotgun sweep` works
# immediately instead of failing on a missing file you have not been told
# about yet; the README still tells you to copy it before editing.
EXAMPLE_PREFS = REPO_ROOT / "config" / "preferences.example.yaml"
DATA_DIR = REPO_ROOT / "data"
PRIVATE_DIR = REPO_ROOT / "private"


def _load_dotenv(path: Path = REPO_ROOT / ".env") -> None:
    """Minimal .env loader. Existing environment always wins."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


# Sensible default per provider, so switching needs one env var, not two.
DEFAULT_MODELS = {"anthropic": "claude-opus-5", "openai": "gpt-5"}

# Prefixes that identify which provider a model name belongs to. Anything
# unrecognised is left alone — a custom deployment name is the user's call.
_MODEL_PREFIXES = {
    "anthropic": ("claude-",),
    "openai": ("gpt-", "o1", "o3", "o4"),
}


def _model_provider(model: str) -> str | None:
    for name, prefixes in _MODEL_PREFIXES.items():
        if model.startswith(prefixes):
            return name
    return None


def matching_companies(company: str | None, names) -> list[str]:
    """Which of `names` the posting's company name matches.

    One rule, shared by the priority bonuses and the any-level title list, so
    "is this that company" cannot mean two different things depending on which
    part of the config is asking.

    The match is on word boundaries rather than substrings, which is what lets
    `amazon` catch "Amazon Web Services (AWS)" while `meta` stays off
    "Metabase". Boards disagree about the exact company string — Ashby reports
    the board token (`duck-duck-go`), Greenhouse the display name — so list
    the spellings you need and let them all match.
    """
    if not company:
        return []
    return [
        name for name in names
        if name and re.search(rf"\b{re.escape(name)}\b", company, re.IGNORECASE)
    ]


@dataclass(frozen=True)
class Env:
    anthropic_api_key: str | None
    openai_api_key: str | None
    provider: str
    model: str
    browser_profile: Path
    headless: bool

    @classmethod
    def load(cls) -> Env:
        _load_dotenv()
        profile = os.environ.get("SHOTGUN_BROWSER_PROFILE", "~/.shotgun/chrome-profile")

        anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
        openai_key = os.environ.get("OPENAI_API_KEY")

        # Explicit wins; otherwise infer from whichever key is present, so
        # dropping an OPENAI_API_KEY into .env is enough to switch.
        provider = (os.environ.get("SHOTGUN_PROVIDER") or "").strip().lower()
        if provider not in DEFAULT_MODELS:
            provider = "openai" if (openai_key and not anthropic_key) else "anthropic"

        # A model name left over from the other provider is a confusing
        # failure — an OpenAI call with "claude-opus-5" comes back as an
        # unknown-model error. .env pins SHOTGUN_MODEL, so switching provider
        # would hit this every time.
        model = os.environ.get("SHOTGUN_MODEL") or ""
        if model and _model_provider(model) not in (None, provider):
            model = ""

        return cls(
            anthropic_api_key=anthropic_key,
            openai_api_key=openai_key,
            provider=provider,
            model=model or DEFAULT_MODELS[provider],
            browser_profile=Path(profile).expanduser(),
            headless=os.environ.get("SHOTGUN_HEADLESS", "") not in ("", "0", "false"),
        )


@dataclass
class Preferences:
    raw: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path = DEFAULT_PREFS) -> Preferences:
        if not path.exists() and path == DEFAULT_PREFS and EXAMPLE_PREFS.exists():
            path = EXAMPLE_PREFS
        if not path.exists():
            raise FileNotFoundError(f"No preferences file at {path}")
        return cls(raw=yaml.safe_load(path.read_text()) or {})

    # -- titles ------------------------------------------------------------
    @cached_property
    def include_patterns(self) -> list[re.Pattern[str]]:
        pats = self.raw.get("titles", {}).get("include", [])
        return [re.compile(p, re.IGNORECASE) for p in pats]

    @cached_property
    def exclude_patterns(self) -> list[re.Pattern[str]]:
        pats = self.raw.get("titles", {}).get("exclude", [])
        return [re.compile(p, re.IGNORECASE) for p in pats]

    @cached_property
    def any_level_companies(self) -> list[str]:
        """Companies whose security openings are all worth seeing."""
        raw = (self.raw.get("titles", {}).get("any_level") or {}).get("companies") or []
        return [str(c).strip().lower() for c in raw if str(c).strip()]

    @cached_property
    def any_level_patterns(self) -> list[re.Pattern[str]]:
        pats = (self.raw.get("titles", {}).get("any_level") or {}).get("include") or []
        return [re.compile(p, re.IGNORECASE) for p in pats]

    def any_level(self, company: str | None) -> bool:
        return bool(matching_companies(company, self.any_level_companies))

    def title_matches(
        self,
        title: str,
        company: str | None = None,
        *,
        any_level: bool = False,
    ) -> tuple[bool, str]:
        """Return (passes, reason). Exclusions beat inclusions.

        For a company on the any-level list the wider `any_level.include`
        patterns are tried as well. The `include` patterns all require a level
        word or a particular role shape — "security ... engineer", "head of
        ... security" — which is right when sweeping thousands of postings but
        throws away real work at a company you actively want: "Senior Privacy
        Engineer", "Principal Security Researcher" and "Detection Engineer"
        all matched no include pattern.

        `any_level=True` applies those wider patterns whatever the company,
        which is what a deliberately-chosen list of companies wants: the
        question there is what the company has open, not what a
        6,000-posting sweep should keep.

        Exclusions are checked first and are never relaxed, so an internship
        is still an internship and a product manager is still a product
        manager however much you want to work there.
        """
        for pat in self.exclude_patterns:
            if pat.search(title):
                return False, f"title excluded by /{pat.pattern}/"
        for pat in self.include_patterns:
            if pat.search(title):
                return True, f"title matched /{pat.pattern}/"

        if any_level or self.any_level(company):
            for pat in self.any_level_patterns:
                if pat.search(title):
                    return True, f"any level; title matched /{pat.pattern}/"
            return False, "any-level company, but title is not a security role"

        return False, "title matched no include pattern"

    # -- locations ---------------------------------------------------------
    @property
    def regions(self) -> list[str]:
        return self.raw.get("locations", {}).get("regions", [])

    @property
    def extra_countries(self) -> list[str]:
        return self.raw.get("locations", {}).get("countries", [])

    @property
    def accept_remote(self) -> bool:
        return bool(self.raw.get("locations", {}).get("accept_remote", True))

    @property
    def reject_other_countries(self) -> bool:
        return bool(self.raw.get("locations", {}).get("reject_other_countries", True))

    @property
    def mobility_friendly_countries(self) -> set[str]:
        """Out-of-region countries kept when the posting supports a move."""
        raw = self.raw.get("locations", {}).get("allow_if_mobility_friendly", []) or []
        return {c.strip().upper() for c in raw if c.strip()}

    # -- prioritisation ----------------------------------------------------
    @property
    def priority_weights(self) -> dict[str, int]:
        defaults = {
            "visa_sponsorship_bonus": 15,
            "relocation_bonus": 10,
            "contract_bonus": 8,
            "no_mobility_penalty": 25,
            "explicit_no_sponsorship_penalty": 50,
        }
        return defaults | {
            k: int(v) for k, v in (self.raw.get("priority") or {}).items() if k in defaults
        }

    @property
    def company_bonuses(self) -> dict[str, int]:
        """Company name -> bonus, flattened from the named groups.

        Groups exist for legibility in the config; nothing downstream cares
        which group a name came from. A name in two groups keeps the higher
        bonus rather than summing, so listing Google under both FAANG and
        MANGO does not double it.
        """
        out: dict[str, int] = {}
        groups = (self.raw.get("priority") or {}).get("company_groups") or {}
        for group in groups.values():
            if not isinstance(group, dict):
                continue
            bonus = int(group.get("bonus") or 0)
            for name in group.get("companies") or []:
                key = str(name).strip().lower()
                if key:
                    out[key] = max(out.get(key, 0), bonus)
        return out

    @property
    def country_bonuses(self) -> dict[str, int]:
        """ISO country code -> bonus for markets worth reaching first."""
        raw = (self.raw.get("priority") or {}).get("country_bonus") or {}
        return {
            str(k).strip().upper(): int(v)
            for k, v in raw.items()
            if str(k).strip()
        }

    # -- scoring / comp ----------------------------------------------------
    @property
    def minimum_score(self) -> int:
        return int(self.raw.get("scoring", {}).get("minimum_score", 70))

    @property
    def dealbreakers(self) -> list[str]:
        return [s.lower() for s in self.raw.get("scoring", {}).get("dealbreakers", [])]

    @cached_property
    def dealbreaker_patterns(self) -> list[re.Pattern[str]]:
        """Dealbreakers as regexes, so one can exclude its own false positives.

        They were plain substring tests, and `polygraph` matched the "Employee
        Polygraph Protection Act (EPPA)" poster link that US-incorporated
        employers paste into every posting. That is a law *forbidding*
        employers from demanding one, and it was rejecting eight in-region
        Elastic security roles in Spain, Portugal, Greece, Ireland, Poland and
        the UK.

        An entry that is not valid regex is matched literally rather than
        raising, so an unbalanced bracket cannot take a whole filter pass down
        with it. That does not rescue a phrase which is valid regex but means
        something other than what was intended — "c++" compiles on Python
        3.11 and later as a possessive quantifier rather than two literal plus
        signs — so an entry containing punctuation is worth checking against a
        posting you already know the answer for.
        """
        out = []
        for phrase in self.dealbreakers:
            try:
                out.append(re.compile(phrase, re.IGNORECASE))
            except re.error:
                out.append(re.compile(re.escape(phrase), re.IGNORECASE))
        return out

    @property
    def salary_minimums(self) -> dict[str, float]:
        return self.raw.get("compensation", {}).get("minimum", {}) or {}

    # -- sources -----------------------------------------------------------
    @property
    def jobspy(self) -> dict:
        return self.raw.get("sources", {}).get("jobspy", {}) or {}

    @property
    def ats_boards(self) -> dict:
        return self.raw.get("sources", {}).get("ats_boards", {}) or {}

    @property
    def remote_boards(self) -> dict:
        return self.raw.get("sources", {}).get("remote_boards", {}) or {}

    @property
    def remotecom(self) -> dict:
        return self.raw.get("sources", {}).get("remotecom", {}) or {}

    # -- apply -------------------------------------------------------------
    @property
    def apply_mode(self) -> str:
        return self.raw.get("apply", {}).get("mode", "queue")

    @property
    def blocked_companies(self) -> set[str]:
        return {c.lower() for c in self.raw.get("apply", {}).get("blocked_companies", [])}

    @property
    def max_per_run(self) -> int:
        return int(self.raw.get("apply", {}).get("max_per_run", 10))
