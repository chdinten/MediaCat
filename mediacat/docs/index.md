# MediaCat

A cataloging platform for physical music media (vinyl + CD, first generation).

This documentation is generated from the source tree and the Markdown files
under `docs/`. It has four layers:

| Layer              | Source                  | Audience                  |
|--------------------|-------------------------|---------------------------|
| Process docs       | `docs/*.md`             | Everyone                  |
| Architecture       | `docs/architecture.md`  | Architects, reviewers     |
| Decisions          | `docs/adr/*.md`         | Reviewers, future-you     |
| Runbooks           | `docs/runbooks/*.md`    | Operators                 |
| API reference      | `docs/reference/` (pdoc)| Developers                |

## Opening MediaCat

| What | Address |
|---|---|
| **MediaCat app** | `https://mediacat.localhost:8443/` |
| This documentation | `https://docs.mediacat.localhost:8443/` |
| Stack control panel (`make ctl-web`) | `http://127.0.0.1:8810/` |

Always include `:8443`. Caddy publishes HTTPS there, and on this machine port 443
belongs to Kasm Workspaces. Details: [Control panel and addresses](runbooks/control-panel.md).

## Quick links

- [Architecture overview](architecture.md)
- [Section 1 — bootstrap & scaffolding](sections/01-bootstrap.md)
- [Control panel and addresses](runbooks/control-panel.md)
- [Backup and restore](runbooks/backup-restore.md)
- [API reference](reference/mediacat.html)

## Regenerating

```bash
make docs           # build static site to ./site
make docs-serve     # live preview on 127.0.0.1:8800
```
