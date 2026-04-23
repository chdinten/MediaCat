"""Tests for :mod:`mediacat.storage` — unit tests with no external deps."""

from __future__ import annotations

import pytest

from mediacat.storage.object_store import ALLOWED_MIME_TYPES, StoredObject, _mime_to_ext
from mediacat.storage.ocr import _parse_tsv
from mediacat.storage.translation import (
    create_translator,
    detect_is_english,
)

# ---------------------------------------------------------------------------
# Object store helpers
# ---------------------------------------------------------------------------


def test_mime_to_ext_known() -> None:
    assert _mime_to_ext("image/jpeg") == ".jpg"
    assert _mime_to_ext("image/png") == ".png"


def test_mime_to_ext_unknown() -> None:
    assert _mime_to_ext("application/pdf") == ""


def test_allowed_mime_types_is_frozen() -> None:
    assert isinstance(ALLOWED_MIME_TYPES, frozenset)
    assert "image/jpeg" in ALLOWED_MIME_TYPES
    assert "application/pdf" not in ALLOWED_MIME_TYPES


def test_stored_object_is_immutable() -> None:
    obj = StoredObject(
        content_hash="abc123",
        bucket="test",
        object_key="ab/c1/abc123.jpg",
        size_bytes=1024,
        mime_type="image/jpeg",
        width_px=100,
        height_px=200,
    )
    assert obj.content_hash == "abc123"
    with pytest.raises(AttributeError):
        obj.content_hash = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# OCR TSV parsing
# ---------------------------------------------------------------------------


def test_parse_tsv_extracts_words_and_confidence() -> None:
    tsv = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
        "left\ttop\twidth\theight\tconf\ttext\n"
        "5\t1\t1\t1\t1\t1\t10\t20\t50\t15\t95\tHello\n"
        "5\t1\t1\t1\t1\t2\t70\t20\t60\t15\t88\tWorld\n"
    )
    result = _parse_tsv(tsv)
    assert result.raw_text == "Hello World"
    assert result.confidence is not None
    assert 0.8 < result.confidence < 1.0  # avg of 95% and 88% normalised
    assert result.engine == "tesseract"


def test_parse_tsv_empty_returns_empty() -> None:
    result = _parse_tsv("level\theader\n")
    assert result.raw_text == ""
    assert result.confidence == 0.0


def test_parse_tsv_skips_low_confidence() -> None:
    tsv = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
        "left\ttop\twidth\theight\tconf\ttext\n"
        "5\t1\t1\t1\t1\t1\t10\t20\t50\t15\t-1\t\n"
        "5\t1\t1\t1\t1\t2\t70\t20\t60\t15\t90\tOnly\n"
    )
    result = _parse_tsv(tsv)
    assert result.raw_text == "Only"


# ---------------------------------------------------------------------------
# Translation heuristics
# ---------------------------------------------------------------------------


def test_detect_is_english_ascii_text() -> None:
    assert detect_is_english("Hello world, this is a test.") is True


def test_detect_is_english_japanese() -> None:
    assert detect_is_english("こんにちは世界") is False


def test_detect_is_english_empty() -> None:
    assert detect_is_english("") is True


def test_detect_is_english_mixed() -> None:
    # Mostly ASCII with a few non-Latin characters
    text = "Hello world こんにちは" + " " * 50
    assert detect_is_english(text) is True


@pytest.mark.asyncio
async def test_passthrough_translator() -> None:
    t = create_translator("passthrough")
    result = await t.translate("Hello world")
    assert result.translated_text == "Hello world"
    assert result.was_translated is False
    assert result.source_language == "en"


@pytest.mark.asyncio
async def test_passthrough_translator_non_english() -> None:
    t = create_translator("passthrough")
    result = await t.translate("こんにちは世界")
    assert result.translated_text == "こんにちは世界"
    assert result.was_translated is False
    assert result.source_language is None  # couldn't detect


def test_create_translator_llm_requires_callable() -> None:
    with pytest.raises(ValueError, match="llm_call is required"):
        create_translator("llm")


@pytest.mark.asyncio
async def test_llm_translator_calls_backend() -> None:
    async def mock_llm(system: str, user: str) -> str:
        return "Translated text here"

    t = create_translator("llm", llm_call=mock_llm)
    result = await t.translate("Texte en français", source_language="fr")
    assert result.was_translated is True
    assert result.translated_text == "Translated text here"
    assert result.source_language == "fr"


@pytest.mark.asyncio
async def test_llm_translator_skips_english() -> None:
    call_count = 0

    async def mock_llm(system: str, user: str) -> str:
        nonlocal call_count
        call_count += 1
        return user

    t = create_translator("llm", llm_call=mock_llm)
    result = await t.translate("Hello world", source_language="en")
    assert result.was_translated is False
    assert call_count == 0
