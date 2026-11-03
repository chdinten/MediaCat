"""Catalogue browser helpers (web/catalogue_browser.py): index order, URLs, back links."""

from __future__ import annotations

import pytest

from mediacat.web.catalogue_browser import (
    BrowseFilters,
    _grade,
    _select_pressing,
    browse_url,
    index_letter,
    safe_back,
    sort_key,
)


@pytest.mark.parametrize(
    ("name", "key", "letter"),
    [
        ("The Cure", "cure", "C"),
        ("the the", "the", "T"),
        ("Björk", "bjork", "B"),
        ("10cc", "10cc", "#"),
        ("Theatre of Hate", "theatre of hate", "T"),
        ("", "", "#"),
    ],
)
def test_index_files_artists_without_the_and_accents(name: str, key: str, letter: str) -> None:
    assert sort_key(name) == key
    assert index_letter(name) == letter


def test_filters_are_trimmed_and_unusable_values_dropped() -> None:
    f = BrowseFilters.clean(
        q="  floyd ", fmt="tape", genres=" Rock, ,Jazz ", year_from="197x", year_to=" 1979 "
    )
    assert f == BrowseFilters(q="floyd", genres="Rock,Jazz", year_to="1979")
    assert f.genre_list == ["Rock", "Jazz"]
    assert f.pairs() == [("q", "floyd"), ("genres", "Rock,Jazz"), ("year_to", "1979")]
    assert f.active
    assert not BrowseFilters().active
    assert BrowseFilters.from_query("fmt=cd&artist=X&country_q=UK") == BrowseFilters(
        fmt="cd", country_q="UK"
    )


def test_browse_url_puts_the_selection_first_then_the_filters() -> None:
    f = BrowseFilters(fmt="vinyl")
    assert browse_url(BrowseFilters()) == "/catalogue"
    assert browse_url(f) == "/catalogue?fmt=vinyl"
    assert browse_url(f, "Pink Floyd") == "/catalogue?artist=Pink+Floyd&fmt=vinyl"
    assert (
        browse_url(f, "Pink Floyd", "Animals", "abc")
        == "/catalogue?artist=Pink+Floyd&album=Animals&pressing=abc&fmt=vinyl"
    )
    # "" is the album without a title; an album or pressing needs an artist.
    assert browse_url(BrowseFilters(), "X", "") == "/catalogue?artist=X&album="
    assert browse_url(BrowseFilters(), None, "Animals", "abc") == "/catalogue"
    assert browse_url(BrowseFilters(), "AC/DC & Co") == "/catalogue?artist=AC%2FDC+%26+Co"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/catalogue", "/catalogue"),
        ("/catalogue?artist=X&album=Y", "/catalogue?artist=X&album=Y"),
        ("/search?q=floyd&scope=albums", "/search?q=floyd&scope=albums"),
        ("  /catalogue?q=a  ", "/catalogue?q=a"),
        ("https://evil.example/catalogue", None),
        ("//evil.example/catalogue", None),
        ("/\\evil.example/", None),
        ("/admin", None),
        ("/catalogue/123", None),
        ("catalogue", None),
        ("", None),
    ],
)
def test_back_links_stay_on_catalogue_and_search_pages(value: str, expected: str | None) -> None:
    assert safe_back(value) == expected


def test_back_url_is_pointed_at_the_pressing_being_shown() -> None:
    url = "/catalogue?artist=Talk+Talk&album=Spirit+of+Eden&pressing=old&fmt=vinyl"
    assert (
        _select_pressing(url, "Talk Talk", "Spirit of Eden", "new")
        == "/catalogue?artist=Talk+Talk&album=Spirit+of+Eden&pressing=new&fmt=vinyl"
    )
    # No pressing yet: it goes right after the album.
    assert (
        _select_pressing("/catalogue?artist=A&album=B&q=x", "A", "B", "p1")
        == "/catalogue?artist=A&album=B&pressing=p1&q=x"
    )
    # Already right, another album, or not the browser: unchanged.
    same = "/catalogue?artist=A&album=B&pressing=p1"
    assert _select_pressing(same, "A", "B", "p1") == same
    assert _select_pressing(same, "A", "Other", "p2") == same
    assert _select_pressing("/search?q=a", "A", "B", "p1") == "/search?q=a"


@pytest.mark.parametrize(
    ("value", "short"),
    [
        ("Very Good Plus (VG+)", "VG+"),
        ("Near Mint (NM or M-)", "NM"),
        ("Mint (M)", "M"),
        ("Generic", "Generic"),
        ("", ""),
    ],
)
def test_discogs_grades_are_shortened(value: str, short: str) -> None:
    assert _grade(value) == short
