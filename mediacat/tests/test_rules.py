"""Tests for :mod:`mediacat.rules` — local rule engine."""

from __future__ import annotations

import pytest

from mediacat.rules.engine import create_rule_engine
from mediacat.rules.local import (
    BarcodeCountryRule,
    CatalogPrefixRule,
    LocalRuleEngine,
    MatrixSidRule,
)

# ===========================================================================
# Barcode country rule
# ===========================================================================


class TestBarcodeCountryRule:
    def test_us_barcode(self) -> None:
        rule = BarcodeCountryRule()
        assert rule.match("vinyl", {"barcode": "075021032613"})
        result = rule.apply("vinyl", {"barcode": "075021032613"})
        assert result["country_from_barcode"] == "US"

    def test_jp_barcode(self) -> None:
        rule = BarcodeCountryRule()
        result = rule.apply("vinyl", {"barcode": "4988009046051"})
        assert result["country_from_barcode"] == "JP"

    def test_gb_barcode(self) -> None:
        rule = BarcodeCountryRule()
        result = rule.apply("cd", {"barcode": "5099902932224"})
        assert result["country_from_barcode"] == "GB"

    def test_unknown_prefix(self) -> None:
        rule = BarcodeCountryRule()
        result = rule.apply("vinyl", {"barcode": "999999999999"})
        assert result["country_from_barcode"] is None

    def test_no_barcode_no_match(self) -> None:
        rule = BarcodeCountryRule()
        assert not rule.match("vinyl", {"barcode": ""})
        assert not rule.match("vinyl", {})

    def test_short_barcode_no_match(self) -> None:
        rule = BarcodeCountryRule()
        assert not rule.match("vinyl", {"barcode": "12"})


# ===========================================================================
# Matrix / SID code rule
# ===========================================================================


class TestMatrixSidRule:
    def test_mastering_sid(self) -> None:
        rule = MatrixSidRule()
        fields = {"matrix_runout": "A1 IFPI L553 Some text"}
        assert rule.match("cd", fields)
        result = rule.apply("cd", fields)
        assert len(result["sid_codes"]) == 1
        assert result["sid_codes"][0]["type"] == "mastering"
        assert result["sid_codes"][0]["code"] == "L553"

    def test_mould_sid(self) -> None:
        rule = MatrixSidRule()
        fields = {"matrix_runout": "IFPI 4502"}
        result = rule.apply("cd", fields)
        assert result["sid_codes"][0]["type"] == "mould"

    def test_multiple_sids(self) -> None:
        rule = MatrixSidRule()
        fields = {"matrix_runout": "IFPI L042 blah IFPI 9901"}
        result = rule.apply("cd", fields)
        assert len(result["sid_codes"]) == 2

    def test_no_sid_no_match(self) -> None:
        rule = MatrixSidRule()
        assert not rule.match("cd", {"matrix_runout": "A-1 B-1 no codes"})
        assert not rule.match("cd", {})


# ===========================================================================
# Catalog prefix rule
# ===========================================================================


class TestCatalogPrefixRule:
    def test_mfsl_prefix(self) -> None:
        rule = CatalogPrefixRule()
        fields = {"catalog_number": "MFSL 1-042"}
        assert rule.match("vinyl", fields)
        result = rule.apply("vinyl", fields)
        assert result["label_hint"] == "Mobile Fidelity Sound Lab"

    def test_movlp_prefix(self) -> None:
        rule = CatalogPrefixRule()
        result = rule.apply("vinyl", {"catalog_number": "MOVLP1234"})
        assert result["label_hint"] == "Music On Vinyl"

    def test_unknown_prefix(self) -> None:
        rule = CatalogPrefixRule()
        result = rule.apply("vinyl", {"catalog_number": "XYZ-123"})
        assert result == {}

    def test_no_catalog_no_match(self) -> None:
        rule = CatalogPrefixRule()
        assert not rule.match("vinyl", {"catalog_number": ""})
        assert not rule.match("vinyl", {})


# ===========================================================================
# LocalRuleEngine integration
# ===========================================================================


@pytest.mark.asyncio
async def test_local_engine_decodes_barcode_and_sid() -> None:
    engine = LocalRuleEngine()
    result = await engine.decode(
        "cd",
        {
            "barcode": "4988009046051",
            "matrix_runout": "IFPI L042",
            "catalog_number": "MFSL 1-042",
        },
    )
    assert result.status == "matched"
    assert result.decoded["country_from_barcode"] == "JP"
    assert len(result.decoded["sid_codes"]) == 1
    assert result.decoded["label_hint"] == "Mobile Fidelity Sound Lab"
    assert "barcode_country" in result.rule_ids
    assert "matrix_sid" in result.rule_ids
    assert "catalog_prefix" in result.rule_ids


@pytest.mark.asyncio
async def test_local_engine_no_match() -> None:
    engine = LocalRuleEngine()
    result = await engine.decode("vinyl", {})
    assert result.status == "unknown"
    assert result.confidence == 0.0


# ===========================================================================
# Factory
# ===========================================================================


def test_create_local_engine() -> None:
    engine = create_rule_engine("local")
    assert isinstance(engine, LocalRuleEngine)


def test_create_opa_engine() -> None:
    from mediacat.rules.opa import OpaRuleEngine

    engine = create_rule_engine("opa", opa_url="http://localhost:8181")
    assert isinstance(engine, OpaRuleEngine)


def test_create_unknown_raises() -> None:
    with pytest.raises(ValueError, match="Unknown rule engine"):
        create_rule_engine("nonexistent")
