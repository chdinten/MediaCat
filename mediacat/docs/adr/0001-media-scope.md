# ADR-0001 — First-generation media scope: vinyl + CD

- **Status**: Accepted
- **Date**: 2026-04-16
- **Deciders**: Project owner
- **Supersedes**: —

## Context

The platform ultimately catalogs physical music media. "Label, obi,
runout" terminology in the initial brief points at vinyl, but CD shares
the same ingestion shape (cover art, OBI spine cards on Japanese
pressings, matrix / IFPI codes in lieu of runout etchings). Each
additional media type (cassette, reel, 8-track, MD, SACD) introduces its
own field vocabulary, decoding rules, and image capture conventions,
which multiplies the rule-engine surface and the review UI.

## Decision

First generation supports **vinyl + CD**. The data model uses a
`media_format` enum on the token table with values `vinyl` and `cd`; the
rule-engine bundles are namespaced per format. Additional formats are a
later-generation change that requires (a) a new enum value via
migration, (b) a new rule bundle, and (c) UI labels — but no core
redesign.

## Consequences

- Rule bundles for vinyl runout/matrix and CD matrix/IFPI decoding are
  shipped in Section 6.
- The OBI field is optional on both formats (Japanese pressings of CDs
  also ship with OBI strips).
- Extending to cassette later is cheap; extending to reel or 8-track
  will need new capture metadata (tape speed, track layout) and is
  explicitly out of scope.

## Alternatives considered

- **Vinyl only** — simpler but forces a rewrite to add CD; rejected.
- **All media types** — triples rule-engine work for no v1 user value;
  rejected.
