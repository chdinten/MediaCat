# Section 5 — Ingestion service framework

## What this section produces

- `src/mediacat/ingestion/base.py` — abstract ``BaseConnector`` with:
  - **Token-bucket rate limiter** — async, configurable per connector
  - **Retry with exponential backoff** — configurable attempts and delays
  - **Circuit breaker** — opens after N failures, auto-recovers
  - **Request accounting** — every HTTP call logged with timing
- `src/mediacat/ingestion/discogs.py` — Discogs API connector with field
  normalisation (artists, labels, barcodes, matrix/runout, formats, images,
  pressing plants).
- `src/mediacat/ingestion/musicbrainz.py` — MusicBrainz WS2 connector
  with field normalisation.
- `src/mediacat/ingestion/drift.py` — LLM-assisted schema drift detector
  (advisory only). Computes structural diff, generates human-readable
  report for the review queue.
- `src/mediacat/ingestion/queue.py` — Redis-backed job queue using
  ``BLMOVE`` for at-least-once delivery with dead-letter handling.
- `src/mediacat/ingestion/registry.py` — connector discovery from
  ``connectors.yaml``, secret resolution from Docker secrets, factory.

## Security properties

- Credentials are read from Docker secrets (file paths), never from
  YAML or environment variables.
- ``User-Agent`` is always set (Discogs and MusicBrainz require it).
- Rate limiting prevents upstream bans and credential revocation.
- Circuit breaker prevents cascading failures.
- Schema drift is **advisory only** — no executable code is generated.

## Adding a new connector

1. Create ``src/mediacat/ingestion/my_source.py``.
2. Subclass ``BaseConnector``, implement ``fetch_release`` and
   ``search_releases``.
3. Register in ``registry._CONNECTOR_CLASSES``.
4. Add a YAML block in ``config/connectors.yaml``.
5. Create a secret file for auth if needed.
