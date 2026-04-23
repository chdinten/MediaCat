# Section 10 — Security hardening & critical review

## Threat model

### Assets
1. **Token database** — intellectual property of the cataloguer.
2. **User credentials** — hashed with Argon2id, never stored in cleartext.
3. **API keys** — Discogs, MusicBrainz, Anthropic, stored as Docker secrets.
4. **Media objects** — images stored in MinIO, keyed by content hash.
5. **OCR/LLM outputs** — may contain sensitive provenance data.

### Threat actors
| Actor | Motivation | Capability |
|-------|-----------|------------|
| External attacker | Data theft, defacement | Network access to exposed ports |
| Malicious upstream | Poisoned API responses | Control of Discogs/MB response payloads |
| Insider (reviewer) | Privilege escalation | Authenticated access to the review UI |
| LLM prompt injection | Data exfiltration via model output | Crafted text in OCR/ingestion data |

### Attack surfaces and mitigations

#### Network
| Surface | Mitigation | Status |
|---------|-----------|--------|
| Exposed ports | Only Caddy (80/443) is publicly bound; all other services are on `internal: true` backend network | ✅ Implemented (Section 2) |
| TLS termination | Caddy with automatic ACME; HSTS 2-year preload | ✅ Implemented |
| Port scanning / brute force | Caddy rate limiting; app-level login rate limiter | ✅ Implemented |

#### Authentication & session
| Surface | Mitigation | Status |
|---------|-----------|--------|
| Password storage | Argon2id (time=3, mem=64MiB, parallel=2) | ✅ Implemented (Section 9) |
| Brute-force login | `LoginRateLimiter`: 10 attempts / 15-min window, account lockout | ✅ Implemented |
| Session hijacking | Signed cookies via `itsdangerous.TimestampSigner`; `Secure`, `HttpOnly`, `SameSite=Lax` | ✅ Implemented |
| Session fixation | New session ID generated on every login | ✅ By design |
| MFA | TOTP-ready (`mfa_secret` column in users table); enforcement for admin/reviewer roles | 🔜 Deferred to v1.1 |

#### Injection
| Surface | Mitigation | Status |
|---------|-----------|--------|
| SQL injection | SQLAlchemy ORM with parameterised queries only; no raw SQL in application code | ✅ Implemented |
| XSS | Jinja2 autoescaping (on by default); strict CSP `script-src 'self'` | ✅ Implemented |
| CSRF | Per-session HMAC-SHA256 tokens validated on every mutating form; `SameSite=Lax` | ✅ Implemented |
| Prompt injection | `mediacat.llm.safety.sanitise()`: length limiting, XML-tag wrapping, injection pattern detection | ✅ Implemented (Section 7) |
| Path traversal | Object-store keys are content hashes; no user-controlled path components | ✅ By design |
| Command injection | Tesseract invoked via `subprocess.run` with list args (no shell); binary path validated | ✅ Implemented |

#### Data plane
| Surface | Mitigation | Status |
|---------|-----------|--------|
| DB privilege escalation | Three roles: `migrator` (DDL), `app` (DML), `readonly` (SELECT). App never runs DDL | ✅ Implemented (Section 2/3) |
| MinIO access | Credentials from Docker secrets; buckets not publicly accessible | ✅ Implemented |
| Redis data leakage | Password-authenticated; dangerous commands (`FLUSHDB`, `FLUSHALL`, `DEBUG`) renamed | ✅ Implemented (Section 2) |
| Backup exposure | Backup dir at `0750`; secrets dir at `0700 root:root` | ✅ Implemented |

#### Container
| Surface | Mitigation | Status |
|---------|-----------|--------|
| Root execution | All app containers run as UID 10001 (`mediacat` user); `cap_drop: [ALL]` | ✅ Implemented |
| Writable rootfs | `read_only: true` on app, worker, Redis, OPA, Caddy; tmpfs for `/tmp` | ✅ Implemented |
| Image supply chain | Multi-stage build; pinned base images; `.dockerignore` blocks secrets | ✅ Implemented |
| Outdated dependencies | `pip-audit` in CI; Trivy scan config below | ✅ Config ready |

## OWASP ASVS v4.0 checklist (key controls)

