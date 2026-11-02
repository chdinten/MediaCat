"""Login / dashboard background: format checks, setting, media route, admin upload."""

from __future__ import annotations

import io
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.templating import Jinja2Templates
from PIL import Image
from starlette.datastructures import Headers, UploadFile
from starlette.requests import Request

from mediacat.web import background as bg
from mediacat.web.app import _WEB_DIR
from mediacat.web.middleware import _is_public

STATIC = _WEB_DIR / "static"
MP4 = (STATIC / "media-cat-hero.mp4").read_bytes()
WEBM = (STATIC / "media-cat-hero.webm").read_bytes()
POSTER = (STATIC / "media-cat-hero-poster.webp").read_bytes()


def _png(width: int, height: int, **save: Any) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "black").save(buf, "PNG", **save)
    return buf.getvalue()


def _code(data: bytes) -> str:
    with pytest.raises(bg.MediaCheckError) as exc:
        bg.check_media(data)
    return exc.value.code


# -- format check -------------------------------------------------------------


def test_builtin_mp4_is_h264_faststart() -> None:
    info = bg.check_media(MP4)
    assert (info.slot, info.mime, info.codec) == ("mp4", "video/mp4", "H.264")
    assert (info.width, info.height) == (1668, 470)
    assert info.duration == pytest.approx(5.4, abs=0.05)
    assert info.faststart


def test_builtin_webm_is_vp9() -> None:
    info = bg.check_media(WEBM)
    assert (info.slot, info.codec, info.width, info.height) == ("webm", "VP9", 1668, 470)
    assert info.duration == pytest.approx(5.4, abs=0.05)


def test_builtin_poster_is_a_webp_still() -> None:
    info = bg.check_media(POSTER)
    assert (info.slot, info.mime, info.width, info.height) == ("image", "image/webp", 1668, 470)


def test_hevc_mp4_is_rejected() -> None:
    assert _code(MP4.replace(b"avc1", b"hvc1")) == "codec"


