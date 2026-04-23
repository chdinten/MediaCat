"""Local in-process rule engine — Python fallback.

Implements the same :class:`~mediacat.rules.engine.RuleEngine` interface
using plain Python functions instead of OPA/Rego.  Useful for:

* Deployments where running a second container is undesirable.
* Development and testing without OPA.
* Rapid prototyping of new rules before formalising them in Rego.

Rules are organised as a list of :class:`DecodeRule` instances.  Each
rule has a ``match()`` predicate and an ``apply()`` method.  Rules are
evaluated in order; the first match wins (unless ``continue_after``
is set, in which case later rules can augment the result).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from mediacat.rules.engine import DecodeResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Rule definition
# ---------------------------------------------------------------------------


@dataclass
class DecodeRule:
    """A single decode rule."""

    rule_id: str
    description: str
    media_formats: list[str]  # which formats this rule applies to

    def match(self, media_format: str, fields: dict[str, Any]) -> bool:
        """Return True if this rule should fire."""
        raise NotImplementedError

    def apply(self, media_format: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Return decoded attributes."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Barcode country prefix rules
# ---------------------------------------------------------------------------

# EAN-13 prefix → country (selected; extend as needed)
_BARCODE_PREFIXES: list[tuple[str, str, str]] = [
    ("000", "099", "US"),
    ("100", "139", "US"),
    ("300", "379", "FR"),
    ("400", "440", "DE"),
    ("450", "459", "JP"),
    ("460", "469", "RU"),
    ("471", "471", "TW"),
    ("489", "489", "HK"),
    ("490", "499", "JP"),
    ("500", "509", "GB"),
    ("520", "521", "GR"),
    ("528", "528", "LB"),
    ("530", "530", "XK"),
    ("531", "531", "MK"),
    ("535", "535", "MT"),
    ("539", "539", "IE"),
    ("540", "549", "BE"),
    ("560", "560", "PT"),
    ("569", "569", "IS"),
    ("570", "579", "DK"),
    ("590", "590", "PL"),
    ("599", "599", "HU"),
    ("600", "601", "ZA"),
    ("609", "609", "MU"),
    ("611", "611", "MA"),
    ("613", "613", "DZ"),
    ("616", "616", "KE"),
    ("618", "618", "CI"),
    ("619", "619", "TN"),
    ("621", "621", "SY"),
    ("622", "622", "EG"),
    ("690", "699", "CN"),
    ("700", "709", "NO"),
    ("729", "729", "IL"),
    ("730", "739", "SE"),
    ("740", "740", "GT"),
    ("750", "750", "MX"),
    ("754", "755", "CA"),
    ("760", "769", "CH"),
    ("770", "771", "CO"),
    ("773", "773", "UY"),
    ("775", "775", "PE"),
    ("777", "777", "BO"),
    ("779", "779", "AR"),
    ("780", "780", "CL"),
    ("784", "784", "PY"),
    ("786", "786", "EC"),
    ("789", "790", "BR"),
    ("800", "839", "IT"),
    ("840", "849", "ES"),
    ("850", "850", "CU"),
    ("858", "858", "SK"),
    ("859", "859", "CZ"),
    ("860", "860", "RS"),
    ("865", "865", "MN"),
    ("867", "867", "KP"),
    ("868", "869", "TR"),
    ("870", "879", "NL"),
    ("880", "880", "KR"),
    ("885", "885", "TH"),
    ("888", "888", "SG"),
    ("890", "890", "IN"),
    ("893", "893", "VN"),
    ("896", "896", "PK"),
    ("899", "899", "ID"),
    ("900", "919", "AT"),
    ("930", "939", "AU"),
    ("940", "949", "NZ"),
    ("950", "950", "GS1_HQ"),
    ("955", "955", "MY"),
    ("958", "958", "MO"),
    ("977", "977", "ISSN"),
    ("978", "979", "ISBN"),
]


class BarcodeCountryRule(DecodeRule):
    """Decode country from EAN-13 barcode prefix."""

    def __init__(self) -> None:
        super().__init__(
            rule_id="barcode_country",
            description="Derive country from EAN-13 barcode prefix",
            media_formats=["vinyl", "cd"],
        )

    def match(self, media_format: str, fields: dict[str, Any]) -> bool:
        barcode = fields.get("barcode", "")
        return bool(barcode) and len(barcode) >= 3 and barcode[:3].isdigit()

    def apply(self, media_format: str, fields: dict[str, Any]) -> dict[str, Any]:
        barcode = fields["barcode"]
        prefix = barcode[:3]
        prefix_int = int(prefix)
        for low, high, country in _BARCODE_PREFIXES:
            if int(low) <= prefix_int <= int(high):
                return {"country_from_barcode": country}
        return {"country_from_barcode": None}


# ---------------------------------------------------------------------------
# Matrix / SID code rules
# ---------------------------------------------------------------------------

# IFPI SID code pattern: IFPI Lxxx (mastering) or IFPI xxxx (mould)
_IFPI_PATTERN = re.compile(r"IFPI\s+([A-Z0-9]{4,5})", re.IGNORECASE)


class MatrixSidRule(DecodeRule):
    """Extract IFPI SID codes from matrix/runout text."""

    def __init__(self) -> None:
        super().__init__(
            rule_id="matrix_sid",
            description="Extract IFPI mastering/mould SID codes from runout",
            media_formats=["vinyl", "cd"],
        )

    def match(self, media_format: str, fields: dict[str, Any]) -> bool:
        runout = fields.get("matrix_runout", "")
        return bool(_IFPI_PATTERN.search(runout)) if runout else False

    def apply(self, media_format: str, fields: dict[str, Any]) -> dict[str, Any]:
        runout = fields["matrix_runout"]
        matches = _IFPI_PATTERN.findall(runout)
        sid_codes: list[dict[str, str]] = []
        for code in matches:
            code_upper = code.upper()
            if code_upper.startswith("L"):
                sid_codes.append({"type": "mastering", "code": code_upper})
            else:
                sid_codes.append({"type": "mould", "code": code_upper})
        return {"sid_codes": sid_codes}


# ---------------------------------------------------------------------------
# Catalog number label hint
# ---------------------------------------------------------------------------

# Well-known catalog number prefixes (extensible)
_CATALOG_PREFIXES: list[tuple[str, str]] = [
    ("MFSL", "Mobile Fidelity Sound Lab"),
    ("UHQR", "Mobile Fidelity Sound Lab"),
    ("AP", "Analogue Productions"),
    ("ORG", "ORG Music"),
    ("MOVLP", "Music On Vinyl"),
    ("MOFI", "Mobile Fidelity Sound Lab"),
    ("UIJY", "Universal Music Japan"),
    ("UICY", "Universal Music Japan"),
    ("SICP", "Sony Music Japan"),
    ("TOCP", "Toshiba-EMI Japan"),
]


class CatalogPrefixRule(DecodeRule):
    """Suggest label from well-known catalog number prefixes."""

    def __init__(self) -> None:
        super().__init__(
            rule_id="catalog_prefix",
            description="Suggest label from catalog number prefix",
            media_formats=["vinyl", "cd"],
        )

    def match(self, media_format: str, fields: dict[str, Any]) -> bool:
        cat = fields.get("catalog_number", "")
        return bool(cat) and len(cat) >= 2

    def apply(self, media_format: str, fields: dict[str, Any]) -> dict[str, Any]:
        cat = fields["catalog_number"].upper().replace("-", "").replace(" ", "")
        for prefix, label in _CATALOG_PREFIXES:
            if cat.startswith(prefix):
                return {"label_hint": label, "label_hint_source": "catalog_prefix"}
        return {}


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

# All built-in rules in evaluation order.
_BUILTIN_RULES: list[DecodeRule] = [
    BarcodeCountryRule(),
    MatrixSidRule(),
    CatalogPrefixRule(),
]


class LocalRuleEngine:
    """In-process rule engine using Python rule objects.

    Parameters
    ----------
    extra_rules
        Additional rules to append after the built-ins.
    """

    def __init__(self, extra_rules: list[DecodeRule] | None = None) -> None:
        self._rules = list(_BUILTIN_RULES)
        if extra_rules:
            self._rules.extend(extra_rules)

    async def decode(
        self,
        media_format: str,
        fields: dict[str, Any],
    ) -> DecodeResult:
        """Evaluate all matching rules and merge results."""
        decoded: dict[str, Any] = {}
        warnings: list[str] = []
        fired: list[str] = []

        for rule in self._rules:
            if media_format not in rule.media_formats:
                continue
            try:
                if rule.match(media_format, fields):
                    result = rule.apply(media_format, fields)
                    decoded.update(result)
                    fired.append(rule.rule_id)
            except Exception as exc:
                warnings.append(f"Rule {rule.rule_id} failed: {exc}")
                logger.exception("Rule %s failed", rule.rule_id)

        status = "matched" if fired else "unknown"
        return DecodeResult(
            status=status,
            decoded=decoded,
            warnings=warnings,
            rule_ids=fired,
            confidence=1.0 if fired else 0.0,
        )
