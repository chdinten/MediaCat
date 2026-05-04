# Section 9 — Web UI

## Overview

The web layer is a server-rendered FastAPI application using Jinja2 templates,
HTMX for partial-page updates, and a single CSS file
(`src/mediacat/web/static/style.css`) with no build step.

## Dashboard (`routes.py → GET /`)

The dashboard is the first page a user sees after login. It is composed of
three visual zones rendered by `templates/dashboard.html`:

### 1 · Hero

A full-bleed photograph (`static/hero-turntable1.jpg`) occupies the upper
portion of the viewport. The hero uses CSS `position: absolute; inset: 0`
layers to composite:

- `hero-bg-img` — the photographic background (object-fit: cover)
- `hero-lamp-wash` — radial amber gradient (animated flicker keyframe)
- `hero-fade` — bottom-to-top dark fade that anchors the UI text
- `hero-vignette` — left-edge vignette to create depth behind the headline
- `hero-grain` — SVG-based film grain overlay (opacity 0.08)

The `hero-content` flex column is positioned at the top of the hero area
(not the bottom), so the headline, tagline, and action buttons are immediately
visible below the navigation bar without scrolling.

### 2 · Stats strip (overlaid on hero bottom)

A `.hero-stats-row` div anchors to the bottom of the hero section via the
parent's `display: flex; flex-direction: column; justify-content: space-between`
layout. It uses a transparent-to-dark gradient to make the stat cards readable
over the image.

The stats grid (`minmax(88px, 1fr)`) renders up to 10 compact cards:

| Card | Source |
|---|---|
| Pressings | `COUNT(tokens)` |
| Artists | `COUNT(DISTINCT artist)` |
| Vinyl | `COUNT` filtered by `media_format = 'vinyl'` |
| CD | `COUNT` filtered by `media_format = 'cd'` |
| Oldest | `MIN(year)` across all active tokens (hidden if no year data) |
| Genre 1–5 | Count of tokens per genre, top 5 by volume |

Genre counts reuse the `_by_genre` dict built for the genre carousels — no
extra query.

### 3 · Content carousels (below the hero)

Below the hero, the dashboard renders up to three carousel rows:

| Row | Content | Sort |
|---|---|---|
| Recently added | 8 most recently created tokens | `created_at DESC` |
| Top rated | 8 highest-rated tokens (personal rating) | `personal_rating DESC` |
| Genre carousels | Up to 12 genre rows, each showing up to 8 tokens | genre count DESC, then rating DESC |

Each carousel card shows the sleeve image with a vinyl disc peeking behind it
on hover (CSS transform).

## Template context variables

| Variable | Type | Description |
|---|---|---|
| `stats` | `dict` | `total`, `vinyl`, `cd`, `artists`, `oldest_year` |
| `genre_stats` | `list[dict]` | `[{"genre": str, "count": int}, …]` — top 5 |
| `recent` | `list[Token]` | 8 most-recently added tokens (with `label` and `media_objects` loaded) |
| `top_rated` | `list[Token]` | 8 highest-rated tokens |
| `genre_carousels` | `list[dict]` | `[{"genre": str, "tokens": list[Token]}, …]` — up to 12 |

## Theme system

The UI uses the *Late Night Hi-Fi — Espresso Lounge* palette defined as CSS
custom properties in `style.css`. Dark mode is the default; `[data-theme="light"]`
overrides are provided for every affected selector. The theme toggle is
handled by `static/theme.js` which runs synchronously before first paint to
prevent flash of wrong theme.

## Static files relevant to the dashboard

| File | Purpose |
|---|---|
| `static/hero-turntable1.jpg` | Hero background photograph |
| `static/style.css` | All styles — design tokens, hero layout, stat cards, carousels |
| `static/theme.js` | Theme persistence via `localStorage['mc-theme']` |

## Full API reference

Run `make docs` to generate the full API reference with pdoc into
`docs/reference/`.
