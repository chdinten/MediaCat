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

## Top bar and search (3panel layout)

The top bar (`templates/base_3panel.html`) holds **Home**, the global search
box, the **Commands** button and the navigation links.

### Search scopes

A picker at the left of the search box chooses what `GET /search?q=&scope=`
(`routes.global_search`) looks in:

| Scope | Matches | Result list (left panel) |
|---|---|---|
| **Artists & albums** (`all`, default) | artist name; album title or catalogue number | *Artists* group, then *Albums* group |
| **Artists** (`artists`) | artist name | artists |
| **Albums** (`albums`) | album title or catalogue number | pressings, with artist, year, format, cat. no. |
| **Tracks** (`tracks`) | song title | tracks, with position (`B1`), artist, album, year |

- Song titles come from each pressing's `Token.tracklist` (JSONB), unnested with
  a lateral `jsonb_array_elements`. There is no track table; about 10,500 tracks
  are searched in roughly 10 ms.
- Case-insensitive substring match; `%` and `_` in the query match literally.
- Albums and tracks show at most 100 hits, artists 50.
- An artist hit loads the artist page into the centre panel. Album and track
  hits are links to the album's full detail page. The centre panel opens with
  the first artist (or the artist of the first album/track hit).
- Exactly one album or track hit and no artist: the route answers with
  `HX-Redirect` and the album page opens directly.
- The scope and the last 10 search terms are remembered per browser
  (`localStorage`); changing the scope re-runs the search.

### Command palette

**Commands** (or Ctrl+K) opens the command palette (`static/command_palette.js`):
pages, recent searches and slash commands such as `/track <title>` (`/t`,
`/song`), `/album <title>` (`/al`) and `/artist <name>` (`/a`).

## Artist page and biographies

`/artists/detail?artist=` shows the biography, the albums in the collection
and the discography. The biography partial (`/artists/info`) is resolved by
`ingestion/artist_bio.py`:

1. Wikipedia via the MusicBrainz artist link (trusted).
2. Otherwise a Wikipedia search, with title checks and an LLM verdict that the
   article is about this music artist.
3. Wikipedia with 800+ characters is used as is.
4. Otherwise the Discogs artist profile is compared; it wins only as a real
   biography (≥ 150 characters, ≥ 2 sentences) at least twice as long as the
   Wikipedia text. Discogs-only artists use Discogs.
5. Then the official homepage, summarised by the LLM from facts on the page
   only (marked "AI summary").
6. Otherwise a "No biography found" statement listing what was checked.

Bios are cut to whole paragraphs of about 1,200–2,500 characters and cached in
`artist_bios`. **Refresh bio** repeats the lookup. Lookups run as server-side
tasks: they finish and save even if the user leaves the page, and a second
request joins the running lookup. `static/bio_research.js` shows a
**Research in progress** banner with a timer while a lookup takes longer than
about a second.

## Pressing page additions

| Feature | Where | Notes |
|---|---|---|
| Pressing variants pick-and-copy | *Pressing variants*, *Manufacturing & stamping* | Pick a Discogs variant, tick matrix codes (or **All**), **Copy to My copy**; **Load variants** fetches identifiers for older imports. Box sets (> 2 LPs): full width, LP blocks side by side, pinned header. *My copy* has runout fields for LP 1–2 only |
| My copy (Discogs collection fields) | *My copy* card | Runout / Matrix LP1–2 A/B, Autographed, Signed by…; **Save in MediaCat** or **Save & write back to Discogs** |
| Album sheet | **Print sheet (PDF)** | A4 PDF: cover, headline, tracklist per LP, runout codes (`web/print_sheet.py`) |
| Image lightbox | Click any image | Pop-up over the page with close button, Esc, ← → stepping |
| Add pressing cards | `/catalogue/new` results → **Cards with images** | Discogs images, format details, barcode and first matrix variant per result, loaded per card as it scrolls into view (`/catalogue/new/discogs-preview/{id}`, cached 1 h) |
| Import banner | Top of the page | Shown while a Discogs collection import runs: Discogs functions may fail or wait (shared ~60 requests/min limit) |

Discogs identifiers and collection fields are stored in `Token.extra`
(`"discogs"`, `"discogs_collection"`), so no migration was needed. Routes:
`POST /catalogue/{id}/discogs-details/refresh`,
`POST /catalogue/{id}/discogs-collection`, `GET /catalogue/{id}/print.pdf`.

## JavaScript and the CSP

The CSP `script-src 'self'` blocks every inline script, including HTMX
`hx-on` attributes, so all handlers live in static files:

| File | Purpose |
|---|---|
| `static/forms.js` | CSRF header on native POSTs; list-row highlight via `data-mc-active="row-class"` (optional `data-mc-clear="#panel"`) |
| `static/search_history.js` | Search scope picker and search history |
| `static/command_palette.js` | Ctrl+K palette and slash commands |
| `static/bio_research.js` | "Research in progress" banner with timer |
| `static/pressing.js` | Pressing page: uploads, variant pick-and-copy, lightbox |
| `static/panel_resize.js` | 3panel layout switching and resizable panels |

## Full API reference

Run `make docs` to generate the full API reference with pdoc into
`docs/reference/`.
