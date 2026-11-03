"""Unit tests for the in-app help: the help file's structure and links."""

from __future__ import annotations

import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from mediacat.web import help as help_mod
from mediacat.web.help import HELP_FILE, docs_url, load_help, parse_help

_TEMPLATES = Path(help_mod.__file__).with_name("templates")


def _entry(ident: str, q: str = "Q?", a: str = "<p>A.</p>") -> dict[str, Any]:
    return {"id": ident, "q": q, "a": a}


def test_parse_help_reads_sections_and_keywords() -> None:
    data = {
        "sections": [
            {"id": "s", "title": "S", "entries": [{**_entry("e"), "keywords": ["brackets", "(3)"]}]}
        ]
    }
    [section] = parse_help(data)
    assert section.title == "S"
    [entry] = section.entries
    assert entry.keywords == "brackets (3)"
    assert entry.answer == "<p>A.</p>"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({}, "sections"),
        ({"sections": [{"id": "s", "entries": [_entry("Bad Id")]}]}, "slug"),
        ({"sections": [{"id": "s", "entries": [_entry("x"), _entry("x")]}]}, "twice"),
        ({"sections": [{"id": "x", "entries": [_entry("x")]}]}, "twice"),
        ({"sections": [{"id": "s", "entries": [_entry("x", a="")]}]}, "needs"),
    ],
)
def test_parse_help_rejects_bad_files(data: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_help(data)


def test_load_help_rereads_a_changed_file(tmp_path: Path) -> None:
    path = tmp_path / "help.yaml"
    path.write_text(yaml.safe_dump({"sections": [{"id": "s", "entries": [_entry("one")]}]}))
    assert load_help(path)[0].entries[0].id == "one"
    path.write_text(yaml.safe_dump({"sections": [{"id": "s", "entries": [_entry("two")]}]}))
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 5))
    assert load_help(path)[0].entries[0].id == "two"


def test_docs_url_follows_the_browser_host() -> None:
    request: Any = SimpleNamespace(
        headers={"host": "mediacat.localhost:8443", "x-forwarded-proto": "https"},
        url=SimpleNamespace(netloc="app:8000", scheme="http"),
    )
    assert docs_url(request) == "https://docs.mediacat.localhost:8443/"
    request.headers = {}
    assert docs_url(request) == "http://docs.app:8000/"


# --- The shipped help file ----------------------------------------------------

SECTIONS = load_help(HELP_FILE)
ENTRIES = [e for s in SECTIONS for e in s.entries]
IDS = {s.id for s in SECTIONS} | {e.id for e in ENTRIES}


def test_help_file_has_content() -> None:
    assert len(SECTIONS) >= 6
    assert all(s.entries for s in SECTIONS)
    assert len(ENTRIES) >= 40


def test_help_file_answers_the_artist_number_question() -> None:
    """Pages link to /help#artist-number from the "?" next to an artist name."""
    entry = next(e for e in ENTRIES if e.id == "artist-number")
    assert "Saga (3)" in entry.question
    assert {"brackets", "parentheses"} <= set(entry.keywords.split())
    for name in (
        "pressing_detail.html",
        "partials/browse_artist.html",
        "partials/artist_detail.html",
    ):
        assert 'href="/help#artist-number"' in (_TEMPLATES / name).read_text()


def test_help_file_html_is_tame() -> None:
    for e in ENTRIES:
        assert not re.search(r"<\s*(script|style|iframe|form)|\son\w+\s*=", e.answer, re.I), e.id


def test_help_file_internal_links_resolve() -> None:
    """``/help#id`` and ``#id`` links point at existing answers or sections."""
    for e in ENTRIES:
        for target in re.findall(r'href="(?:/help)?#([^"]+)"', e.answer):
            assert target in IDS or target == "developers", (e.id, target)


def test_help_file_uses_the_browser_port() -> None:
    """Host port 443 belongs to Kasm: MediaCat addresses always carry :8443."""
    for e in ENTRIES:
        for host in re.findall(r"https://[a-z.]*localhost(:\d+)?", e.answer):
            assert host == ":8443", e.id
