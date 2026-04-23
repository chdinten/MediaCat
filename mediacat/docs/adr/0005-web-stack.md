# ADR-0005 — Web UI: FastAPI + Jinja2 + HTMX

- **Status**: Accepted
- **Date**: 2026-04-16

## Context

The review UI is an internal tool used by a small number of human
reviewers. It must be easy to secure (strict CSP, CSRF), simple to
deploy (same container as the API), and easy to audit.

## Decision

Server-rendered HTML with Jinja2 templates, progressively enhanced with
HTMX for partial updates. No client-side framework and no bundler are
required.

## Consequences

- Strict CSP is trivial: `script-src 'self'` covers HTMX and tiny
  helpers, no `unsafe-inline`, no CDN.
- CSRF is a per-form hidden token validated by middleware.
- No separate frontend build pipeline, no npm/yarn in the repo, no
  bundler CVE surface.
- Interactive review widgets (image-region markup, diff view) that
  benefit from richer interactivity can still ship as targeted
  `<script src="/static/…">` modules — same-origin, CSP-allowed.
- Dev velocity is high for the team profile (Python-first).

## Alternatives considered

- **FastAPI + React SPA** — larger attack surface (npm supply chain,
  inline scripts, CORS), heavier deployment, overkill for an internal
  tool. Rejected.
- **Django + templates** — would bundle an ORM, auth, and admin we do
  not need on top of FastAPI's existing async stack. Rejected.
