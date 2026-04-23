# Section 6 — Rule engine

## What this section produces

- `src/mediacat/rules/engine.py` — `RuleEngine` protocol, `DecodeResult`
  dataclass, factory function.
- `src/mediacat/rules/opa.py` — OPA HTTP adapter (POST /v1/data/).
- `src/mediacat/rules/local.py` — in-process fallback with three
  built-in rules:
  - `BarcodeCountryRule` — EAN-13 prefix → country code lookup.
  - `MatrixSidRule` — IFPI mastering/mould SID code extraction.
  - `CatalogPrefixRule` — well-known catalog prefixes → label hint.
- `deploy/opa/bundles/mediacat/` — Rego policies + data.json for OPA.

## Swapping the engine

Change `rule_engine.backend` in `config/app.yaml` from `"opa"` to
`"local"` (or vice versa). The adapter interface is identical; callers
never see backend-specific types.

To add a future external engine API, implement the `RuleEngine` protocol
and register it in the factory.
