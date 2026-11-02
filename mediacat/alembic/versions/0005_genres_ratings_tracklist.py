"""genres, ratings, and tracklist on tokens

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-30

Schema changes
--------------
* tokens.genres                      JSONB nullable  — ["Rock", "Jazz", …]
* tokens.tracklist                   JSONB nullable  — [{disc, position, title, artists, duration_ms}, …]
* tokens.personal_rating             FLOAT nullable  — user's own 0-10 rating
* tokens.discogs_community_rating    FLOAT nullable  — Discogs avg community rating
* tokens.discogs_community_votes     INTEGER nullable — number of Discogs community votes
* tokens.musicbrainz_release_url     VARCHAR(200) nullable — direct link to MB release page
* tokens.wikipedia_artist_url        VARCHAR(500) nullable — Wikipedia article URL for the artist
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tokens", sa.Column("genres",                   JSONB(),          nullable=True))
    op.add_column("tokens", sa.Column("tracklist",                JSONB(),          nullable=True))
    op.add_column("tokens", sa.Column("personal_rating",          sa.Float(),       nullable=True))
    op.add_column("tokens", sa.Column("discogs_community_rating", sa.Float(),       nullable=True))
    op.add_column("tokens", sa.Column("discogs_community_votes",  sa.Integer(),     nullable=True))
    op.add_column("tokens", sa.Column("musicbrainz_release_url",  sa.String(200),   nullable=True))
    op.add_column("tokens", sa.Column("wikipedia_artist_url",     sa.String(500),   nullable=True))


def downgrade() -> None:
    op.drop_column("tokens", "wikipedia_artist_url")
    op.drop_column("tokens", "musicbrainz_release_url")
    op.drop_column("tokens", "discogs_community_votes")
    op.drop_column("tokens", "discogs_community_rating")
    op.drop_column("tokens", "personal_rating")
    op.drop_column("tokens", "tracklist")
    op.drop_column("tokens", "genres")
