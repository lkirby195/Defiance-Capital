"""An address, read off a listing URL and nothing else.  # SPEC §4.4

Zillow, Redfin, Realtor.com, Hubzu, Auction.com and Xome all put the address in the URL
slug, and all of them prohibit scraping the page behind it. This module reads the slug and
stops there: it takes a string and returns a string, opens no connection, and has no idea
whether the page exists. The original URL is kept on the property so a person can open it
by hand for the photos and the remarks (SPEC §4.4).

Two slug shapes cover the major sites:

* one segment carrying the whole address - Zillow's
  ``/homedetails/3320-Meade-St-Denver-CO-80211/12345_zpid/``, Realtor.com's
  ``/realestateandhomes-detail/3320-Meade-St_Denver_CO_80211_M1234-5678``, Auction.com's
  ``/details/3320-meade-st-denver-co-80211-12345``, Hubzu's and Xome's variations on them;
* Redfin's ``/CO/Denver/3320-Meade-St-80211/home/123``, where the state and the city are
  the two segments before the street.

Both end in a state code and a ZIP, which is what makes the parse reliable: the street is
what comes before the city, and the city is what sits between the street's last word - a
street-type word like ``St`` or ``Ave``, plus any unit after it - and the state. A slug with
no street-type word falls back to a one-word city, which is right more often than it is wrong
and is a stand-in for a person to check either way.

Nothing here is an ``Adapter``. There is no remote call to fail and no ``enrichment_runs``
row to write; it is a pure function the web form (and later the LINK channel) calls, and the
Phase 3 adapter that wraps the URL channel will call it too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

# The words a street name ends on. Lower-cased; the slug is matched case-insensitively.
_STREET_TYPES: frozenset[str] = frozenset(
    {
        "st",
        "street",
        "ave",
        "avenue",
        "rd",
        "road",
        "dr",
        "drive",
        "ln",
        "lane",
        "blvd",
        "boulevard",
        "ct",
        "court",
        "cir",
        "circle",
        "pl",
        "place",
        "way",
        "ter",
        "terrace",
        "trl",
        "trail",
        "pkwy",
        "parkway",
        "hwy",
        "highway",
        "loop",
        "run",
        "pike",
        "path",
        "row",
        "sq",
        "square",
        "xing",
        "crossing",
        "cv",
        "cove",
        "pt",
        "point",
        "bnd",
        "bend",
    }
)
# What may follow the street type and still be part of the street: a direction, a unit.
_DIRECTIONS: frozenset[str] = frozenset({"n", "s", "e", "w", "ne", "nw", "se", "sw"})
_UNIT_WORDS: frozenset[str] = frozenset({"apt", "unit", "ste", "suite", "lot", "bldg", "fl"})

# ``...-CO-80211`` or ``...-CO-80211-1234`` at the end of a segment, with an optional site id
# after it (Auction.com appends its own number; Realtor.com an ``M1234-5678``).
_TAIL = re.compile(
    r"^(?P<body>.+?)-(?P<state>[A-Za-z]{2})-(?P<zip>\d{5})(?:-\d{4})?(?:-[A-Za-z0-9]+)*$"
)
# Redfin: the street segment ends in the ZIP alone; the state and city are the two segments
# before it.
_STREET_ZIP = re.compile(r"^(?P<body>.+?)-(?P<zip>\d{5})$")
_STARTS_WITH_NUMBER = re.compile(r"^\d+[A-Za-z]?$")
# The two letters before the ZIP have to be a state, or ``100-Elm-St-Dr-80211`` reads as a
# house in the state of DR. Every state plus the District.
_STATE_CODES: frozenset[str] = frozenset(
    """AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV
    NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY""".split()
)


@dataclass(frozen=True)
class ListingAddress:
    """What a slug said, title-cased for a form box; a person checks it either way."""

    street: str
    city: str
    state: str  # the two-letter code as the slug had it, upper-cased
    zip: str

    @property
    def line(self) -> str:
        """``3320 Meade St, Denver, CO 80211``, the way ``address_raw`` is written."""
        return f"{self.street}, {self.city}, {self.state} {self.zip}"


def _words(text: str) -> list[str]:
    return [word for word in re.split(r"[-_]+", text) if word]


def _title(words: list[str]) -> str:
    out: list[str] = []
    for word in words:
        lowered = word.lower()
        if lowered in _DIRECTIONS and len(lowered) <= 2:
            out.append(lowered.upper())
        elif lowered.startswith("#"):
            out.append(word)
        else:
            out.append(word[:1].upper() + word[1:].lower())
    return " ".join(out)


def _split_street_and_city(words: list[str]) -> tuple[list[str], list[str]] | None:
    """Where the street ends and the city begins, or None when it does not look like one.

    The street starts with a house number. It ends on the last street-type word, plus a
    direction or a unit if one follows; the city is everything after that. With no
    street-type word at all the city is the last word, which handles ``123-Main-Denver``.
    """
    if len(words) < 3 or not _STARTS_WITH_NUMBER.match(words[0]):
        return None
    lowered = [word.lower() for word in words]
    end = None
    for index in range(1, len(words) - 1):
        if lowered[index] in _STREET_TYPES:
            end = index
    if end is None:
        return words[:-1], words[-1:]
    # a direction or a unit after the street type is still the street
    while end + 1 < len(words) - 1:
        following = lowered[end + 1]
        if following in _DIRECTIONS or following in _UNIT_WORDS or following.startswith("#"):
            end += 1
            if following in _UNIT_WORDS and end + 1 < len(words) - 1:
                end += 1  # the unit's own number or letter
            continue
        break
    street, city = words[: end + 1], words[end + 1 :]
    return (street, city) if city else None


def _from_one_segment(segment: str) -> ListingAddress | None:
    # Realtor.com separates the parts with underscores; everybody else with dashes.
    match = _TAIL.match(segment.replace("_", "-"))
    if match is None or match.group("state").upper() not in _STATE_CODES:
        return None
    split = _split_street_and_city(_words(match.group("body")))
    if split is None:
        return None
    street, city = split
    return ListingAddress(
        street=_title(street),
        city=_title(city),
        state=match.group("state").upper(),
        zip=match.group("zip"),
    )


def _from_redfin_segments(segments: list[str]) -> ListingAddress | None:
    """``/CO/Denver/3320-Meade-St-80211/...``: state, city, then the street with its ZIP."""
    for index in range(2, len(segments)):
        state, city, street = segments[index - 2], segments[index - 1], segments[index]
        if state.upper() not in _STATE_CODES:
            continue
        match = _STREET_ZIP.match(street)
        if match is None:
            continue
        words = _words(match.group("body"))
        if not words or not _STARTS_WITH_NUMBER.match(words[0]):
            continue
        return ListingAddress(
            street=_title(words),
            city=_title(_words(city)),
            state=state.upper(),
            zip=match.group("zip"),
        )
    return None


def address_from_url(url: str | None) -> ListingAddress | None:
    """The address a listing URL carries in its slug, or None when it carries none.

    Never raises on a bad URL: a borrower pasting the wrong thing is a blank to be filled in,
    not an error. Never fetches anything.
    """
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    segments = [unquote(segment) for segment in parts.path.split("/") if segment]
    for segment in segments:
        found = _from_one_segment(segment)
        if found is not None:
            return found
    return _from_redfin_segments(segments)
