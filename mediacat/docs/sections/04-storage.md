# Section 4 — Object storage + OCR + translation pipeline

## What this section produces

- `src/mediacat/storage/object_store.py` — MinIO wrapper with SHA-256
  content-hash deduplication, MIME validation, Pillow image verification.
- `src/mediacat/storage/ocr.py` — Tesseract backend via subprocess,
  TSV confidence parsing, cloud OCR stub.
- `src/mediacat/storage/translation.py` — language detection heuristic,
  passthrough translator, LLM-backed translator with prompt injection
  awareness.
- `src/mediacat/storage/pipeline.py` — `ImagePipeline` orchestrator:
  upload → OCR → translate in a single `process_image` call.

## Security properties

- Image bytes are validated by Pillow before storage (decompression
  bomb limit enforced).
- Only MIME types in `ALLOWED_MIME_TYPES` are accepted.
- Object keys are content hashes — no user-controlled path components.
- Tesseract is invoked with a fixed argument list, no shell expansion.
- Translation inputs are length-limited before reaching the LLM.