def test_truncated_mp4_is_rejected() -> None:
    assert _code(MP4[: len(MP4) // 2]) == "corrupt"


def test_moov_after_mdat_is_reported() -> None:
    ftyp_len = int.from_bytes(MP4[:4], "big")
    boxes, pos = [], ftyp_len
    while pos < len(MP4):
        size = int.from_bytes(MP4[pos : pos + 4], "big")
        boxes.append(MP4[pos : pos + size])
        pos += size
    reordered = MP4[:ftyp_len] + b"".join(sorted(boxes, key=lambda b: b[4:8] == b"moov"))
    assert not bg.check_media(reordered).faststart


def test_webm_with_unknown_codec_is_rejected() -> None:
    assert _code(WEBM.replace(b"V_VP9", b"V_XYZ", 1)) == "webm_codec"


def test_image_checks() -> None:
    assert bg.check_media(_png(1600, 400)).mime == "image/png"
    assert _code(_png(800, 400)) == "image_dims"
    assert _code(b"GIF89a" + b"\0" * 64) == "type"
    assert _code(b"\x89PNG\r\n\x1a\n" + b"\0" * 64) == "corrupt"


def test_animated_image_is_rejected() -> None:
    buf = io.BytesIO()
    frames = [Image.new("RGB", (1600, 400), c) for c in ("black", "white")]
    frames[0].save(buf, "WEBP", save_all=True, append_images=frames[1:])
    assert _code(buf.getvalue()) == "animated"


def test_size_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bg, "MAX_VIDEO_BYTES", 1000)
    assert _code(MP4) == "video_size"
    assert _code(WEBM) == "video_size"


# -- setting ------------------------------------------------------------------


def test_resolve_defaults_to_the_builtin_animation() -> None:
    r = bg.resolve(bg.default_setting())
    assert (r["mode"], r["fit"]) == ("video", "width")
    assert r["ratio"] == round(470 / 1668, 4)
    assert r["image_url"] == bg.BUILTIN_IMAGE
    assert [v["type"] for v in r["videos"]] == ["video/webm", "video/mp4"]


def test_resolve_uploaded_files() -> None:
    name = "a" * 64
    setting = bg.default_setting() | {
        "mode": "image",
        "image": {"name": name + ".jpg"},
        "mp4": {"name": name + ".mp4"},
    }
    r = bg.resolve(setting)
    assert r["mode"] == "image"
    assert r["image_url"] == bg.PUBLIC_PREFIX + name + ".jpg"
    assert r["videos"] == [{"url": bg.PUBLIC_PREFIX + name + ".mp4", "type": "video/mp4"}]


def test_normalise_drops_unsafe_names() -> None:
    s = bg._normalise({"mode": "bogus", "fit": "zoom", "image": {"name": "../etc/passwd"}})
    assert s == bg.default_setting()


def test_describe() -> None:
    entry = {"ext": ".mp4", "codec": "H.264", "width": 1668, "height": 470}
    assert bg.describe(entry | {"duration": 5.4, "size": 420456}) == (
        "MP4 · H.264 · 1668 × 470 · 5.4 s · 410 KB"
    )
    assert bg.describe(None) == "Built-in"


# -- media route --------------------------------------------------------------


def test_ranged_response() -> None:
    data = bytes(range(10))
    full = bg.ranged_response(data, "video/mp4", None)
    assert (full.status_code, full.body) == (200, data)
    assert full.headers["accept-ranges"] == "bytes"
    part = bg.ranged_response(data, "video/mp4", "bytes=2-4")
    assert (part.status_code, part.body) == (206, data[2:5])
    assert part.headers["content-range"] == "bytes 2-4/10"
    assert bg.ranged_response(data, "video/mp4", "bytes=0-").body == data
    assert bg.ranged_response(data, "video/mp4", "bytes=-3").body == data[7:]
    assert bg.ranged_response(data, "video/mp4", "bytes=20-").status_code == 416


def test_background_media_is_public() -> None:
    assert _is_public(bg.PUBLIC_PREFIX + "x.mp4")


class _Store:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put_object(self, key: str, data: bytes, mime: str) -> None:
        self.objects[key] = data

    async def get_object(self, key: str, bucket: str | None = None, **_: Any) -> bytes:
        return self.objects[key]


def _request(
    store: _Store | None = None, headers: list[tuple[bytes, bytes]] | None = None
) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "headers": headers or [],
        "app": SimpleNamespace(state=SimpleNamespace(object_store=store)),
        "state": {},
    }
    req = Request(scope)
    req.state.user_role = "admin"
    req.state.session = {}
    return req


async def test_serve_background_rejects_other_names() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        await bg.serve_background(_request(_Store()), "../secret.mp4")


async def test_serve_background_reads_the_store_once() -> None:
    bg._MEDIA_CACHE.clear()
    store = _Store()
    name = "b" * 64 + ".mp4"
    store.objects[bg.OBJECT_PREFIX + name] = MP4
    resp = await bg.serve_background(_request(store, [(b"range", b"bytes=0-99")]), name)
    assert (resp.status_code, resp.body, resp.media_type) == (206, MP4[:100], "video/mp4")
    store.objects.clear()
    assert (await bg.serve_background(_request(store), name)).body == MP4  # from the cache
    bg._MEDIA_CACHE.clear()


# -- admin upload -------------------------------------------------------------