| ASVS # | Control | Status |
|--------|---------|--------|
| V2.1 | Password minimum 8 chars, Argon2id | ✅ |
| V2.4 | Credential recovery not implemented (admin-reset only) | ✅ By design |
| V2.8 | MFA ready (column exists, enforcement deferred) | 🔜 |
| V3.1 | Session tokens are cryptographically signed | ✅ |
| V3.3 | Session timeout configurable (default 24h) | ✅ |
| V3.5 | Cookie: Secure + HttpOnly + SameSite | ✅ |
| V4.1 | Access control on every route (middleware) | ✅ |
| V5.1 | Input validation via Pydantic models | ✅ |
| V5.3 | Output encoding via Jinja2 autoescaping | ✅ |
| V8.1 | Logging with request-id correlation | ✅ |
| V8.2 | Secret redaction in logs | ✅ |
| V8.3 | No sensitive data in error responses | ✅ |
| V9.1 | TLS on all external connections | ✅ |
| V10.1 | Dependency scanning (pip-audit, Trivy) | ✅ Config |
| V13.1 | API input size limits (16 MiB request, 100 MiB upload) | ✅ |
| V14.1 | Security headers (CSP, HSTS, X-Frame-Options, etc.) | ✅ |

## Trivy container scan configuration

```yaml
# .trivy.yaml — place in repo root
severity: [CRITICAL, HIGH]
exit-code: 1
ignore-unfixed: true
scanners: [vuln, secret, misconfig]
```

Usage: `trivy image mediacat:dev --config .trivy.yaml`

## Static analysis tools in CI

| Tool | Purpose | Trigger |
|------|---------|---------|
| `ruff` | Linting + Bandit-lite (`S` rules) | Pre-commit + CI |
| `mypy --strict` | Type safety | Pre-commit + CI |
| `bandit` | Security-focused SAST | Pre-commit + CI |
| `pip-audit` | Dependency vulnerability scan | CI |
| `gitleaks` | Secret detection in git history | Pre-commit |
| `hadolint` | Dockerfile best practices | Pre-commit |
| `shellcheck` | Shell script linting | Pre-commit |
| `trivy` | Container image scan | CI (post-build) |
| `opa test` | Policy correctness | CI |

## Residual risks and proposed mitigations

### 1. No rate limiting on API endpoints (beyond login)
**Risk**: Authenticated users could abuse search or ingestion endpoints.
**Proposed**: Add Redis-backed sliding-window rate limiter middleware keyed
by user ID. Priority: medium.

### 2. MFA not enforced yet
**Risk**: Compromised password = full account access.
**Proposed**: Implement TOTP validation in the login flow; enforce for
admin and reviewer roles. Priority: high.

### 3. No encryption at rest for MinIO
**Risk**: If the host disk is compromised, media objects are readable.
**Proposed**: Enable MinIO server-side encryption (SSE-S3) with a
KMS-managed key. Priority: medium.

### 4. LLM prompt injection is mitigated but not eliminated
**Risk**: Sophisticated injection could influence model output.
**Proposed**: (a) Structured output validation against JSON schema,
(b) secondary model call to verify critical decisions,
(c) all LLM proposals go through human review (already enforced).
Priority: low (human-in-the-loop is the primary control).

### 5. No audit log tamper protection
**Risk**: An attacker with DB write access could delete audit entries.
**Proposed**: (a) `mediacat_app` role has INSERT-only on `audit_log`
(add REVOKE UPDATE, DELETE), (b) forward audit logs to an external
append-only store (e.g. S3 with object lock). Priority: medium.

### 6. Session tokens are not rotated
**Risk**: Long-lived session after privilege change.
**Proposed**: Rotate session ID on role change and on every N minutes.
Priority: low.

### 7. No WAF in front of Caddy
**Risk**: Application-layer attacks (slow HTTP, request smuggling).
**Proposed**: Add ModSecurity or Coraza as a Caddy plugin, or deploy
Cloudflare/similar in front. Priority: low for single-host deployments.

## Code review findings from Sections 1–9

1. **`db/engine.py`** has 0% test coverage — all paths require a live
   database. Add integration test with `testcontainers-python` in CI.
2. **`ingestion/registry.py`** has 0% coverage — the secret-loading
   path needs a test with a temp secret file.
3. **`storage/pipeline.py`** has 0% coverage — orchestrator needs an
   integration test with mock store + OCR + translator.
4. **`vision/candidates.py`** has 28% coverage — the DB query functions
   need integration tests with a seeded test database.
5. **Middleware `response: Response = await call_next(request)`** — the
   Starlette `BaseHTTPMiddleware` has known issues with streaming and
   request body consumption. Consider migrating to pure ASGI middleware
   in v1.1.
6. **Redis job queue** uses `BLMOVE` without a reaper for stale jobs in
   the processing queue. Implement a periodic reaper task.
