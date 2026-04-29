"""structured matrix runout parsed fields

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-29

Schema changes
--------------
* tokens.matrix_runout_parsed   JSONB nullable
* tokens.matrix_runout_b_parsed JSONB nullable

Each column stores a dict of named runout components, each with value,
confidence, and source:

    {
      "matrix_number":  {"value": "10AA6305231", "confidence": 0.9,  "source": "vision"},
      "stamper_code":   {"value": "1Y",          "confidence": 0.85, "source": "vision"},
      "sid_mastering":  {"value": null,           "confidence": null, "source": null},
      "sid_mould":      {"value": "320",          "confidence": 0.6,  "source": "vision"},
      "lacquer_cutter": {"value": null,           "confidence": null, "source": null},
      "pressing_plant": {"value": null,           "confidence": null, "source": null},
      "other_etchings": {"value": null,           "confidence": null, "source": null}
    }

source is one of: "vision", "human", "import", "rule", null.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tokens", sa.Column("matrix_runout_parsed",   JSONB(), nullable=True))
    op.add_column("tokens", sa.Column("matrix_runout_b_parsed", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("tokens", "matrix_runout_b_parsed")
    op.drop_column("tokens", "matrix_runout_parsed")
