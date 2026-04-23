"""Web UI — FastAPI + Jinja2 + HTMX for human review and admin.

Submodules
----------
* ``app``        — FastAPI application factory
* ``auth``       — Argon2id password hashing, session, CSRF
* ``middleware`` — security headers, request-id, rate limiting
* ``routes``     — review queue, token browser, health endpoints
"""

from __future__ import annotations
