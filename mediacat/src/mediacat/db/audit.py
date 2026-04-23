"""Audit log writer — append-only action logging.

Every significant action (login, review approve/reject, token update,
user change) calls :func:`write_audit` to record a tamper-evident trail.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from mediacat.db.models import AuditLog
from mediacat.logging_filters import request_id_var

logger = logging.getLogger(__name__)


async def write_audit(
    session: AsyncSession,
    *,
    action: str,
    entity_type: str,
    entity_id: str,
    user_id: str | None = None,
    detail: dict[str, Any] | None = None,
    ip_address: str | None = None,
) -> None:
    """Write an entry to the audit log.

    Parameters
    ----------
    session
        Active DB session (caller manages commit).
    action
        Action name, e.g. ``"login"``, ``"review.approve"``, ``"token.update"``.
    entity_type
        Type of entity acted upon, e.g. ``"user"``, ``"token"``, ``"review_item"``.
    entity_id
        ID of the entity.
    user_id
        ID of the acting user (None for system actions).
    detail
        Optional JSON-serialisable detail dict.
    ip_address
        Client IP address.
    """
    import uuid

    entry = AuditLog(
        user_id=uuid.UUID(user_id) if user_id else None,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        detail=detail,
        ip_address=ip_address,
        request_id=request_id_var.get(),
    )
    session.add(entry)
    logger.info(
        "Audit: action=%s entity=%s:%s user=%s",
        action,
        entity_type,
        entity_id,
        user_id or "system",
    )
