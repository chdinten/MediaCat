"""Image processing pipeline — upload → OCR → translate → persist.

This module orchestrates the three storage-layer concerns into a single
``process_image`` workflow.  It is called by ingestion workers and by
the review UI's manual-upload handler.

Flow
----
1. Upload image to MinIO via :mod:`object_store` (content-hash dedup).
2. Run OCR via :mod:`ocr` for the specified image region.
3. Detect language and translate to ``en-GB`` via :mod:`translation`.
4. Return a :class:`ProcessedImage` that the caller can persist to the
   database (media_object + ocr_artifact rows).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from mediacat.db.enums import ImageRegion
from mediacat.storage.object_store import ObjectStore, StoredObject
from mediacat.storage.ocr import OcrBackend, OcrResult
from mediacat.storage.translation import TranslationBackend, TranslationResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProcessedImage:
    """Complete result of the image processing pipeline."""

    stored: StoredObject
    """Where the image landed in MinIO."""

    ocr: OcrResult
    """Raw OCR output."""

    translation: TranslationResult
    """Translation result (may be a no-op for English text)."""

    region: ImageRegion | None
    """Which physical region of the media this image covers."""


class ImagePipeline:
    """Orchestrate upload → OCR → translate.

    Parameters
    ----------
    store
        Object-store client.
    ocr_backend
        OCR engine (Tesseract or cloud).
    translator
        Translation backend (passthrough or LLM).
    """

    def __init__(
        self,
        store: ObjectStore,
        ocr_backend: OcrBackend,
        translator: TranslationBackend,
    ) -> None:
        self._store = store
        self._ocr = ocr_backend
        self._translator = translator

    async def process_image(
        self,
        image_bytes: bytes,
        mime_type: str,
        *,
        region: ImageRegion | None = None,
        ocr_languages: list[str] | None = None,
        source_language: str | None = None,
        bucket: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> ProcessedImage:
        """Run the full pipeline on a single image.

        Parameters
        ----------
        image_bytes
            Raw image data.
        mime_type
            Image MIME type (validated by the object store).
        region
            Physical region of the media (label_a, obi_front, etc.).
        ocr_languages
            Tesseract language hints.
        source_language
            BCP-47 hint for translation.
        bucket
            Target MinIO bucket.
        metadata
            Extra S3 metadata.

        Returns
        -------
        ProcessedImage
            Combined result of all three stages.
        """
        # 1. Upload (dedup by content hash)
        stored = await self._store.put_image(
            image_bytes, mime_type, bucket=bucket, metadata=metadata
        )
        logger.info(
            "Stored image %s (%s, %d bytes)",
            stored.content_hash[:12],
            region.value if region else "unspecified",
            stored.size_bytes,
        )

        # 2. OCR
        ocr_result = await self._ocr.extract(image_bytes, languages=ocr_languages)
        logger.info(
            "OCR result: %d chars, confidence=%.2f, engine=%s",
            len(ocr_result.raw_text),
            ocr_result.confidence or 0.0,
            ocr_result.engine,
        )

        # 3. Translate to en-GB
        translation = await self._translator.translate(
            ocr_result.raw_text,
            source_language=source_language or ocr_result.detected_language,
        )
        if translation.was_translated:
            logger.info(
                "Translated from %s: %d → %d chars",
                translation.source_language,
                len(translation.source_text),
                len(translation.translated_text),
            )

        return ProcessedImage(
            stored=stored,
            ocr=ocr_result,
            translation=translation,
            region=region,
        )
