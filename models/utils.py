"""
utils.py — text / timestamp / category / location helpers for tools.py.

Pure deterministic. No model downloads, no network, no LLM. Every function
must degrade gracefully (return a safe default) rather than raise — the
unseen CSV has malformed timestamps, misspelled labels and missing values,
and one bad row must not kill the pipeline.

pip installs: python-dateutil, rapidfuzz
"""

from __future__ import annotations
import re
from datetime import datetime
from typing import Optional

from dateutil import parser as dateparser
from rapidfuzz import fuzz, process


# ---------------------------------------------------------------------------
# Canonical vocabulary
# ---------------------------------------------------------------------------
CANONICAL_CATEGORIES = [
    "fire_smoke_electrical",
    "network_outage",
    "flooding_plumbing",
    "lift_accessibility",
    "security_violence",
    "contractor_verification",
    "structural_hazard",
    "noise_disturbance",
    "medical",
    "other",
]

# Keyword -> canonical category. Substring match against normalized text,
# so "elec fire", "electrical fire", "smoke smell" all hit.
# Order matters: earlier entries win on a tie. Keep "other" out of this dict.
_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "fire_smoke_electrical": [
        "fire", "smoke", "smoky", "burning", "burn", "electrical", "electric",
        "spark", "short circuit", "wiring", "overheat", "flame", "alarm",
        "buzzing", "cable cabinet", "contactor", "distribution", "panel hot",
    ],
    "network_outage": [
        "network", "wifi", "wi-fi", "internet", "outage", "connectivity",
        "server down", "no signal", "offline", "lan", "router", "switch",
        "learning platform", "authentication", "login", "log in",
        "restart", "reboot", "desktop", "computer", "pc",
    ],
    "flooding_plumbing": [
        "flood", "flooding", "leak", "leaking", "burst pipe", "water damage",
        "plumbing", "overflow", "sewage", "water", "wet floor", "pooling",
        "pipe", "carpet", "ceiling leak",
    ],
    "lift_accessibility": [
        "lift", "elevator", "escalator", "wheelchair", "accessib", "ramp",
        "stuck lift", "disabled access", "trapped", "between floors",
        "drop-off", "drop off", "blocked access", "accessible route",
        "accessible drop", "intercom",
    ],
    "security_violence": [
        "assault", "fight", "violence", "weapon", "theft", "robbery",
        "intruder", "trespass", "security threat", "harassment", "unknown person",
        "restricted", "forced entry", "photographing", "suspicious",
    ],
    "contractor_verification": [
        "contractor", "vendor", "technician", "service provider",
        "unverified", "unidentified worker", "id check", "credentials",
        "work order", "no badge", "reflective vest",
    ],
    "structural_hazard": [
        "crack", "collapse", "ceiling", "structural", "unstable", "debris",
        "scaffolding", "falling", "handrail", "railing", "loose", "trip hazard",
        "stairwell", "stairs",
    ],
    "noise_disturbance": [
        "noise", "loud", "disturbance", "party", "music complaint", "music",
    ],
    "medical": [
        "injury", "injured", "collapsed", "unconscious", "medical",
        "ambulance", "first aid", "seizure", "ankle", "sprain", "minor injury",
        "breathing", "paramedic", "dehydration", "confused",
    ],
}

# Status signals used by tools.update_incident to detect conflict.
# "resolved" means: evidence says the incident is now over or fully handled.
# "ongoing"  means: evidence says it is still active or getting worse.
# Deliberately conservative — false positives here corrupt lifecycle scoring.
RESOLVED_KEYWORDS = [
    "resolved", "restored", "back online", "back up", "repaired",
    "under control", "extinguished", "no longer", "returned to service",
    "no further", "no remaining", "handover completed", "reopened after",
    "declares the immediate danger controlled", "no wider hazard",
    "removed and", "stable during testing", "passes the safety test",
    "repairs are complete", "cleared",
]

ONGOING_OR_WORSENING_KEYWORDS = [
    "still", "ongoing", "worse", "worsening", "spreading", "not fixed",
    "continues", "again", "reoccurred", "escalat", "remains", "remain",
    "no end in sight", "unconfirmed", "conflicts with",
]


