"""Seed countries table with ISO 3166-1 and Discogs-specific entries

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-02

Schema changes
--------------
* Inserts ~90 rows into the countries table (no schema change).
  Discogs country naming conventions used for US/UK.
  Non-standard Discogs values (Europe, Worldwide, etc.) use X-prefix synthetic codes.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

# (alpha2, alpha3, name, numeric_code|None)
# Discogs uses "US" and "UK" rather than ISO full names; both variants seeded.
_COUNTRIES = [
    ("AR", "ARG", "Argentina", "032"),
    ("AU", "AUS", "Australia", "036"),
    ("AT", "AUT", "Austria", "040"),
    ("BE", "BEL", "Belgium", "056"),
    ("BO", "BOL", "Bolivia", "068"),
    ("BR", "BRA", "Brazil", "076"),
    ("BG", "BGR", "Bulgaria", "100"),
    ("CA", "CAN", "Canada", "124"),
    ("CL", "CHL", "Chile", "152"),
    ("CN", "CHN", "China", "156"),
    ("CO", "COL", "Colombia", "170"),
    ("HR", "HRV", "Croatia", "191"),
    ("CZ", "CZE", "Czech Republic", "203"),
    ("DK", "DNK", "Denmark", "208"),
    ("EE", "EST", "Estonia", "233"),
    ("EG", "EGY", "Egypt", "818"),
    ("FI", "FIN", "Finland", "246"),
    ("FR", "FRA", "France", "250"),
    ("DE", "DEU", "Germany", "276"),
    ("GH", "GHA", "Ghana", "288"),
    ("GR", "GRC", "Greece", "300"),
    ("GT", "GTM", "Guatemala", "320"),
    ("HK", "HKG", "Hong Kong", "344"),
    ("HU", "HUN", "Hungary", "348"),
    ("IS", "ISL", "Iceland", "352"),
    ("IN", "IND", "India", "356"),
    ("ID", "IDN", "Indonesia", "360"),
    ("IE", "IRL", "Ireland", "372"),
    ("IL", "ISR", "Israel", "376"),
    ("IT", "ITA", "Italy", "380"),
    ("JM", "JAM", "Jamaica", "388"),
    ("JP", "JPN", "Japan", "392"),
    ("KZ", "KAZ", "Kazakhstan", "398"),
    ("KE", "KEN", "Kenya", "404"),
    ("KR", "KOR", "South Korea", "410"),
    ("LV", "LVA", "Latvia", "428"),
    ("LT", "LTU", "Lithuania", "440"),
    ("LU", "LUX", "Luxembourg", "442"),
    ("MY", "MYS", "Malaysia", "458"),
    ("MX", "MEX", "Mexico", "484"),
    ("MA", "MAR", "Morocco", "504"),
    ("NL", "NLD", "Netherlands", "528"),
    ("NZ", "NZL", "New Zealand", "554"),
    ("NG", "NGA", "Nigeria", "566"),
    ("NO", "NOR", "Norway", "578"),
    ("PK", "PAK", "Pakistan", "586"),
    ("PE", "PER", "Peru", "604"),
    ("PH", "PHL", "Philippines", "608"),
    ("PL", "POL", "Poland", "616"),
    ("PT", "PRT", "Portugal", "620"),
    ("PR", "PRI", "Puerto Rico", "630"),
    ("RO", "ROU", "Romania", "642"),
    ("RU", "RUS", "Russia", "643"),
    ("RS", "SRB", "Serbia", "688"),
    ("SG", "SGP", "Singapore", "702"),
    ("SK", "SVK", "Slovakia", "703"),
    ("SI", "SVN", "Slovenia", "705"),
    ("ZA", "ZAF", "South Africa", "710"),
    ("ES", "ESP", "Spain", "724"),
    ("LK", "LKA", "Sri Lanka", "144"),
    ("SE", "SWE", "Sweden", "752"),
    ("CH", "CHE", "Switzerland", "756"),
    ("TW", "TWN", "Taiwan", "158"),
    ("TH", "THA", "Thailand", "764"),
    ("TR", "TUR", "Turkey", "792"),
    ("UA", "UKR", "Ukraine", "804"),
    # Discogs uses "UK" not "United Kingdom"
    ("GB", "GBR", "UK", "826"),
    # Discogs uses "US" not "United States"
    ("US", "USA", "US", "840"),
    ("UY", "URY", "Uruguay", "858"),
    ("VE", "VEN", "Venezuela", "862"),
    ("VN", "VNM", "Vietnam", "704"),
    ("ZW", "ZWE", "Zimbabwe", "716"),
    # Deprecated codes still used on Discogs releases
    ("YU", "YUG", "Yugoslavia", None),
    ("SU", "SUN", "USSR", None),
    ("CS", "CSK", "Czechoslovakia", None),
    # X-series synthetic codes for non-country Discogs values
    ("XE", "XEU", "Europe", None),
    ("XW", "XWW", "Worldwide", None),
    ("XS", "XSC", "Scandinavia", None),
    ("XU", "XNK", "Unknown", None),
]


def upgrade() -> None:
    bind = op.get_bind()
    stmt = sa.text(
        "INSERT INTO countries "
        "(id, alpha2, alpha3, name, numeric_code, created_at, updated_at) "
        "VALUES (uuid_generate_v4(), :a2, :a3, :name, :nc, now(), now()) "
        "ON CONFLICT DO NOTHING"
    )
    for alpha2, alpha3, name, numeric_code in _COUNTRIES:
        bind.execute(stmt, {"a2": alpha2, "a3": alpha3, "name": name, "nc": numeric_code})


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM countries"))