@pytest.fixture
def saved(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    saves: list[dict[str, Any]] = []
    current = {"setting": bg.default_setting()}

    async def load(_request: Request) -> dict[str, Any]:
        return current["setting"]

    async def save(_request: Request, setting: dict[str, Any]) -> None:
        saves.append(setting)
        current["setting"] = setting

    monkeypatch.setattr(bg, "load_setting", load)
    monkeypatch.setattr(bg, "_save_setting", save)
    return saves


def _upload(name: str, data: bytes) -> UploadFile:
    return UploadFile(io.BytesIO(data), filename=name, headers=Headers({}))


async def test_upload_video_activates_the_animation(saved: list[dict[str, Any]]) -> None:
    store = _Store()
    resp = await bg.background_upload(
        _request(store), files=[_upload("a.mp4", MP4), _upload("p.webp", POSTER)]
    )
    assert resp.headers["location"] == "/admin?tab=layout&bg_msg=uploaded"
    setting = saved[-1]
    assert setting["mode"] == "video"
    assert setting["mp4"]["codec"] == "H.264"
    assert setting["image"]["ext"] == ".webp"
    assert setting["webm"] is None
    assert bg.OBJECT_PREFIX + setting["mp4"]["name"] in store.objects


async def test_new_mp4_drops_the_old_webm(saved: list[dict[str, Any]]) -> None:
    store = _Store()
    await bg.background_upload(_request(store), files=[_upload("a.webm", WEBM)])
    assert saved[-1]["webm"] is not None
    await bg.background_upload(_request(store), files=[_upload("a.mp4", MP4)])
    assert saved[-1]["webm"] is None
    assert saved[-1]["mp4"] is not None


async def test_image_alone_activates_the_still(saved: list[dict[str, Any]]) -> None:
    await bg.background_upload(_request(_Store()), files=[_upload("p.png", _png(1600, 400))])
    assert saved[-1]["mode"] == "image"


async def test_rejected_upload_saves_nothing(saved: list[dict[str, Any]]) -> None:
    resp = await bg.background_upload(
        _request(_Store()), files=[_upload("a.mp4", MP4.replace(b"avc1", b"hvc1"))]
    )
    assert resp.headers["location"] == "/admin?tab=layout&bg_error=codec"
    assert saved == []
    resp = await bg.background_upload(
        _request(_Store()), files=[_upload("a.mp4", MP4), _upload("b.mp4", MP4)]
    )
    assert resp.headers["location"].endswith("bg_error=duplicate")
    resp = await bg.background_upload(_request(None), files=[_upload("a.mp4", MP4)])
    assert resp.headers["location"].endswith("bg_error=store")
    resp = await bg.background_upload(_request(_Store()), files=[])
    assert resp.headers["location"].endswith("bg_error=empty")
    assert saved == []


async def test_mode_switch(saved: list[dict[str, Any]]) -> None:
    resp = await bg.background_mode(_request(), mode="image")
    assert resp.headers["location"].endswith("bg_msg=mode")
    assert saved[-1]["mode"] == "image"


# -- templates ----------------------------------------------------------------


def _render(background: dict[str, Any]) -> str:
    env = Jinja2Templates(directory=str(_WEB_DIR / "templates")).env
    return env.get_template("partials/page_background.html").render(background=background)


def test_partial_renders_video_over_the_still() -> None:
    html = _render(bg.resolve(bg.default_setting()))
    assert f'poster="{bg.BUILTIN_IMAGE}"' in html
    assert html.index("video/webm") < html.index("video/mp4")
    assert "autoplay muted loop playsinline" in html


def test_partial_still_mode_has_no_video() -> None:
    html = _render(bg.resolve(bg.default_setting() | {"mode": "image"}))
    assert "<video" not in html
    assert bg.BUILTIN_IMAGE in html


def test_ratio_follows_the_shown_file() -> None:
    name = "c" * 64
    setting = bg.default_setting() | {
        "image": {"name": name + ".jpg", "width": 1600, "height": 900},
        "mp4": {"name": name + ".mp4", "width": 1920, "height": 1080},
    }
    assert bg.resolve(setting)["ratio"] == 0.5625
    assert bg.resolve(setting | {"mode": "image"})["ratio"] == 0.5625
    setting["image"] = {"name": name + ".jpg", "width": 2000, "height": 500}
    assert bg.resolve(setting | {"mode": "image"})["ratio"] == 0.25


async def test_fit_switch(saved: list[dict[str, Any]]) -> None:
    resp = await bg.background_fit(_request(), fit="cover")
    assert resp.headers["location"].endswith("bg_msg=fit")
    assert saved[-1]["fit"] == "cover"


def test_partial_carries_fit_and_ratio() -> None:
    html = _render(bg.resolve(bg.default_setting()))
    assert 'class="dash-bg--width"' in html
    assert "--bg-ratio: 0.2818" in html
