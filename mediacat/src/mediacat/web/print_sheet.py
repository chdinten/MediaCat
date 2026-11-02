"""A4 album sheet PDF — cover, headline, LP tracklists and runout codes.

Layout (A4 portrait, all measures in mm)::

    ┌────────────────────────────────────────────┐
    │ [cover]  ARTIST                            │
    │ [cover]  ALBUM TITLE        (title size,   │
    │ [cover]  YEAR               bold, shrinks  │
    │ [cover]  COUNTRY            to cover height)│
    │                                            │
    │ LP 1                  │ Runout / Matrix     │
    │ A1 Track …            │ Side A  2564…       │
    │ B1 Track …            │ Side B  2564…       │
    │ LP 2                  │ Runout / Matrix     │
    │ C1 Track …            │ Side C  …           │
    │ Total LPs in this set: 4                    │
    │ Signed by: Name                             │
    └────────────────────────────────────────────┘

Track titles are truncated with an ellipsis so they never pass the middle of
the page (105 mm).  Runout codes are wrapped, never truncated, because every
character matters.  A Unicode TTF (DejaVu) is used when available so dead-wax
symbols such as ∇ or ⊥ render; the PDF core fonts are the fallback.
"""

from __future__ import annotations

import functools
import io
import logging
import pathlib
from datetime import UTC, datetime
from typing import Any

from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

logger = logging.getLogger(__name__)

PAGE_W, PAGE_H = A4
MARGIN = 14 * mm
COVER = 58 * mm
HALF = PAGE_W / 2
TRACK_POS_W = 10 * mm
TRACK_RIGHT = HALF - 2 * mm
RUNOUT_X = HALF + 4 * mm
RUNOUT_LABEL_W = 14 * mm
BOTTOM = 16 * mm
MAX_TRACKS_PER_LP = 40

_FONT_DIRS = (
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/dejavu",
    "/usr/share/fonts/TTF",
)


@functools.lru_cache(maxsize=1)
def _register_fonts() -> dict[str, str]:
    """Register DejaVu fonts once; fall back to PDF core fonts."""
    files = {
        "MC-Sans": "DejaVuSans.ttf",
        "MC-Bold": "DejaVuSans-Bold.ttf",
        "MC-Mono": "DejaVuSansMono.ttf",
    }
    for d in _FONT_DIRS:
        paths = {name: pathlib.Path(d) / f for name, f in files.items()}
        if all(p.exists() for p in paths.values()):
            try:
                for name, p in paths.items():
                    pdfmetrics.registerFont(TTFont(name, str(p)))
                return {"sans": "MC-Sans", "bold": "MC-Bold", "mono": "MC-Mono"}
            except Exception as exc:  # pragma: no cover - corrupt font files
                logger.warning("DejaVu font registration failed: %s", exc)
    logger.warning("DejaVu fonts not found — runout symbols may not render in PDF")
    return {"sans": "Helvetica", "bold": "Helvetica-Bold", "mono": "Courier"}


def _width(text: str, font: str, size: float) -> float:
    return float(pdfmetrics.stringWidth(text, font, size))


def truncate(text: str, font: str, size: float, max_w: float) -> str:
    """Shorten *text* with a trailing ellipsis so it fits within *max_w*."""
    if _width(text, font, size) <= max_w:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _width(text[:mid].rstrip() + "…", font, size) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + "…"


def wrap(text: str, font: str, size: float, max_w: float) -> list[str]:
    """Word-wrap *text*; words longer than a line are split by character."""
    lines: list[str] = []
    line = ""
    for raw_word in text.split(" "):
        word = raw_word
        candidate = f"{line} {word}" if line else word
        if _width(candidate, font, size) <= max_w:
            line = candidate
            continue
        if line:
            lines.append(line)
        while _width(word, font, size) > max_w and len(word) > 1:
            cut = len(word)
            while cut > 1 and _width(word[:cut], font, size) > max_w:
                cut -= 1
            lines.append(word[:cut])
            word = word[cut:]
        line = word
    if line:
        lines.append(line)
    return lines or [""]


