"""Page background of the login page and the dashboard: a still image or a looping video.

The admin chooses the mode and may upload replacements (Admin → Layout).
The choice lives in one ``SiteSetting`` row (``background``, JSON); uploaded
files are stored in MinIO under ``site/background/<sha256><ext>`` and served
by the public ``/site-media/background/<name>`` route, since the login page
shows them before anyone is signed in.  Without a setting the built-in files
in ``/static`` are used.

  GET  /site-media/background/{name}   → uploaded file (Range aware, immutable)
  POST /admin/background/upload        → check and store 1–3 files, switch mode
  POST /admin/background/mode          → image | video
  POST /admin/background/fit           → width (whole picture) | cover (fill, cropped)
  POST /admin/background/reset         → back to the built-in files

Uploads are identified by their content, not by name or browser MIME type:
JPEG / PNG / WebP stills, MP4 (H.264 or AV1) and WebM (VP8 / VP9 / AV1)
videos, each checked for size, dimensions and (videos) duration.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import struct
import time
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from PIL import Image
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from mediacat.db.models import SiteSetting
from mediacat.web.routes import _require_role

logger = logging.getLogger(__name__)

background_router = APIRouter(tags=["background"])

SETTING_KEY = "background"
MODES = ("video", "image")
# width: whole picture across the window, page colour below; cover: fill the window, cropped
FITS = ("width", "cover")
OBJECT_PREFIX = "site/background/"
PUBLIC_PREFIX = "/site-media/background/"

BUILTIN_IMAGE = "/static/media-cat-hero-poster.webp"
BUILTIN_SIZE = (1668, 470)  # all three built-in files
BUILTIN_VIDEOS = (
    {"url": "/static/media-cat-hero.webm", "type": "video/webm"},
    {"url": "/static/media-cat-hero.mp4", "type": "video/mp4"},
)

MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_VIDEO_BYTES = 50 * 1024 * 1024
MIN_WIDTH, MIN_HEIGHT = 1200, 300
MAX_SIDE = 8192
MAX_VIDEO_SIDE = 3840
MAX_DURATION_S = 60.0

_NAME_RE = re.compile(r"^[0-9a-f]{64}\.(?:jpg|png|webp|mp4|webm)$")
_ADMIN_URL = "/admin?tab=layout"

# Admin page messages for the ``bg_error`` redirect codes.
ERRORS: dict[str, str] = {
    "empty": "Choose at least one file to upload.",
    "type": (
        "Unsupported file type. Use a JPEG, PNG or WebP image, an MP4 (H.264) "
        "or a WebM (VP8 / VP9 / AV1) video."
    ),
    "corrupt": "The file could not be read: it is damaged or not what its name says.",
    "image_size": "Images may be at most 15 MB.",
    "video_size": "Videos may be at most 50 MB.",
    "image_dims": (
        f"The image must be at least {MIN_WIDTH} × {MIN_HEIGHT} px and at most {MAX_SIDE} px a side."
    ),
    "video_dims": f"The video must be 640–{MAX_VIDEO_SIDE} px wide.",
    "animated": "Animated images are not supported: upload the animation as MP4 or WebM.",
    "codec": (
        "The MP4 must be H.264 (or AV1) encoded; H.265/HEVC does not play in every browser. "
        "Convert with: ffmpeg -i in.mp4 -c:v libx264 -crf 23 -an -movflags +faststart out.mp4"
    ),
    "webm_codec": "The WebM must be VP8, VP9 or AV1 encoded.",
    "duration": f"Videos may be at most {MAX_DURATION_S:.0f} seconds long (a short loop is best).",
    "duplicate": "Choose at most one image, one MP4 and one WebM.",
    "store": "The file store (MinIO) is not available; uploads are disabled.",
    "db": "The setting could not be saved. Check the server log.",
}


class MediaCheckError(ValueError):
    """An upload failed the format check; ``code`` is a key of :data:`ERRORS`."""

    def __init__(self, code: str) -> None:
        super().__init__(ERRORS.get(code, code))
        self.code = code


@dataclass(frozen=True, slots=True)
class MediaInfo:
    """What the format check found in an upload."""

    slot: str  # image | mp4 | webm
    mime: str
    ext: str
    width: int
    height: int
    codec: str = ""
    duration: float | None = None
    faststart: bool = True


# ---------------------------------------------------------------------------
# Format check
# ---------------------------------------------------------------------------


def check_media(data: bytes) -> MediaInfo:
    """Identify an upload by its content and check it is a usable background.

    Raises :class:`MediaCheckError` when the file is not acceptable.
    """
    head = data[:16]
    if head.startswith(b"\xff\xd8\xff") or head.startswith(b"\x89PNG\r\n\x1a\n"):
        return _check_image(data)
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return _check_image(data)
    if head[4:8] == b"ftyp":
        return _check_mp4(data)
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return _check_webm(data)
    raise MediaCheckError("type")


_IMAGE_FORMATS = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
}


def _check_image(data: bytes) -> MediaInfo:
    if len(data) > MAX_IMAGE_BYTES:
        raise MediaCheckError("image_size")
    try:
        Image.open(io.BytesIO(data)).verify()
        img = Image.open(io.BytesIO(data))
    except Exception as exc:
        raise MediaCheckError("corrupt") from exc
    if img.format not in _IMAGE_FORMATS:
        raise MediaCheckError("type")
    if getattr(img, "is_animated", False):
        raise MediaCheckError("animated")
    width, height = img.size
    if width < MIN_WIDTH or height < MIN_HEIGHT or max(width, height) > MAX_SIDE:
        raise MediaCheckError("image_dims")
    mime, ext = _IMAGE_FORMATS[img.format]
    return MediaInfo("image", mime, ext, width, height)


def _check_video_limits(width: int, height: int, duration: float | None) -> None:
    if not 640 <= width <= MAX_VIDEO_SIDE or not 0 < height <= MAX_VIDEO_SIDE:
        raise MediaCheckError("video_dims")
    if duration is not None and duration > MAX_DURATION_S:
        raise MediaCheckError("duration")


# -- MP4 (ISO base media file format) ----------------------------------------

_MP4_CODECS = {b"avc1": "H.264", b"avc3": "H.264", b"av01": "AV1"}
_MP4_CONTAINERS = {b"trak", b"mdia", b"minf", b"stbl"}


def _boxes(data: bytes, start: int, end: int) -> Iterator[tuple[bytes, int, int]]:
    """Yield ``(type, payload_start, box_end)`` for the boxes in ``data[start:end]``."""
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, pos)
        header = 8
        if size == 1:
            if pos + 16 > end:
                raise MediaCheckError("corrupt")
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            raise MediaCheckError("corrupt")
        yield kind, pos + header, pos + size
        pos += size


def _mp4_video_track(data: bytes, start: int, end: int) -> tuple[str, int, int] | None:
    """Codec fourcc, width and height of the first video track in a ``trak`` box."""
    width = height = 0
    handler = b""
    fourcc = b""

    def walk(s: int, e: int) -> None:
        nonlocal width, height, handler, fourcc
        for kind, p, box_end in _boxes(data, s, e):
            if kind == b"tkhd" and box_end - p >= 8:
                width, height = (v >> 16 for v in struct.unpack_from(">II", data, box_end - 8))
            elif kind == b"hdlr" and box_end - p >= 12:
                handler = data[p + 8 : p + 12]
            elif kind == b"stsd" and box_end - p >= 16:
                fourcc = data[p + 12 : p + 16]
            elif kind in _MP4_CONTAINERS:
                walk(p, box_end)

    walk(start, end)
    if handler != b"vide":
        return None
    return fourcc.decode("latin-1"), width, height


def _check_mp4(data: bytes) -> MediaInfo:
    if len(data) > MAX_VIDEO_BYTES:
        raise MediaCheckError("video_size")
    top = list(_boxes(data, 0, len(data)))
    kinds = [kind for kind, _, _ in top]
    if not kinds or kinds[0] != b"ftyp" or b"moov" not in kinds:
        raise MediaCheckError("corrupt")
    _, moov_start, moov_end = top[kinds.index(b"moov")]
    duration: float | None = None
    track: tuple[str, int, int] | None = None
    for kind, p, box_end in _boxes(data, moov_start, moov_end):
        if kind == b"mvhd" and box_end - p >= 32:
            if data[p] == 1:
                timescale, length = struct.unpack_from(">IQ", data, p + 20)
            else:
                timescale, length = struct.unpack_from(">II", data, p + 12)
            duration = length / timescale if timescale else None
        elif kind == b"trak" and track is None:
            track = _mp4_video_track(data, p, box_end)
    if track is None:
        raise MediaCheckError("corrupt")
    fourcc, width, height = track
    codec = _MP4_CODECS.get(fourcc.encode("latin-1"))
    if codec is None:
        raise MediaCheckError("codec")
    _check_video_limits(width, height, duration)
    faststart = b"mdat" not in kinds or kinds.index(b"moov") < kinds.index(b"mdat")
    return MediaInfo("mp4", "video/mp4", ".mp4", width, height, codec, duration, faststart)


# -- WebM (EBML / Matroska) ---------------------------------------------------

_WEBM_CODECS = {"V_VP8": "VP8", "V_VP9": "VP9", "V_AV1": "AV1"}
_EBML, _DOCTYPE, _SEGMENT = 0x1A45DFA3, 0x4282, 0x18538067
_INFO, _TIMESCALE, _DURATION = 0x1549A966, 0x2AD7B1, 0x4489
_TRACKS, _TRACK_ENTRY, _TRACK_TYPE, _CODEC_ID = 0x1654AE6B, 0xAE, 0x83, 0x86
_VIDEO, _PIXEL_WIDTH, _PIXEL_HEIGHT, _CLUSTER = 0xE0, 0xB0, 0xBA, 0x1F43B675


def _vint_length(data: bytes, pos: int) -> int:
    if pos >= len(data):
        raise MediaCheckError("corrupt")
    first = data[pos]
    length = 1
    while length <= 8 and not first & (0x80 >> (length - 1)):
        length += 1
    if length > 8 or pos + length > len(data):
        raise MediaCheckError("corrupt")
    return length


def _element_id(data: bytes, pos: int) -> tuple[int, int]:
    """Read an EBML element ID (marker bit kept)."""
    length = _vint_length(data, pos)
    return int.from_bytes(data[pos : pos + length], "big"), pos + length


def _element_size(data: bytes, pos: int) -> tuple[int | None, int]:
    """Read an EBML element size; ``None`` means "unknown size"."""
    length = _vint_length(data, pos)
    mask = (1 << (7 * length)) - 1
    value = int.from_bytes(data[pos : pos + length], "big") & mask
    return (None if value == mask else value), pos + length


def _elements(data: bytes, start: int, end: int) -> Iterator[tuple[int, int, int]]:
    """Yield ``(id, payload_start, payload_end)``; stops at the first cluster."""
    pos = start
    while pos < end:
        element_id, pos = _element_id(data, pos)
        size, pos = _element_size(data, pos)
        if element_id == _CLUSTER:
            return
        payload_end = end if size is None else pos + size
        if payload_end > end:
            raise MediaCheckError("corrupt")
        yield element_id, pos, payload_end
        pos = payload_end


def _uint(data: bytes, start: int, end: int) -> int:
    return int.from_bytes(data[start:end], "big")


def _check_webm(data: bytes) -> MediaInfo:  # noqa: PLR0912 - flat walk over the EBML tree
    if len(data) > MAX_VIDEO_BYTES:
        raise MediaCheckError("video_size")
    doctype = ""
    timescale = 1_000_000
    raw_duration: float | None = None
    codec = ""
    width = height = 0
    for element_id, p, e in _elements(data, 0, len(data)):
        if element_id == _EBML:
            for child, cp, ce in _elements(data, p, e):
                if child == _DOCTYPE:
                    doctype = data[cp:ce].decode("ascii", "replace").rstrip("\x00")
        elif element_id == _SEGMENT:
            for child, cp, ce in _elements(data, p, e):
                if child == _INFO:
                    for info, ip, ie in _elements(data, cp, ce):
                        if info == _TIMESCALE:
                            timescale = _uint(data, ip, ie) or timescale
                        elif info == _DURATION and ie - ip in (4, 8):
                            fmt = ">f" if ie - ip == 4 else ">d"
                            raw_duration = struct.unpack_from(fmt, data, ip)[0]
                elif child == _TRACKS:
                    for entry, ep, ee in _elements(data, cp, ce):
                        if entry != _TRACK_ENTRY or codec:
                            continue
                        fields = {f: (fp, fe) for f, fp, fe in _elements(data, ep, ee)}
                        if _TRACK_TYPE not in fields or _uint(data, *fields[_TRACK_TYPE]) != 1:
                            continue
                        if _CODEC_ID in fields:
                            codec = data[slice(*fields[_CODEC_ID])].decode("ascii", "replace")
                        if _VIDEO in fields:
                            for vid, vp, ve in _elements(data, *fields[_VIDEO]):
                                if vid == _PIXEL_WIDTH:
                                    width = _uint(data, vp, ve)
                                elif vid == _PIXEL_HEIGHT:
                                    height = _uint(data, vp, ve)
    if doctype != "webm" or not codec:
        raise MediaCheckError("corrupt")
    name = _WEBM_CODECS.get(codec.rstrip("\x00"))
    if name is None:
        raise MediaCheckError("webm_codec")
    duration = raw_duration * timescale / 1e9 if raw_duration is not None else None
    _check_video_limits(width, height, duration)
    return MediaInfo("webm", "video/webm", ".webm", width, height, name, duration)


# ---------------------------------------------------------------------------
# Setting
# ---------------------------------------------------------------------------

_CACHE_TTL = 60.0
_settings_cache: tuple[float, dict[str, Any]] | None = None


def default_setting() -> dict[str, Any]:
    return {"mode": "video", "fit": "width", "image": None, "mp4": None, "webm": None}


def _normalise(raw: Any) -> dict[str, Any]:
    setting = default_setting()
    if not isinstance(raw, dict):
        return setting
    if raw.get("mode") in MODES:
        setting["mode"] = raw["mode"]
    if raw.get("fit") in FITS:
        setting["fit"] = raw["fit"]
    for slot in ("image", "mp4", "webm"):
        entry = raw.get(slot)
        if isinstance(entry, dict) and _NAME_RE.match(str(entry.get("name", ""))):
            setting[slot] = entry
    return setting


async def load_setting(request: Request) -> dict[str, Any]:
    """The stored background setting (cached for a minute; defaults on any error)."""
    global _settings_cache  # noqa: PLW0603 - per-process cache, reset on save
    now = time.monotonic()
    if _settings_cache is not None and now - _settings_cache[0] < _CACHE_TTL:
        return _settings_cache[1]
    raw: Any = None
    try:
        async with request.app.state.db_session_factory() as db:
            value = (
                await db.execute(select(SiteSetting.value).where(SiteSetting.key == SETTING_KEY))
            ).scalar_one_or_none()
        raw = json.loads(value) if value else None
    except Exception as exc:
        logger.warning("background setting unavailable: %s", exc)
    setting = _normalise(raw)
    _settings_cache = (now, setting)
    return setting


async def _save_setting(request: Request, setting: dict[str, Any]) -> None:
    global _settings_cache  # noqa: PLW0603 - per-process cache, reset on save
    value = json.dumps(setting)
    async with request.app.state.db_session_factory() as db:
        await db.execute(
            insert(SiteSetting)
            .values(key=SETTING_KEY, value=value, description="Login / dashboard background")
            .on_conflict_do_update(
                index_elements=["key"], set_={"value": value, "updated_at": datetime.now(UTC)}
            )
        )
        await db.commit()
    _settings_cache = None


def resolve(setting: dict[str, Any]) -> dict[str, Any]:
    """Template data: ``mode``, ``fit``, ``ratio``, ``image_url`` and ``videos`` (WebM first).

    ``ratio`` (height / width of the shown file) lets the login card sit below
    the picture when it is fitted to the window width.
    """
    image = setting.get("image")
    image_url = PUBLIC_PREFIX + image["name"] if image else BUILTIN_IMAGE
    if setting.get("mp4") or setting.get("webm"):
        videos = [
            {"url": PUBLIC_PREFIX + setting[slot]["name"], "type": f"video/{slot}"}
            for slot in ("webm", "mp4")
            if setting.get(slot)
        ]
    else:
        videos = [dict(v) for v in BUILTIN_VIDEOS]
    mode = setting.get("mode", "video") if videos else "image"
    slots = ("webm", "mp4", "image") if mode == "video" else ("image",)
    shown = next((setting[s] for s in slots if setting.get(s)), None)
    width, height = BUILTIN_SIZE
    if shown and shown.get("width") and shown.get("height"):
        width, height = int(shown["width"]), int(shown["height"])
    return {
        "mode": mode,
        "fit": setting.get("fit", "width"),
        "ratio": round(height / width, 4),
        "image_url": image_url,
        "videos": videos,
    }


async def page_background(request: Request) -> dict[str, Any]:
    """Background data for ``partials/page_background.html``."""
    return resolve(await load_setting(request))


def describe(entry: dict[str, Any] | None) -> str:
    """One-line summary of a stored file for the admin page, e.g. ``MP4 · H.264 · 1668 × 470 · 5.4 s · 411 KB``."""
    if not entry:
        return "Built-in"
    parts = [str(entry.get("ext", "")).lstrip(".").upper(), str(entry.get("codec", ""))]
    if entry.get("width") and entry.get("height"):
        parts.append(f"{entry['width']} × {entry['height']}")
    if entry.get("duration"):
        parts.append(f"{entry['duration']:.1f} s")
    size = int(entry.get("size") or 0)
    parts.append(f"{size / 1048576:.1f} MB" if size >= 1048576 else f"{size // 1024} KB")
    return " · ".join(p for p in parts if p)


async def admin_view(request: Request) -> dict[str, Any]:
    """Data for the Background section of Admin → Layout."""
    setting = await load_setting(request)
    uploaded_video = setting.get("mp4") or setting.get("webm")
    return resolve(setting) | {
        "image_desc": describe(setting.get("image")),
        "video_descs": (
            [describe(setting[s]) for s in ("webm", "mp4") if setting.get(s)]
            if uploaded_video
            else ["Built-in (WebM + MP4)"]
        ),
        "customised": any(setting.get(s) for s in ("image", "mp4", "webm")),
        "errors": ERRORS,
        "limits": {
            "image_mb": MAX_IMAGE_BYTES // 1048576,
            "video_mb": MAX_VIDEO_BYTES // 1048576,
            "min_width": MIN_WIDTH,
            "min_height": MIN_HEIGHT,
            "max_duration": int(MAX_DURATION_S),
        },
    }


# ---------------------------------------------------------------------------
# Public media route
# ---------------------------------------------------------------------------

_MEDIA_CACHE: OrderedDict[str, bytes] = OrderedDict()
_MEDIA_CACHE_BYTES = 150 * 1024 * 1024
_MIME_BY_EXT = {
    "jpg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "mp4": "video/mp4",
    "webm": "video/webm",
}
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _cache_put(name: str, data: bytes) -> None:
    _MEDIA_CACHE[name] = data
    while sum(len(v) for v in _MEDIA_CACHE.values()) > _MEDIA_CACHE_BYTES and len(_MEDIA_CACHE) > 1:
        _MEDIA_CACHE.popitem(last=False)


def ranged_response(data: bytes, media_type: str, range_header: str | None) -> Response:
    """Full or single-range response; browsers fetch video with ``Range: bytes=0-``."""
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=31536000, immutable",
    }
    total = len(data)
    match = _RANGE_RE.match((range_header or "").strip())
    if not match or match.groups() == ("", ""):
        return Response(content=data, media_type=media_type, headers=headers)
    first, last = match.groups()
    if first:
        start = int(first)
        end = min(int(last), total - 1) if last else total - 1
    else:
        start = max(total - int(last), 0)
        end = total - 1
    if start >= total or start > end:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})
    headers["Content-Range"] = f"bytes {start}-{end}/{total}"
    return Response(
        content=data[start : end + 1], status_code=206, media_type=media_type, headers=headers
    )


@background_router.get(PUBLIC_PREFIX + "{name}")
async def serve_background(request: Request, name: str) -> Response:
    """An uploaded background file (public: the login page shows it)."""
    if not _NAME_RE.match(name):
        raise HTTPException(status_code=404, detail="Not found")
    data = _MEDIA_CACHE.get(name)
    if data is None:
        store = getattr(request.app.state, "object_store", None)
        if store is None:
            raise HTTPException(status_code=404, detail="Not found")
        try:
            data = await store.get_object(OBJECT_PREFIX + name, max_bytes=MAX_VIDEO_BYTES)
        except Exception as exc:
            raise HTTPException(status_code=404, detail="Not found") from exc
        _cache_put(name, data)
    else:
        _MEDIA_CACHE.move_to_end(name)
    mime = _MIME_BY_EXT[name.rsplit(".", 1)[1]]
    return ranged_response(data, mime, request.headers.get("range"))


# ---------------------------------------------------------------------------
# Admin actions
# ---------------------------------------------------------------------------


def _redirect(**params: str) -> RedirectResponse:
    query = "".join(f"&{k}={v}" for k, v in params.items())
    return RedirectResponse(url=_ADMIN_URL + query, status_code=303)


async def _check_uploads(files: list[UploadFile]) -> dict[str, tuple[MediaInfo, bytes]]:
    """Read and check the chosen files, at most one per slot (image / mp4 / webm)."""
    checked: dict[str, tuple[MediaInfo, bytes]] = {}
    for upload in files:
        if not upload.filename:
            continue  # empty file input
        data = await upload.read(MAX_VIDEO_BYTES + 1)
        if not data:
            continue
        info = check_media(data)
        if info.slot in checked:
            raise MediaCheckError("duplicate")
        checked[info.slot] = (info, data)
    return checked


@background_router.post("/admin/background/upload", response_model=None)
async def background_upload(
    request: Request, files: list[UploadFile] = File(default=[])
) -> RedirectResponse:
    """Check and store 1–3 files; a video switches to the animation, an image alone to the still.

    A new MP4 without a WebM (or the reverse) drops the old other format, so
    every browser shows the same animation.
    """
    _require_role(request, "admin")
    try:
        checked = await _check_uploads(files)
    except MediaCheckError as exc:
        logger.info("background upload rejected: %s", exc.code)
        return _redirect(bg_error=exc.code)
    if not checked:
        return _redirect(bg_error="empty")

    store = getattr(request.app.state, "object_store", None)
    if store is None:
        return _redirect(bg_error="store")

    setting = dict(await load_setting(request))
    try:
        for slot, (info, data) in checked.items():
            name = hashlib.sha256(data).hexdigest() + info.ext
            await store.put_object(OBJECT_PREFIX + name, data, info.mime)
            setting[slot] = {"name": name, "size": len(data), **asdict(info)}
    except Exception:
        logger.exception("background upload: storing failed")
        return _redirect(bg_error="store")

    videos = {"mp4", "webm"} & checked.keys()
    if videos:
        for slot in {"mp4", "webm"} - videos:
            setting[slot] = None
        setting["mode"] = "video"
    else:
        setting["mode"] = "image"
    try:
        await _save_setting(request, setting)
    except Exception:
        logger.exception("background upload: saving the setting failed")
        return _redirect(bg_error="db")
    logger.info("background updated: %s, mode=%s", sorted(checked), setting["mode"])
    slow = "mp4" in checked and not checked["mp4"][0].faststart
    return _redirect(bg_msg="uploaded_slow" if slow else "uploaded")


@background_router.post("/admin/background/mode", response_model=None)
async def background_mode(request: Request, mode: str = Form(...)) -> RedirectResponse:
    _require_role(request, "admin")
    if mode not in MODES:
        raise HTTPException(status_code=400, detail="Unknown mode")
    setting = dict(await load_setting(request))
    setting["mode"] = mode
    try:
        await _save_setting(request, setting)
    except Exception:
        logger.exception("background mode: saving failed")
        return _redirect(bg_error="db")
    return _redirect(bg_msg="mode")


@background_router.post("/admin/background/fit", response_model=None)
async def background_fit(request: Request, fit: str = Form(...)) -> RedirectResponse:
    _require_role(request, "admin")
    if fit not in FITS:
        raise HTTPException(status_code=400, detail="Unknown fit")
    setting = dict(await load_setting(request))
    setting["fit"] = fit
    try:
        await _save_setting(request, setting)
    except Exception:
        logger.exception("background fit: saving failed")
        return _redirect(bg_error="db")
    return _redirect(bg_msg="fit")


@background_router.post("/admin/background/reset", response_model=None)
async def background_reset(request: Request) -> RedirectResponse:
    _require_role(request, "admin")
    try:
        await _save_setting(request, default_setting())
    except Exception:
        logger.exception("background reset: saving failed")
        return _redirect(bg_error="db")
    return _redirect(bg_msg="reset")
