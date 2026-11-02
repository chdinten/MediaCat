# Browser logo sources

Original artwork (1254 × 1254 px RGB PNG, June 2026) for the MediaCat browser icon.
These files are design sources only: the app does not serve them.

| File | Motif |
|---|---|
| `browser-logo-01.png` | Turntable in a rounded square, warm bokeh background |
| `browser-logo-02.png` | Record under a desk lamp |
| `browser-logo-03.png` | Record as a coffee cup under a desk lamp |
| `browser-logo-04.png` | Record as a coffee cup, rounded square |
| `browser-logo-05.png` | Record as a coffee cup with a desk lamp |
| `browser-logo-06.png` | Record with a music note, rounded square on white |
| `browser-logo-07.png` | Record with a music note, rounded square, bokeh background — **in use** |
| `browser-logo-08.png` | Turntable, rounded square on white |

## In use

`src/mediacat/web/static/favicon.png` is `browser-logo-07.png` scaled to 256 × 256
(Pillow, Lanczos). `base.html` and `base_3panel.html` link it as
`<link rel="icon" type="image/png" href="/static/favicon.png">`.

To make the icon from another source, run this from `mediacat/`:

```bash
.venv/bin/python -c "from PIL import Image; Image.open('design/logos/browser-logo-07.png').resize((256, 256), Image.LANCZOS).save('src/mediacat/web/static/favicon.png', optimize=True)"
```

Static files are served without `Cache-Control`, and browsers cache favicons for a long
time. After changing the icon, press Ctrl+F5 or clear the site data to see the new one.

## Storage

The files are 1.2–2.2 MB each, so this folder is exempt from the 1 MB
`check-added-large-files` pre-commit hook (`mediacat/.pre-commit-config.yaml`).
All other folders keep the limit.
