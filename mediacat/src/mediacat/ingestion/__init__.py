"""Ingestion service framework — connector-based data acquisition.

Submodules
----------
* ``base``       — abstract connector class and common types
* ``config``     — YAML connector config loader and validator
* ``registry``   — connector discovery and instantiation
* ``discogs``    — Discogs API connector
* ``musicbrainz``— MusicBrainz API connector
* ``drift``      — LLM-assisted schema drift detection (advisory)
* ``queue``      — Redis-backed job queue
"""

from __future__ import annotations