# ---------------------------------------------------------------------------
# Basic text helpers
# ---------------------------------------------------------------------------
def clean_text(value) -> str:
    """
    Lowercase, collapse whitespace, strip punctuation noise. Never raises.
    Keeps letters, digits, spaces and hyphens (hyphens matter for
    'wi-fi', 'drop-off', 'E3-lab').
    """
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in ("nan", "none", "null", "n/a", "na", ""):
        return ""
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s\-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def safe_get(d: dict, *keys, default=""):
    """
    Return the first present, non-empty value among possible column spellings.
    Case-insensitive. Treats '', 'nan', 'none', 'null', 'n/a' as missing.
    """
    if not isinstance(d, dict):
        return default
    lower_map = {str(k).lower(): v for k, v in d.items()}
    for key in keys:
        v = lower_map.get(key.lower())
        if v is None:
            continue
        if str(v).strip().lower() in ("", "nan", "none", "null", "n/a"):
            continue
        return v
    return default


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------
def parse_timestamp(value) -> Optional[datetime]:
    """
    Parse a possibly-malformed timestamp. Returns None (never raises) so
    callers fall back to arrival order rather than dropping the report.
    """
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in ("", "nan", "none", "null", "n/a"):
        return None

    try:
        return dateparser.parse(text, fuzzy=True, dayfirst=False)
    except (ValueError, OverflowError, TypeError):
        pass
    try:
        return dateparser.parse(text, fuzzy=True, dayfirst=True)
    except (ValueError, OverflowError, TypeError):
        return None


def minutes_between(t1: Optional[datetime],
                     t2: Optional[datetime]) -> Optional[float]:
    """Absolute gap in minutes, or None if either side is unknown."""
    if t1 is None or t2 is None:
        return None
    try:
        return abs((t1 - t2).total_seconds()) / 60.0
    except (OverflowError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Category normalization
# ---------------------------------------------------------------------------
def normalize_category(raw_value) -> str:
    """
    Map a messy free-text category to a canonical one.
    Priority: exact keyword substring -> fuzzy keyword match -> "other".
    Descriptions are handled by the caller (tools._coerce_raw_row retries
    with category+description when the raw category is a department name).
    """
    text = clean_text(raw_value)
    if not text:
        return "other"

    # 1. Direct substring match (cheap, deterministic).
    for category, keywords in _CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                return category

    # 2. Fuzzy: best match across all keywords. 82 threshold tuned so real
    # misspellings ("electircal") still hit but random words don't.
    best_category, best_score = "other", 0.0
    for category, keywords in _CATEGORY_KEYWORDS.items():
        match = process.extractOne(text, keywords, scorer=fuzz.partial_ratio)
        if match and match[1] > best_score:
            best_score = match[1]
            best_category = category

    if best_score >= 82:
        return best_category
    return "other"


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------
# Filler words that carry no identifying information. Deliberately does NOT
# include "block" / "building" — those are structural prefixes in this
# dataset ("Block A", "Building E3") and stripping them collapses unrelated
# locations ("Academic Block A" vs "Administration Block A" would both
# become "a").
_LOCATION_FILLER = re.compile(r"\b(the|near|at|on|of|in)\b")


def normalize_location(raw_value) -> str:
    """Lowercase, strip filler words, collapse whitespace. Never raises."""
    text = clean_text(raw_value)
    if not text:
        return "unknown"
    text = _LOCATION_FILLER.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or "unknown"


def location_similarity(a: str, b: str) -> float:
    """
    Fuzzy 0-100 similarity between two normalized locations.
    token_sort_ratio is right here because it's order-independent — it
    matches "lift a admin block" against "admin block lift a".
    """
    a, b = normalize_location(a), normalize_location(b)
    if a == "unknown" or b == "unknown":
        return 0.0
    if a == b:
        return 100.0
    return float(fuzz.token_sort_ratio(a, b))


def text_similarity(a: str, b: str) -> float:
    """
    Fuzzy 0-100 similarity between two descriptions.
    token_set_ratio is more forgiving than token_sort — duplicate reports
    often add or drop a sentence, and this still scores them high.
    """
    a, b = clean_text(a), clean_text(b)
    if not a or not b:
        return 0.0
    return float(fuzz.token_set_ratio(a, b))


# ---------------------------------------------------------------------------
# Status signals
# ---------------------------------------------------------------------------
def has_any_keyword(text: str, keywords: list) -> bool:
    t = clean_text(text)
    return any(kw in t for kw in keywords)


def detect_status_signal(text: str) -> Optional[str]:
    """
    Returns one of:
      "resolved"  — evidence says the incident is over / fully handled
      "ongoing"   — evidence says it's still active or worsening
      "conflict"  — the SAME report asserts both (rare, but tools.py handles it)
      None        — no signal detected

    Keyword-only. Semantic matching was evaluated and dropped: it needed a
    model download at judge time (single point of failure) and the keyword
    lists cover the dev vocabulary. See README limitations.
    """
    resolved_hit = has_any_keyword(text, RESOLVED_KEYWORDS)
    ongoing_hit = has_any_keyword(text, ONGOING_OR_WORSENING_KEYWORDS)

    if resolved_hit and ongoing_hit:
        return "conflict"
    if resolved_hit:
        return "resolved"
    if ongoing_hit:
        return "ongoing"
    return None