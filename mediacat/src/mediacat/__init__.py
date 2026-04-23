"""MediaCat — cataloging platform for physical music media.

This top-level package is intentionally thin. Subpackages are added in
later sections:

* ``mediacat.config``     — typed configuration loader (Section 4+)
* ``mediacat.db``         — SQLAlchemy models and repositories (Section 3)
* ``mediacat.storage``    — object-store client and OCR pipeline (Section 4)
* ``mediacat.ingestion``  — connector framework (Section 5)
* ``mediacat.rules``      — rule-engine adapters (Section 6)
* ``mediacat.llm``        — LLM adapters (Section 7)
* ``mediacat.vision``     — vision adapters (Section 8)
* ``mediacat.web``        — FastAPI + Jinja + HTMX UI (Section 9)

The public API surface is stabilised at release time. Internal helpers
begin with an underscore.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__: str = "0.1.0"
