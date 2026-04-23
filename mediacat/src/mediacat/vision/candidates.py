"""Candidate matching — match vision transcription output against tokens.

After the VLM extracts structured fields from an image, this module
searches the token table for potential matches using a combination of:

1. **Exact match** on barcode, catalog number, or matrix/runout.
2. **Trigram similarity** on title and artist (uses ``pg_trgm``).
3. **External ID match** if a Discogs or MusicBrainz ID was extracted.

Results are ranked by a composite score and returned as a candidate list.
The caller writes the candidates to the review queue; they are never
auto-applied to the reference table.

Design invariant
-----------------
Vision proposals are **always** written to the review queue. They never
directly update the token or reference tables without human approval.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mediacat.db.models import Label, Manufacturer, Symbol, Token

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Candidate:
    """A potential match from the token table."""

    token_id: str
    """UUID of the matched token."""

    title: str | None
    artist: str | None
    score: float
    """Composite match score (0-1)."""

    match_reasons: list[str] = field(default_factory=list)
    """Which fields contributed to the match."""


@dataclass(frozen=True, slots=True)
class CandidateResult:
    """Result of a candidate search."""

    candidates: list[Candidate]
    """Ranked list of matches (best first)."""

    proposed_updates: dict[str, Any]
    """Fields from vision output that differ from the best match."""

    is_novel: bool
    """True if no matches were found — suggests a new token."""


async def find_candidates(
    session: AsyncSession,
    vision_fields: dict[str, Any],
    *,
    max_candidates: int = 10,
    similarity_threshold: float = 0.3,
) -> CandidateResult:
    """Search for tokens matching the vision-extracted fields.

    Parameters
    ----------
    session
        Database session.
    vision_fields
        Structured output from the VLM (label_name, catalog_number,
        artist, title, barcode, matrix_number, etc.).
    max_candidates
        Maximum number of candidates to return.
    similarity_threshold
        Minimum trigram similarity score (0-1).
    """
    candidates: list[Candidate] = []

    # --- Exact match on barcode ---
    barcode = vision_fields.get("barcode") or vision_fields.get("catalog_number")
    if barcode:
        stmt = (
            select(Token)
            .where(or_(Token.barcode == barcode, Token.catalog_number == barcode))
            .limit(max_candidates)
        )
        result = await session.execute(stmt)
        for token in result.scalars():
            candidates.append(
                Candidate(
                    token_id=str(token.id),
                    title=token.title,
                    artist=token.artist,
                    score=1.0,
                    match_reasons=["exact_barcode_or_catalog"],
                )
            )

    # --- Trigram similarity on title + artist ---
    title = vision_fields.get("title")
    artist = vision_fields.get("artist")
    if title and not candidates:
        similarity_col = func.similarity(Token.title, title).label("sim")
        stmt = (
            select(Token, similarity_col)
            .where(
                text("similarity(tokens.title, :title) > :threshold").bindparams(
                    title=title, threshold=similarity_threshold
                )
            )
            .order_by(similarity_col.desc())
            .limit(max_candidates)
        )
        result = await session.execute(stmt)
        for row in result:
            token = row[0]
            sim = float(row[1])
            # Boost if artist also matches
            artist_boost = 0.0
            if artist and token.artist:
                artist_sim_stmt = select(func.similarity(Token.artist, artist)).where(
                    Token.id == token.id
                )
                artist_result = await session.execute(artist_sim_stmt)
                artist_sim = artist_result.scalar() or 0.0
                artist_boost = float(artist_sim) * 0.3

            score = min(sim + artist_boost, 1.0)
            reasons = [f"title_similarity={sim:.2f}"]
            if artist_boost > 0:
                reasons.append(f"artist_boost={artist_boost:.2f}")
            candidates.append(
                Candidate(
                    token_id=str(token.id),
                    title=token.title,
                    artist=token.artist,
                    score=score,
                    match_reasons=reasons,
                )
            )

    # --- Deduplicate and sort ---
    seen: set[str] = set()
    unique: list[Candidate] = []
    for c in sorted(candidates, key=lambda x: x.score, reverse=True):
        if c.token_id not in seen:
            seen.add(c.token_id)
            unique.append(c)
    candidates = unique[:max_candidates]

    # --- Compute proposed updates (diff against best match) ---
    proposed: dict[str, Any] = {}
    if candidates:
        # Only propose updates for fields the vision model extracted
        # that differ from the best match
        for field_name in ("title", "artist", "label_name", "catalog_number"):
            vision_val = vision_fields.get(field_name)
            if vision_val and field_name in ("title", "artist"):
                best = candidates[0]
                existing = getattr(best, field_name, None)
                if existing and existing != vision_val:
                    proposed[field_name] = {
                        "current": existing,
                        "proposed": vision_val,
                    }

    return CandidateResult(
        candidates=candidates,
        proposed_updates=proposed,
        is_novel=len(candidates) == 0,
    )


async def find_label_candidates(
    session: AsyncSession,
    label_name: str,
    *,
    max_results: int = 5,
    threshold: float = 0.3,
) -> list[dict[str, Any]]:
    """Search the labels table by trigram similarity.

    Returns a list of dicts with ``id``, ``name``, ``score``.
    """
    similarity_col = func.similarity(Label.name_normalised, label_name.lower()).label("sim")
    stmt = (
        select(Label, similarity_col)
        .where(
            text("similarity(labels.name_normalised, :name) > :threshold").bindparams(
                name=label_name.lower(), threshold=threshold
            )
        )
        .order_by(similarity_col.desc())
        .limit(max_results)
    )
    result = await session.execute(stmt)
    return [{"id": str(row[0].id), "name": row[0].name, "score": float(row[1])} for row in result]


async def find_manufacturer_candidates(
    session: AsyncSession,
    name: str,
    *,
    max_results: int = 5,
    threshold: float = 0.3,
) -> list[dict[str, Any]]:
    """Search the manufacturers table by trigram similarity."""
    similarity_col = func.similarity(Manufacturer.name_normalised, name.lower()).label("sim")
    stmt = (
        select(Manufacturer, similarity_col)
        .where(
            text("similarity(manufacturers.name_normalised, :name) > :threshold").bindparams(
                name=name.lower(), threshold=threshold
            )
        )
        .order_by(similarity_col.desc())
        .limit(max_results)
    )
    result = await session.execute(stmt)
    return [{"id": str(row[0].id), "name": row[0].name, "score": float(row[1])} for row in result]


async def find_symbol_candidates(
    session: AsyncSession,
    detections: list[dict[str, Any]],
    *,
    max_results: int = 5,
) -> list[dict[str, Any]]:
    """Match vision-detected symbols against the symbol registry.

    Each detection comes from ``symbol_detections`` in the runout vision
    output or from ``symbol_identification_prompt`` results.  A detection
    must have at least one of ``slug_suggestion`` or ``unicode_approx``.

    Returns a list of dicts with:
      ``detection``  — the original detection dict
      ``matches``    — list of {id, slug, name, category, score, match_by}

    Results are always written to the review queue; never auto-applied.
    """
    output: list[dict[str, Any]] = []

    for detection in detections:
        slug_hint: str | None = detection.get("slug_suggestion")
        unicode_hint: str | None = detection.get("unicode_approx")
        matches: list[dict[str, Any]] = []

        # Exact slug lookup — highest confidence path
        if slug_hint:
            stmt = (
                select(Symbol)
                .where(Symbol.slug == slug_hint, Symbol.deleted_at.is_(None))
                .limit(1)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is not None:
                matches.append({
                    "id": str(row.id),
                    "slug": row.slug,
                    "name": row.name,
                    "category": row.category.value,
                    "score": 1.0,
                    "match_by": "exact_slug",
                })

        # Unicode approximation lookup — fallback when slug unknown
        if not matches and unicode_hint:
            stmt = (
                select(Symbol)
                .where(
                    Symbol.unicode_approx == unicode_hint,
                    Symbol.deleted_at.is_(None),
                )
                .order_by(Symbol.taxonomy_level)
                .limit(max_results)
            )
            rows = (await session.execute(stmt)).scalars().all()
            for row in rows:
                matches.append({
                    "id": str(row.id),
                    "slug": row.slug,
                    "name": row.name,
                    "category": row.category.value,
                    "score": 0.8,
                    "match_by": "unicode_approx",
                })

        if not matches:
            logger.debug(
                "No symbol registry match for detection slug=%r unicode=%r",
                slug_hint,
                unicode_hint,
            )

        output.append({"detection": detection, "matches": matches})

    return output
