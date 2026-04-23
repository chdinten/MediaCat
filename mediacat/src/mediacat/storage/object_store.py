"""MinIO-backed object store with content-hash deduplication.

Every uploaded file is keyed by ``SHA-256(raw_bytes)`` so the same image
uploaded twice (from Discogs and from a user scan, say) resolves to a
single stored object.  The caller receives a :class:`StoredObject`
dataclass that includes the hash, bucket, key, and basic image metadata.

Security
--------
* File content is validated: MIME type is checked against an allowlist.
* Images are opened with Pillow to verify they are valid images and to
  extract dimensions.  ``Pillow.Image.MAX_IMAGE_PIXELS`` is enforced to
  prevent decompression bombs.
* Object keys are content hashes — no user-controlled path components.

Thread / async safety
---------------------
The :class:`minio.Minio` client is synchronous.  This module wraps it in
``asyncio.to_thread`` calls so it can be used from async callers without
blocking the event loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
from dataclasses import dataclass

from minio import Minio
from minio.error import S3Error
from PIL import Image

logger = logging.getLogger(__name__)

# Hard limit on decompression (Pillow default is 178 megapixels).
Image.MAX_IMAGE_PIXELS = 178_956_970

# Only these MIME types are accepted.
ALLOWED_MIME_TYPES: frozenset[str] = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/tiff",
        "image/webp",
        "image/gif",
        "image/bmp",
    }
)


@dataclass(frozen=True, slots=True)
class StoredObject:
    """Result of a successful upload."""

    content_hash: str
    bucket: str
    object_key: str
    size_bytes: int
    mime_type: str
    width_px: int | None = None
    height_px: int | None = None


class ObjectStoreError(Exception):
    """Raised on storage-layer failures."""


class ObjectStore:
    """Async-friendly wrapper around :class:`minio.Minio`.

    Parameters
    ----------
    endpoint
        MinIO endpoint, e.g. ``minio:9000``.
    access_key, secret_key
        Credentials.
    secure
        Use TLS.  ``False`` for internal Docker networking.
    default_bucket
        Bucket used when none is specified.
    """

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        *,
        secure: bool = False,
        default_bucket: str = "media-originals",
    ) -> None:
        self._client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
        )
        self.default_bucket = default_bucket

    # ------------------------------------------------------------------
    # Bucket lifecycle
    # ------------------------------------------------------------------

    async def ensure_bucket(self, bucket: str | None = None) -> None:
        """Create the bucket if it does not exist."""
        bucket = bucket or self.default_bucket
        exists = await asyncio.to_thread(self._client.bucket_exists, bucket)
        if not exists:
            await asyncio.to_thread(self._client.make_bucket, bucket)
            logger.info("Created bucket %s", bucket)

    # ------------------------------------------------------------------
    # Upload with dedup
    # ------------------------------------------------------------------

    async def put_image(
        self,
        data: bytes,
        mime_type: str,
        *,
        bucket: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        """Upload an image, deduplicating by content hash.

        Parameters
        ----------
        data
            Raw image bytes.
        mime_type
            Must be in :data:`ALLOWED_MIME_TYPES`.
        bucket
            Target bucket; defaults to ``self.default_bucket``.
        metadata
            Optional user metadata attached to the S3 object.

        Returns
        -------
        StoredObject
            Includes the content hash, dimensions, and storage coordinates.

        Raises
        ------
        ObjectStoreError
            On invalid MIME type, corrupt image data, or S3 failure.
        """
        if mime_type not in ALLOWED_MIME_TYPES:
            msg = f"Rejected MIME type {mime_type!r}; allowed: {sorted(ALLOWED_MIME_TYPES)}"
            raise ObjectStoreError(msg)

        # Validate image and extract dimensions
        width: int | None = None
        height: int | None = None
        try:
            img = Image.open(io.BytesIO(data))
            img.verify()  # checks integrity without full decode
            # Re-open after verify (verify leaves file pointer in unknown state)
            img = Image.open(io.BytesIO(data))
            width, height = img.size
        except Exception as exc:
            msg = f"Image validation failed: {exc}"
            raise ObjectStoreError(msg) from exc

        content_hash = hashlib.sha256(data).hexdigest()
        bucket = bucket or self.default_bucket
        ext = _mime_to_ext(mime_type)
        object_key = f"{content_hash[:2]}/{content_hash[2:4]}/{content_hash}{ext}"

        # Check if object already exists (dedup)
        already_exists = False
        try:
            await asyncio.to_thread(self._client.stat_object, bucket, object_key)
            already_exists = True
            logger.debug("Dedup hit: %s/%s", bucket, object_key)
        except S3Error as e:
            if e.code != "NoSuchKey":
                raise ObjectStoreError(str(e)) from e

        if not already_exists:
            try:
                await asyncio.to_thread(
                    self._client.put_object,
                    bucket,
                    object_key,
                    io.BytesIO(data),
                    length=len(data),
                    content_type=mime_type,
                    metadata=metadata,  # type: ignore[arg-type]  # minio accepts broader union
                )
                logger.info("Stored %s/%s (%d bytes)", bucket, object_key, len(data))
            except S3Error as exc:
                raise ObjectStoreError(str(exc)) from exc

        return StoredObject(
            content_hash=content_hash,
            bucket=bucket,
            object_key=object_key,
            size_bytes=len(data),
            mime_type=mime_type,
            width_px=width,
            height_px=height,
        )

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    async def get_object(
        self,
        object_key: str,
        bucket: str | None = None,
        *,
        max_bytes: int = 200_000_000,
    ) -> bytes:
        """Download an object by key.

        Parameters
        ----------
        max_bytes
            Maximum bytes to read (default 200 MB).  Raises
            :class:`ObjectStoreError` if the object exceeds this limit.

        Raises
        ------
        ObjectStoreError
            If the object does not exist, exceeds *max_bytes*, or S3 fails.
        """
        bucket = bucket or self.default_bucket
        try:
            # Check size before downloading
            stat = await asyncio.to_thread(self._client.stat_object, bucket, object_key)
            if stat.size is not None and stat.size > max_bytes:
                msg = f"Object {object_key} size {stat.size} exceeds limit {max_bytes}"
                raise ObjectStoreError(msg)
            response = await asyncio.to_thread(self._client.get_object, bucket, object_key)
            try:
                data = response.read(max_bytes + 1)
                if len(data) > max_bytes:
                    msg = f"Object {object_key} read exceeded limit {max_bytes}"
                    raise ObjectStoreError(msg)
                return data
            finally:
                response.close()
                response.release_conn()
        except ObjectStoreError:
            raise
        except S3Error as exc:
            raise ObjectStoreError(str(exc)) from exc

    async def exists(
        self,
        object_key: str,
        bucket: str | None = None,
    ) -> bool:
        """Check whether an object exists."""
        bucket = bucket or self.default_bucket
        try:
            await asyncio.to_thread(self._client.stat_object, bucket, object_key)
            return True
        except S3Error as e:
            if e.code == "NoSuchKey":
                return False
            raise ObjectStoreError(str(e)) from e


def _mime_to_ext(mime_type: str) -> str:
    """Map MIME type to file extension."""
    mapping: dict[str, str] = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/tiff": ".tiff",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/bmp": ".bmp",
    }
    return mapping.get(mime_type, "")
