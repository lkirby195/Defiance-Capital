"""An address off a listing URL's slug, and nothing fetched.  # SPEC §4.4

Every case here is a string in and a string out. There is no network to mock because there
is no network: the module never opens a connection, and the test that proves it is that
``socket`` is never touched - ``address_from_url`` is a pure function of its argument.
"""

from __future__ import annotations

import socket

import pytest

from adapters.listing_url import ListingAddress, address_from_url

MEADE = ListingAddress("3320 Meade St", "Denver", "CO", "80211")
PEORIA = ListingAddress("1412 S Peoria Ave", "Tulsa", "OK", "74120")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # one segment carrying the whole address
        ("https://www.zillow.com/homedetails/3320-Meade-St-Denver-CO-80211/13241234_zpid/", MEADE),
        (
            "https://www.realtor.com/realestateandhomes-detail/3320-Meade-St_Denver_CO_80211_M1234-5678",
            MEADE,
        ),
        (
            "https://www.auction.com/details/1412-s-peoria-ave-tulsa-ok-74120-3182367-e_13107/",
            PEORIA,
        ),
        ("https://www.hubzu.com/property/1412-S-PEORIA-AVE-TULSA-OK-74120/1234567", PEORIA),
        ("https://www.xome.com/homes/1412-S-Peoria-Ave-Tulsa-OK-74120/123456", PEORIA),
        # Redfin: the state and the city are the two segments before the street
        ("https://www.redfin.com/CO/Denver/3320-Meade-St-80211/home/95713912", MEADE),
        (
            "https://www.redfin.com/OK/Oklahoma-City/1234-NW-23rd-St-73107/home/1",
            ListingAddress("1234 NW 23rd St", "Oklahoma City", "OK", "73107"),
        ),
        # a two-word city after the street type
        (
            "https://www.zillow.com/homedetails/123-Main-St-Oklahoma-City-OK-73102/1_zpid/",
            ListingAddress("123 Main St", "Oklahoma City", "OK", "73102"),
        ),
        # a unit after the street type stays with the street
        (
            "https://www.zillow.com/homedetails/500-E-5th-St-APT-12-Tulsa-OK-74103/2_zpid/",
            ListingAddress("500 E 5th St Apt 12", "Tulsa", "OK", "74103"),
        ),
        # no street-type word at all: the city is the last word
        (
            "https://example.com/listing/123-Broadway-Denver-CO-80203",
            ListingAddress("123 Broadway", "Denver", "CO", "80203"),
        ),
        # a ZIP+4 and a percent-encoded slug both read
        (
            "https://example.com/l/3320-Meade-St-Denver-CO-80211-1234",
            MEADE,
        ),
        ("https://example.com/l/3320%2DMeade-St-Denver-CO-80211", MEADE),
    ],
)
def test_the_address_is_read_off_the_slug(url: str, expected: ListingAddress) -> None:
    assert address_from_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "   ",
        "not a url",
        "https://www.zillow.com/",
        "https://www.zillow.com/homes/for_sale/Denver-CO/",
        # two letters before a number that are not a state
        "https://example.com/l/100-Elm-St-Dr-80211",
        # a state and a ZIP with no house number in front
        "https://example.com/l/Meade-St-Denver-CO-80211",
        "https://example.com/l/CO-80211",
        "http://[not-a-host/3320-Meade-St-Denver-CO-80211",
    ],
)
def test_a_url_that_carries_no_address_is_none_rather_than_a_guess(url: str | None) -> None:
    assert address_from_url(url) is None


def test_nothing_is_fetched(monkeypatch: pytest.MonkeyPatch) -> None:
    """SPEC §4.4: the URL string is read and the page behind it never is."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("address_from_url opened a connection")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    assert (
        address_from_url(
            "https://www.zillow.com/homedetails/3320-Meade-St-Denver-CO-80211/13241234_zpid/"
        )
        == MEADE
    )


def test_the_line_reads_the_way_an_address_is_written() -> None:
    assert MEADE.line == "3320 Meade St, Denver, CO 80211"