def _cover_reader(cover: bytes | None) -> ImageReader | None:
    """Downscale the cover to keep the PDF small; ``None`` if unreadable."""
    if not cover:
        return None
    try:
        img: Image.Image = Image.open(io.BytesIO(cover))
        img = img.convert("RGB")
        img.thumbnail((900, 900))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        buf.seek(0)
        return ImageReader(buf)
    except Exception as exc:
        logger.warning("Cover image unreadable for PDF: %s", exc)
        return None


class _Sheet:
    """Small cursor wrapper around a reportlab canvas with page breaks."""

    def __init__(self, c: canvas.Canvas, fonts: dict[str, str]) -> None:
        self.c = c
        self.f = fonts
        self.y = PAGE_H - MARGIN

    def ensure(self, needed: float) -> None:
        if self.y - needed < BOTTOM:
            self.c.showPage()
            self.y = PAGE_H - MARGIN

    def header(self, sheet: dict[str, Any], cover: bytes | None) -> None:
        c, f = self.c, self.f
        top = self.y
        reader = _cover_reader(cover)
        if reader:
            iw, ih = reader.getSize()
            scale = min(COVER / iw, COVER / ih)
            w, h = iw * scale, ih * scale
            c.drawImage(reader, MARGIN, top - h, width=w, height=h)
        else:
            c.setStrokeGray(0.7)
            c.rect(MARGIN, top - COVER, COVER, COVER)
            c.setFont(f["sans"], 9)
            c.setFillGray(0.5)
            c.drawCentredString(MARGIN + COVER / 2, top - COVER / 2, "No cover image")
            c.setFillGray(0)

        x = MARGIN + COVER + 7 * mm
        max_w = PAGE_W - MARGIN - x
        parts = [
            str(p)
            for p in (
                sheet.get("artist"),
                sheet.get("title"),
                sheet.get("year"),
                sheet.get("country"),
            )
            if p not in (None, "")
        ]
        size = 18.0
        while True:
            lines = [ln for p in parts for ln in wrap(p, f["bold"], size, max_w)]
            leading = size * 1.25
            if len(lines) * leading <= COVER or size <= 11:
                break
            size -= 1
        c.setFont(f["bold"], size)
        ty = top - size
        for ln in lines:
            c.drawString(x, ty, truncate(ln, f["bold"], size, max_w))
            ty -= leading
        self.y = min(top - COVER, ty) - 8 * mm

    def lp_block(self, lp: dict[str, Any]) -> None:
        c, f = self.c, self.f
        tracks = list(lp.get("tracks") or [])
        overflow = max(len(tracks) - MAX_TRACKS_PER_LP, 0)
        tracks = tracks[:MAX_TRACKS_PER_LP]
        track_lh = 4.4 * mm
        mono_size, mono_lh = 8.5, 3.9 * mm

        runout_w = PAGE_W - MARGIN - RUNOUT_X - RUNOUT_LABEL_W
        runouts = [
            (label, wrap(value, f["mono"], mono_size, runout_w))
            for label, value in (lp.get("runouts") or [])
            if value
        ]
        left_h = 7 * mm + max(len(tracks) + bool(overflow), 1) * track_lh
        right_h = (
            7 * mm + sum(len(lines) * mono_lh + 1.5 * mm for _, lines in runouts) if runouts else 0
        )
        self.ensure(max(left_h, right_h))
        top = self.y

        # Left: LP heading + tracks (truncated at half page width)
        c.setFont(f["bold"], 12)
        c.drawString(MARGIN, top - 12, f"LP {lp['lp']}")
        ty = top - 7 * mm - 9.5
        if not tracks:
            c.setFont(f["sans"], 9.5)
            c.setFillGray(0.5)
            c.drawString(MARGIN, ty, "No tracklist available")
            c.setFillGray(0)
        for pos, title in tracks:
            c.setFont(f["bold"], 9.5)
            c.drawString(MARGIN, ty, truncate(str(pos), f["bold"], 9.5, TRACK_POS_W - 1 * mm))
            c.setFont(f["sans"], 9.5)
            tx = MARGIN + TRACK_POS_W
            c.drawString(tx, ty, truncate(str(title), f["sans"], 9.5, TRACK_RIGHT - tx))
            ty -= track_lh
        if overflow:
            c.setFont(f["sans"], 9)
            c.setFillGray(0.4)
            c.drawString(MARGIN + TRACK_POS_W, ty, f"+ {overflow} more track(s)")
            c.setFillGray(0)

        # Right: runout / matrix per side, aligned with the LP heading
        if runouts:
            c.setFont(f["bold"], 9)
            c.setFillGray(0.35)
            c.drawString(RUNOUT_X, top - 12, "Runout / Matrix")
            c.setFillGray(0)
            ry = top - 7 * mm - mono_size
            for label, lines in runouts:
                c.setFont(f["bold"], 8.5)
                c.drawString(RUNOUT_X, ry, label)
                c.setFont(f["mono"], mono_size)
                for ln in lines:
                    c.drawString(RUNOUT_X + RUNOUT_LABEL_W, ry, ln)
                    ry -= mono_lh
                ry -= 1.5 * mm

        self.y = top - max(left_h, right_h) - 5 * mm

    def line(self, label: str, value: str = "", size: float = 11) -> None:
        c, f = self.c, self.f
        self.ensure(size * 1.6)
        c.setFont(f["bold"], size)
        c.drawString(MARGIN, self.y - size, label)
        if value:
            lw = _width(label + " ", f["bold"], size)
            c.setFont(f["sans"], size)
            c.drawString(
                MARGIN + lw,
                self.y - size,
                truncate(value, f["sans"], size, PAGE_W - 2 * MARGIN - lw),
            )
        self.y -= size * 1.6


