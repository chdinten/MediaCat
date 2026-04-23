"""Provider-agnostic rule engine interface.

Every rule backend implements :class:`RuleEngine` and exposes a single
method:

    decode(media_format, fields) -> DecodeResult

The caller never sees OPA, Rego, or any backend-specific types.
Swapping the backend is a one-line config change.

Design
------
* Country-specific decoding covers: barcode country prefix, matrix / SID
  codes, catalog-number patterns per label, pressing-plant identification.
* The engine receives raw fields (strings) and returns normalised
  attributes plus confidence and warnings.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DecodeResult:
    """Output of a rule-engine decode call."""

    status: str
    """'matched', 'partial', or 'unknown'."""

    decoded: dict[str, Any]
    """Normalised attributes produced by the rules."""

    warnings: list[str] = field(default_factory=list)
    """Human-readable warnings (e.g. 'barcode prefix not recognised')."""

    rule_ids: list[str] = field(default_factory=list)
    """IDs of rules that fired (for audit)."""

    confidence: float = 1.0
    """Overall decode confidence (0-1)."""


class RuleEngine(Protocol):
    """Interface that every rule backend must implement."""

    async def decode(
        self,
        media_format: str,
        fields: dict[str, Any],
    ) -> DecodeResult:
        """Decode raw fields using country-specific rules.

        Parameters
        ----------
        media_format
            ``"vinyl"`` or ``"cd"``.
        fields
            Raw fields from ingestion / OCR / vision — barcode,
            matrix_runout, catalog_number, label_name, country hint, etc.

        Returns
        -------
        DecodeResult
            Normalised attributes, warnings, and rule provenance.
        """
        ...


def create_rule_engine(
    backend: str = "opa",
    *,
    opa_url: str = "http://opa:8181",
    opa_policy_path: str = "mediacat/decode",
    **kwargs: Any,  # noqa: ARG001 — reserved for backend-specific options
) -> RuleEngine:
    """Factory for rule engine backends.

    Parameters
    ----------
    backend
        ``"opa"`` (default) or ``"local"`` (in-process fallback).
    opa_url
        OPA base URL (only used when backend is ``"opa"``).
    opa_policy_path
        OPA Data API path to query.
    """
    if backend == "opa":
        from mediacat.rules.opa import OpaRuleEngine

        return OpaRuleEngine(base_url=opa_url, policy_path=opa_policy_path)

    if backend in ("local", "business_rules"):
        from mediacat.rules.local import LocalRuleEngine

        return LocalRuleEngine()

    msg = f"Unknown rule engine backend: {backend!r}"
    raise ValueError(msg)
