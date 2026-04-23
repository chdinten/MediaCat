"""Tests for :mod:`mediacat.vision`."""

from __future__ import annotations

import pytest

from mediacat.vision.adapter import HybridVision, VisionResponse, _try_parse_json
from mediacat.vision.candidates import Candidate, CandidateResult
from mediacat.vision.prompts import (
    VISION_SYSTEM,
    get_prompt_for_region,
    label_prompt,
    obi_prompt,
    runout_prompt,
)

# ===========================================================================
# Prompts
# ===========================================================================


def test_label_prompt_returns_tuple() -> None:
    prompt, schema = label_prompt("vinyl")
    assert "label" in prompt.lower()
    assert "vinyl" in prompt
    assert schema["type"] == "object"


def test_label_prompt_cd() -> None:
    prompt, _ = label_prompt("cd")
    assert "cd" in prompt


def test_obi_prompt_mentions_japanese() -> None:
    prompt, schema = obi_prompt()
    assert "japanese" in prompt.lower() or "OBI" in prompt
    assert "japanese_title" in schema["properties"]


def test_runout_prompt_vinyl() -> None:
    prompt, schema = runout_prompt("vinyl")
    assert "dead wax" in prompt.lower()
    assert "matrix_number" in schema["properties"]


def test_runout_prompt_cd() -> None:
    prompt, _ = runout_prompt("cd")
    assert "inner ring" in prompt.lower()


def test_get_prompt_for_region_label() -> None:
    system, user, _schema = get_prompt_for_region("label_a", "vinyl")
    assert system == VISION_SYSTEM
    assert "label" in user.lower()


def test_get_prompt_for_region_obi() -> None:
    _, user, _ = get_prompt_for_region("obi_front", "cd")
    assert "obi" in user.lower() or "OBI" in user


def test_get_prompt_for_region_runout() -> None:
    _, user, _ = get_prompt_for_region("runout_a", "vinyl")
    assert "matrix" in user.lower() or "runout" in user.lower()


def test_get_prompt_for_region_unknown() -> None:
    _, user, _schema = get_prompt_for_region("cover_front", "vinyl")
    assert "text" in user.lower()


# ===========================================================================
# Adapter helpers
# ===========================================================================


def test_try_parse_json_valid() -> None:
    result = _try_parse_json('{"key": "value"}')
    assert result == {"key": "value"}


def test_try_parse_json_with_fences() -> None:
    result = _try_parse_json('```json\n{"key": "value"}\n```')
    assert result == {"key": "value"}


def test_try_parse_json_invalid() -> None:
    assert _try_parse_json("not json") == {}


def test_try_parse_json_array_returns_empty() -> None:
    assert _try_parse_json("[1, 2, 3]") == {}


# ===========================================================================
# Hybrid adapter with mocks
# ===========================================================================


class _MockVision:
    def __init__(self, name: str, response: str = "{}", fail: bool = False) -> None:
        self._name = name
        self._response = response
        self._fail = fail
        self.call_count = 0

    @property
    def provider_name(self) -> str:
        return self._name

    async def transcribe(
        self,
        image_bytes: bytes,
        mime_type: str,
        prompt: str,
        *,
        system: str = "",
        model: str | None = None,
    ) -> VisionResponse:
        self.call_count += 1
        if self._fail:
            raise RuntimeError(f"{self._name} failed")
        parsed = _try_parse_json(self._response)
        return VisionResponse(
            text=self._response,
            parsed=parsed,
            provider=self._name,
            model="test",
        )


@pytest.mark.asyncio
async def test_hybrid_vision_primary() -> None:
    primary = _MockVision("local", '{"label_name": "EMI"}')
    hybrid = HybridVision(primary)
    resp = await hybrid.transcribe(b"fake", "image/jpeg", "test", task="label")
    assert resp.provider == "local"
    assert resp.parsed.get("label_name") == "EMI"


@pytest.mark.asyncio
async def test_hybrid_vision_fallback() -> None:
    primary = _MockVision("local", fail=True)
    fallback = _MockVision("api", '{"label_name": "Decca"}')
    hybrid = HybridVision(primary, fallback)
    resp = await hybrid.transcribe(b"fake", "image/jpeg", "test", task="label")
    assert resp.provider == "api"


@pytest.mark.asyncio
async def test_hybrid_vision_both_fail() -> None:
    primary = _MockVision("local", fail=True)
    fallback = _MockVision("api", fail=True)
    hybrid = HybridVision(primary, fallback)
    with pytest.raises(RuntimeError, match="api failed"):
        await hybrid.transcribe(b"fake", "image/jpeg", "test", task="label")


# ===========================================================================
# Candidate types
# ===========================================================================


def test_candidate_dataclass() -> None:
    c = Candidate(
        token_id="abc", title="Test", artist="Band", score=0.95, match_reasons=["exact_barcode"]
    )
    assert c.score == 0.95
    assert "exact_barcode" in c.match_reasons


def test_candidate_result_novel() -> None:
    result = CandidateResult(candidates=[], proposed_updates={}, is_novel=True)
    assert result.is_novel