_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def price_label(when: datetime) -> str:
    """``"October-2026 - price estimate -"`` for the month the sheet is printed."""
    return f"{_MONTHS[when.month - 1]}-{when.year} - price estimate -"


def build_album_sheet(sheet: dict[str, Any], cover: bytes | None = None) -> bytes:
    """Render one album sheet and return the PDF bytes.

    *sheet* keys: ``artist``, ``title``, ``year``, ``country``,
    ``lps`` (``[{"lp": 1, "tracks": [(pos, title)], "runouts": [(label, text)]}]``,
    at most two are printed), ``lp_total`` (int), ``signed`` (bool),
    ``signed_by``, ``signed_on`` and ``price_estimate`` (str).
    """
    fonts = _register_fonts()
    buf = io.BytesIO()
    title = " — ".join(str(p) for p in (sheet.get("artist"), sheet.get("title")) if p)
    c = canvas.Canvas(buf, pagesize=A4, pageCompression=1)
    c.setTitle(title or "Album sheet")
    c.setAuthor("MediaCat")

    s = _Sheet(c, fonts)
    s.header(sheet, cover)
    for lp in (sheet.get("lps") or [])[:2]:
        s.lp_block(lp)

    lp_total = int(sheet.get("lp_total") or 0)
    if lp_total > 2:
        s.line("Total LPs in this set:", str(lp_total))
    if sheet.get("signed"):
        signed = str(sheet.get("signed_by") or "—")
        if sheet.get("signed_on"):
            signed += f" ({sheet['signed_on']})"
        s.line("Signed by:", signed, size=12)
    if sheet.get("price_estimate"):
        s.line(price_label(datetime.now(UTC)), str(sheet["price_estimate"]))

    c.setFont(fonts["sans"], 7)
    c.setFillGray(0.55)
    c.drawRightString(PAGE_W - MARGIN, 8 * mm, f"MediaCat · {datetime.now(UTC).date().isoformat()}")
    c.save()
    return buf.getvalue()
