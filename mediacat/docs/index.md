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

## Quick links

- [Architecture overview](architecture.md)
- [Section 1 — bootstrap & scaffolding](sections/01-bootstrap.md)
- [API reference](reference/mediacat.html)

## Regenerating

```bash
make docs           # build static site to ./site
make docs-serve     # live preview on 127.0.0.1:8800
```
