"""In-app help: questions and answers for everyday use (``/help``).

The text lives in ``help.yaml`` next to this module (sections of entries
with ``id``, ``q``, ``a`` and optional ``keywords``).  Answers are HTML from
that file (our own, so rendered with ``|safe``); ``keywords`` are extra search
words that are not shown (other ways of asking, e.g. "brackets" /
"parentheses").  Each entry is addressable as
``/help#<id>``, so other pages can link to an answer.  The last section points
developers to the MkDocs site, which runs only while it is started from the
control panel.

The file is re-read when it changes, so wording fixes show without a restart.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from mediacat.web.routes import _ctx, _tmpl

logger = logging.getLogger(__name__)

help_router = APIRouter(tags=["help"])

HELP_FILE = Path(__file__).with_name("help.yaml")
CONTROL_PANEL_URL = "http://127.0.0.1:8810/"

_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True)
class HelpEntry:
    id: str
    question: str
    answer: str  # trusted HTML, rendered with |safe
    keywords: str


@dataclass(frozen=True)
class HelpSection:
    id: str
    title: str
    entries: tuple[HelpEntry, ...]


def parse_help(data: Any) -> list[HelpSection]:
    """Validate the YAML structure; ids must be unique slugs (they are URL anchors)."""
    if not isinstance(data, dict) or not isinstance(data.get("sections"), list):
        raise ValueError("help file needs a top-level 'sections' list")
    seen: set[str] = set()

    def _id(value: Any) -> str:
        ident = str(value or "")
        if not _ID_RE.match(ident):
            raise ValueError(f"help id {ident!r} is not a lower-case slug")
        if ident in seen:
            raise ValueError(f"help id {ident!r} is used twice")
        seen.add(ident)
        return ident

    sections: list[HelpSection] = []
    for raw in data["sections"]:
        sid = _id(raw.get("id"))
        entries = []
        for item in raw.get("entries") or []:
            question, answer = str(item.get("q") or "").strip(), str(item.get("a") or "").strip()
            if not question or not answer:
                raise ValueError(f"help entry {item.get('id')!r} needs 'q' and 'a'")
            entries.append(
                HelpEntry(
                    id=_id(item.get("id")),
                    question=question,
                    answer=answer,
                    keywords=" ".join(str(k) for k in item.get("keywords") or []),
                )
            )
        sections.append(
            HelpSection(id=sid, title=str(raw.get("title") or sid), entries=tuple(entries))
        )
    return sections


_cache: dict[Path, tuple[float, list[HelpSection]]] = {}


def load_help(path: Path = HELP_FILE) -> list[HelpSection]:
    """The parsed help file, re-read when its modification time changes."""
    mtime = path.stat().st_mtime
    cached = _cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    sections = parse_help(yaml.safe_load(path.read_text(encoding="utf-8")))
    _cache[path] = (mtime, sections)
    return sections


def docs_url(request: Request) -> str:
    """The MkDocs site's address: ``docs.`` in front of the host the browser used."""
    host = request.headers.get("host") or request.url.netloc
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    return f"{scheme}://docs.{host}/"


@help_router.get("/help", response_class=HTMLResponse)
async def help_page(request: Request) -> HTMLResponse:
    ctx = _ctx(
        request,
        sections=load_help(),
        docs_url=docs_url(request),
        control_panel_url=CONTROL_PANEL_URL,
        query=request.query_params.get("q", ""),
    )
    return _tmpl().TemplateResponse(request=request, name="help.html", context=ctx)
