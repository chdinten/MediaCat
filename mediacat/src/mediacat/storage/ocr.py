"""OCR pipeline — extract text from images of labels, OBI, and runout.

Architecture
------------
1. **Primary**: Tesseract via subprocess (no native bindings — keeps the
   dependency surface small and upgradeable).
2. **Fallback**: pluggable cloud adapter (Azure, AWS Textract) — stub
   interface only; implementations land when enabled.

The caller receives an :class:`OcrResult` with raw text, detected
language, and a confidence score.  Translation to British English is
handled by the separate :mod:`mediacat.storage.translation` module.

Security
--------
* Image bytes are validated upstream by :mod:`object_store`.
* Tesseract is invoked with ``--psm 6`` (assume uniform block of text)
  and a restricted language list from config.  No shell expansion: args
  are passed as a list.
* Temporary files use ``tempfile`` in a private directory.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess  # nosec B404 — Tesseract invocation with fixed args, no shell
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OcrResult:
    """Output of a single OCR run."""

    raw_text: str
    detected_language: str | None = None
    confidence: float | None = None
    engine: str = "tesseract"
    metadata: dict[str, str] = field(default_factory=dict)


class OcrBackend(Protocol):
    """Interface that every OCR backend must implement."""

    async def extract(
        self,
        image_bytes: bytes,
        *,
        languages: list[str] | None = None,
        psm: int = 6,
    ) -> OcrResult: ...


# ---------------------------------------------------------------------------
# Tesseract backend
# ---------------------------------------------------------------------------


class TesseractBackend:
    """Invoke Tesseract via subprocess.

    Parameters
    ----------
    binary
        Path to the ``tesseract`` executable.  Resolved from ``$PATH``
        if not given.
    languages
        Default languages to pass with ``-l``.
    psm
        Page segmentation mode (default 6 = assume uniform block).
    """

    def __init__(
        self,
        *,
        binary: str | None = None,
        languages: list[str] | None = None,
        psm: int = 6,
    ) -> None:
        self.binary = binary or shutil.which("tesseract") or "tesseract"
        self.default_languages = languages or ["eng"]
        self.default_psm = psm

    async def extract(
        self,
        image_bytes: bytes,
        *,
        languages: list[str] | None = None,
        psm: int | None = None,
    ) -> OcrResult:
        """Run Tesseract on *image_bytes* and return structured result."""
        langs = languages or self.default_languages
        seg_mode = psm if psm is not None else self.default_psm

        with tempfile.TemporaryDirectory(prefix="mediacat_ocr_") as tmp:
            in_path = Path(tmp) / "input.png"
            out_base = Path(tmp) / "output"
            in_path.write_bytes(image_bytes)

            cmd: list[str] = [
                self.binary,
                str(in_path),
                str(out_base),
                "-l",
                "+".join(langs),
                "--psm",
                str(seg_mode),
                "--oem",
                "3",  # default OCR engine mode
                "tsv",  # output TSV for confidence parsing
            ]

            proc = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                timeout=60,
                check=False,
            )

            # Read results from tempdir (sync helper, called via to_thread above)
            result = await asyncio.to_thread(
                _read_tesseract_output, out_base, proc.returncode, proc.stderr
            )
            return result


def _read_tesseract_output(out_base: Path, returncode: int, stderr: bytes) -> OcrResult:
    """Read Tesseract output files synchronously (called from to_thread)."""
    if returncode != 0:
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        logger.error("Tesseract failed (rc=%d): %s", returncode, stderr_text)
        return OcrResult(
            raw_text="",
            engine="tesseract",
            confidence=0.0,
            metadata={"error": stderr_text},
        )

    # Read TSV output for per-word confidence
    tsv_path = Path(f"{out_base}.tsv")
    if tsv_path.exists():
        return _parse_tsv(tsv_path.read_text(encoding="utf-8"))

    # Fallback: read plain text
    txt_path = Path(f"{out_base}.txt")
    raw = txt_path.read_text(encoding="utf-8").strip() if txt_path.exists() else ""
    return OcrResult(raw_text=raw, engine="tesseract")


def _parse_tsv(tsv_content: str) -> OcrResult:
    """Parse Tesseract TSV output into an :class:`OcrResult`.

    The TSV has columns: level, page_num, block_num, par_num, line_num,
    word_num, left, top, width, height, conf, text.
    """
    lines = tsv_content.strip().split("\n")
    if len(lines) < 2:
        return OcrResult(raw_text="", engine="tesseract", confidence=0.0)

    words: list[str] = []
    confidences: list[float] = []

    for line in lines[1:]:  # skip header
        parts = line.split("\t")
        if len(parts) < 12:
            continue
        conf_str = parts[10].strip()
        text = parts[11].strip()
        if not text or conf_str == "-1":
            continue
        try:
            conf = float(conf_str)
        except ValueError:
            conf = 0.0
        words.append(text)
        confidences.append(conf)

    raw_text = " ".join(words)
    avg_conf = sum(confidences) / len(confidences) if confidences else 0.0

    return OcrResult(
        raw_text=raw_text,
        engine="tesseract",
        confidence=round(avg_conf / 100.0, 4),  # normalise to 0..1
    )


# ---------------------------------------------------------------------------
# Fallback cloud backend (stub)
# ---------------------------------------------------------------------------


class CloudOcrBackend:
    """Placeholder for Azure / AWS Textract / Google Vision OCR.

    Not implemented in first generation.  Instantiating raises
    ``NotImplementedError`` until a concrete adapter is added.
    """

    def __init__(self, provider: str = "azure", **_kwargs: object) -> None:
        self.provider = provider

    async def extract(
        self,
        image_bytes: bytes,
        *,
        languages: list[str] | None = None,
        psm: int = 6,
    ) -> OcrResult:
        raise NotImplementedError(
            f"Cloud OCR backend '{self.provider}' is not yet implemented. "
            "Enable it in config when an adapter is available."
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_ocr_backend(
    primary: str = "tesseract",
    *,
    languages: list[str] | None = None,
    psm: int = 6,
    fallback: str | None = None,  # noqa: ARG001 — reserved for future use
    **kwargs: object,
) -> OcrBackend:
    """Instantiate the configured OCR backend.

    Parameters
    ----------
    primary
        ``"tesseract"`` or a cloud provider name.
    languages
        Default languages.
    psm
        Tesseract page segmentation mode.
    fallback
        Optional fallback backend name (unused in first gen).
    """
    if primary == "tesseract":
        return TesseractBackend(languages=languages, psm=psm)
    return CloudOcrBackend(provider=primary, **kwargs)
