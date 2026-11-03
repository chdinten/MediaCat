/* catalogue_browser.js — the 3panel catalogue browser (catalogue_browser.html).
 *
 * The URL and history handling is declarative (hx-push-url / hx-replace-url in
 * the templates); this file adds the keyboard and pointer behaviour around the
 * HTMX swaps.  Everything is delegated from document, because Back restores
 * htmx snapshots that replace the panels.  No inline handlers: the CSP blocks
 * them.
 */
(function () {
    'use strict';

    /* Same folding as catalogue_browser._fold: lower case, no accents. */
    function fold(text) {
        return (text || '').normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase();
    }

    /* ── Artist index: type to filter ───────────────────────── */
    function filterIndex(value) {
        var list = document.getElementById('mc-index-list');
        if (!list) return;
        var needle = fold(value.trim());
        var shown = 0;
        list.querySelectorAll('.mc-artist').forEach(function (li) {
            var hit = !needle || li.getAttribute('data-fold').indexOf(needle) !== -1;
            li.hidden = !hit;
            if (hit) shown += 1;
        });
        list.querySelectorAll('.mc-letter').forEach(function (header) {
            header.hidden = !firstArtistUnder(header);
        });
        var none = document.getElementById('mc-index-nomatch');
        if (none) none.hidden = shown > 0;
    }

    function firstArtistUnder(header) {
        var node = header.nextElementSibling;
        while (node && !node.classList.contains('mc-letter')) {
            if (node.classList.contains('mc-artist') && !node.hidden) return node;
            node = node.nextElementSibling;
        }
        return null;
    }

    function visibleArtistLinks() {
        return Array.prototype.filter.call(
            document.querySelectorAll('#mc-index-list .mc-artist-link'),
            function (a) { return !a.parentElement.hidden; }
        );
    }

    document.addEventListener('input', function (e) {
        if (e.target && e.target.id === 'mc-index-filter') filterIndex(e.target.value);
    });

    /* ── A–Z rail ───────────────────────────────────────────── */
    function jumpTo(anchor) {
        var list = document.getElementById('mc-index-list');
        var header = document.getElementById('mc-letter-' + anchor);
        if (!list || !header || header.hidden) return;
        /* The letter header is sticky, so measure the first artist under it. */
        var first = firstArtistUnder(header);
        list.scrollTop = (first ? first.offsetTop : header.offsetTop) - header.offsetHeight;
    }

    /* ── Clicks ─────────────────────────────────────────────── */
    document.addEventListener('click', function (e) {
        var t = e.target;
        if (!t || !t.closest) return;
        var letter = t.closest('.mc-rail [data-letter]');
        if (letter) {
            jumpTo(letter.getAttribute('data-letter'));
            return;
        }
        if (t.closest('.mc-preview-close')) {
            document.body.classList.remove('mc-preview-open');
            return;
        }
        /* A click anywhere on a pressing row previews it (the link does the
           request) and takes the keyboard focus, so ↑ ↓ and Enter work next. */
        var row = t.closest('.mc-version');
        if (row && !t.closest('a, input, label, button')) {
            var link = row.querySelector('.mc-version-link');
            if (link) {
                link.focus({ preventScroll: true });
                link.click();
            }
        } else if (t.closest('.mc-version-link')) {
            t.closest('.mc-version-link').focus({ preventScroll: true });
        }
    });

    /* Double-click on a pressing row opens it. */
    document.addEventListener('dblclick', function (e) {
        var row = e.target && e.target.closest ? e.target.closest('.mc-version') : null;
        if (!row || e.target.closest('input, label')) return;
        var link = row.querySelector('.mc-version-link');
        if (link) window.location.href = link.href;
    });

    /* The pressing link the keys act on: the focused one, else the previewed
       one when the focus is on the page itself or inside the pressings table. */
    function pressingLinkFor(target) {
        var link = target.closest('.mc-version-link');
        if (link) return link;
        if (target === document.body || target === document.documentElement ||
                (target.closest('#mc-versions') && !target.closest('a, input, button, select, textarea'))) {
            return document.querySelector('#mc-versions .mc-version--active .mc-version-link');
        }
        return null;
    }

    /* ── Keyboard ───────────────────────────────────────────── */
    function step(links, current, delta) {
        var i = links.indexOf(current) + delta;
        return i >= 0 && i < links.length ? links[i] : null;
    }

    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && document.body.classList.contains('mc-preview-open')) {
            document.body.classList.remove('mc-preview-open');
            return;
        }
        if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey || !e.target || !e.target.closest) return;
        var down = e.key === 'ArrowDown';
        var up = e.key === 'ArrowUp';

        /* Filter box: ↓ moves into the list. */
        if (e.target.id === 'mc-index-filter' && down) {
            var firstLink = visibleArtistLinks()[0];
            if (firstLink) { e.preventDefault(); firstLink.focus(); }
            return;
        }

        /* Artist index: ↑ ↓ move between artists, Enter opens one (a normal link click). */
        var artist = e.target.closest('.mc-artist-link');
        if (artist && (down || up)) {
            e.preventDefault();
            var nextArtist = step(visibleArtistLinks(), artist, down ? 1 : -1);
            if (nextArtist) nextArtist.focus();
            else if (up) document.getElementById('mc-index-filter').focus();
            return;
        }

        /* Pressings: ↑ ↓ preview the next one; Enter on the previewed one opens it. */
        var link = pressingLinkFor(e.target);
        if (!link) return;
        var row = link.closest('.mc-version');
        if (down || up) {
            e.preventDefault();
            var sibling = down ? row.nextElementSibling : row.previousElementSibling;
            var next = sibling && sibling.querySelector('.mc-version-link');
            if (next) {
                next.focus();
                next.click();
            }
        } else if (e.key === 'Enter' && row.classList.contains('mc-version--active')) {
            e.preventDefault();
            window.location.href = link.href;
        }
    });

    /* ── Preview: cover tint and the narrow-screen drawer ───── */
    function toHsl(r, g, b) {
        r /= 255; g /= 255; b /= 255;
        var max = Math.max(r, g, b), min = Math.min(r, g, b);
        var h = 0, s = 0, l = (max + min) / 2;
        if (max !== min) {
            var d = max - min;
            s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
            if (max === r) h = (g - b) / d + (g < b ? 6 : 0);
            else if (max === g) h = (b - r) / d + 2;
            else h = (r - g) / d + 4;
            h *= 60;
        }
        /* Dark and muted enough for the light text on it, in both themes. */
        return [Math.round(h), Math.round(Math.min(s, 0.5) * 100),
                Math.round(Math.min(Math.max(l, 0.16), 0.3) * 100)];
    }

    /* Average colour of the cover, weighted towards its coloured pixels.
       The image comes from this site, so the canvas is not tainted. */
    function coverColour(img) {
        try {
            var size = 24;
            var canvas = document.createElement('canvas');
            canvas.width = size;
            canvas.height = size;
            var ctx = canvas.getContext('2d', { willReadFrequently: true });
            ctx.drawImage(img, 0, 0, size, size);
            var px = ctx.getImageData(0, 0, size, size).data;
            var r = 0, g = 0, b = 0, total = 0;
            for (var i = 0; i < px.length; i += 4) {
                var max = Math.max(px[i], px[i + 1], px[i + 2]);
                var min = Math.min(px[i], px[i + 1], px[i + 2]);
                var light = (max + min) / 510;
                if (light < 0.06 || light > 0.94) continue;  /* skip borders and paper white */
                var weight = 1 + (max - min) / 64;
                r += px[i] * weight;
                g += px[i + 1] * weight;
                b += px[i + 2] * weight;
                total += weight;
            }
            return total ? toHsl(r / total, g / total, b / total) : null;
        } catch (err) {
            return null;
        }
    }

    function tintPreview(root) {
        var head = root && root.querySelector('.mc-preview-head');
        var img = head && head.querySelector('img.mc-preview-cover');
        if (!img) return;
        function apply() {
            var hsl = coverColour(img);
            if (hsl) head.style.setProperty('--mc-tint', 'hsl(' + hsl[0] + ' ' + hsl[1] + '% ' + hsl[2] + '%)');
        }
        if (img.complete && img.naturalWidth) apply();
        else img.addEventListener('load', apply, { once: true });
    }

    document.addEventListener('htmx:afterSwap', function (e) {
        var target = e.detail && e.detail.target;
        if (!target || target.id !== 'sp-right') return;
        /* A preview the user asked for opens the drawer (it only shows below 1100 px). */
        document.body.classList.toggle('mc-preview-open', !!target.querySelector('.mc-preview'));
        tintPreview(target);
    });

    document.addEventListener('htmx:oobAfterSwap', function (e) {
        var target = e.detail && e.detail.target;
        if (target && target.id === 'sp-right') document.body.classList.remove('mc-preview-open');
    });

    document.addEventListener('htmx:historyRestore', function () {
        document.body.classList.remove('mc-preview-open');
    });

    document.addEventListener('DOMContentLoaded', function () {
        tintPreview(document.getElementById('sp-right'));
    });
}());
