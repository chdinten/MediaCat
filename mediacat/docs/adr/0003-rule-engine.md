# ADR-0003 — Rule engine: Open Policy Agent (default) with external-API escape hatch

- **Status**: Accepted
- **Date**: 2026-04-16

## Context

Country-specific decoding of vinyl/CD metadata (barcode country
prefixes, matrix/SID codes, catalog-number patterns per label per era,
etc.) is a rules problem that evolves faster than the application code.
Requirements:

1. Rules must be editable without redeploying the app.
2. Rules must be version-controlled and reviewable.
3. A future external rule engine (commercial or SaaS) must be swappable
   behind the same interface.
4. The solution must be open source.

## Decision

Use **Open Policy Agent (OPA)** with Rego policies bundled from a
directory tree. The application talks to OPA over HTTP using the Data
API. A thin Python adapter (`mediacat.rules.engine`) hides the
transport, so swapping OPA for an in-process or external engine is a
one-file change.

Fallback in-process alternative: the `business-rules` Python package,
usable in constrained deployments where running a second service is
undesirable. Selected via `rule_engine.backend` in `config/app.yaml`.

## Consequences

- Policies live under `deploy/opa/bundles/mediacat/` as Rego + JSON
  data files, committed to the repo, loaded into OPA as a bundle.
- A small CI job lints the policies with `opa fmt` and `opa test`.
- The adapter exposes one function: `decode(media_format, fields) ->
  DecodedAttributes`. No OPA types leak into the domain layer.
- Replacing OPA with a commercial engine later is an adapter swap, not
  a refactor.

## Alternatives considered

- **Durable Rules / Drools (JVM)** — JVM footprint too large for a
  single-host deployment; rejected.
- **Pure Python DSL** — maintainable in the short term but re-invents
  OPA's lineage, testing, and policy bundle story; rejected as the
  default but kept as a fallback.
- **Hand-coded conditionals** — fastest initially, catastrophic for
  maintainability; rejected.
