"""Object storage, OCR, and translation pipeline.

Submodules
----------
* ``object_store``  — MinIO wrapper with content-hash deduplication
* ``ocr``           — Tesseract primary, cloud OCR fallback
* ``translation``   — language detection + LLM-based translation to en-GB
"""

from __future__ import annotations
