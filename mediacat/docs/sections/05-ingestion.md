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
- `src/mediacat/ingestion/wikipedia.py` — Wikipedia REST summaries, plain-text
  extracts, and MusicBrainz artist links (Wikipedia/Wikidata, homepage, Discogs).
- `src/mediacat/ingestion/artist_bio.py` — artist biography resolution:
  Wikipedia (MusicBrainz link or LLM-verified search), Discogs artist profile
  comparison weighted towards Wikipedia, official homepage summarised by the
  LLM, else a "not found" statement. See [Section 9](09-web.md#artist-page-and-biographies).
- `src/mediacat/ingestion/discogs_extras.py` — pure helpers for Discogs release
  identifiers (runout variants grouped by LP side) and collection custom fields
  (matched by name pattern), stored in `Token.extra`.
- `src/mediacat/ingestion/spotify.py` — Spotify album links for the discography
  (client-credentials; omit `include_groups` / `market`, Spotify rejects them).

## Security properties

- Credentials are read from Docker secrets (file paths), never from
  YAML or environment variables.
- ``User-Agent`` is always set (Discogs and MusicBrainz require it).
- Rate limiting prevents upstream bans and credential revocation.
- Circuit breaker prevents cascading failures.
- Schema drift is **advisory only** — no executable code is generated.
- Homepage fetches for artist bios refuse private, loopback, link-local and
  reserved addresses (SSRF guard) and follow at most two about/bio links.
- Discogs allows about 60 requests/minute per token, shared by the collection
  import and manual refreshes; refreshes retry HTTP 429 after 2, 4 and 6 s.

## Adding a new connector

1. Create ``src/mediacat/ingestion/my_source.py``.
2. Subclass ``BaseConnector``, implement ``fetch_release`` and
   ``search_releases``.
3. Register in ``registry._CONNECTOR_CLASSES``.
4. Add a YAML block in ``config/connectors.yaml``.
5. Create a secret file for auth if needed.
