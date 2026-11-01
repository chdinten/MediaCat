'use strict';

/**
 * Global search bar with localStorage history.
 * Clicking a history entry fills the input and runs the search, exactly as
 * pressing Enter would (same as picking a recent search in the Ctrl+K palette).
 */
(function () {
    var STORAGE_KEY = 'mc-search-history';
    var MAX_ENTRIES = 10;

    // ── Storage ────────────────────────────────────────────────
    function loadHistory() {
        try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]'); }
        catch (_) { return []; }
    }

    function saveHistory(entries) {
        try { localStorage.setItem(STORAGE_KEY, JSON.stringify(entries)); }
        catch (_) { /* quota exceeded — ignore */ }
    }

    function addEntry(term) {
        if (!term || !term.trim()) return;
        term = term.trim();
        var entries = loadHistory().filter(function (e) { return e.term !== term; });
        entries.unshift({ term: term, ts: Date.now() });
        if (entries.length > MAX_ENTRIES) entries.length = MAX_ENTRIES;
        saveHistory(entries);
    }

    function clearHistory() {
        saveHistory([]);
    }

    // ── HTML escaping ──────────────────────────────────────────
    function esc(s) {
        return String(s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    // ── Dropdown rendering ─────────────────────────────────────
    function renderDropdown(filter) {
        var drop = document.getElementById('sp-search-history-drop');
        if (!drop) return;

        var entries = loadHistory().filter(function (e) {
            return !filter || e.term.toLowerCase().indexOf(filter.toLowerCase()) !== -1;
        });

        if (!entries.length) { drop.classList.remove('open'); return; }

        drop.innerHTML =
            '<div class="sp-history-header">' +
                '<span>Recent searches</span>' +
                '<button type="button" class="sp-history-clear-btn" id="sp-hist-clear">Clear all</button>' +
            '</div>' +
            entries.map(function (e) {
                return '<div class="sp-history-item" data-term="' + esc(e.term) + '">' +
                    '<span class="sp-history-item-icon">🕐</span>' +
                    '<span class="sp-history-item-term">' + esc(e.term) + '</span>' +
                    '<span class="sp-history-item-recall">click to search</span>' +
                '</div>';
            }).join('');

        drop.classList.add('open');

        // Recall: populate input and run the search
        drop.querySelectorAll('.sp-history-item').forEach(function (item) {
            item.addEventListener('mousedown', function (ev) {
                ev.preventDefault(); // prevent blur on input
                var inp = document.getElementById('sp-search-input');
                drop.classList.remove('open');
                if (!inp) return;
                inp.value = item.dataset.term;
                toggleClearBtn(inp.value.length > 0);
                submitSearch(inp);
            });
        });

        var clrBtn = drop.querySelector('#sp-hist-clear');
        if (clrBtn) {
            clrBtn.addEventListener('mousedown', function (ev) {
                ev.preventDefault();
                clearHistory();
                drop.classList.remove('open');
            });
        }
    }

    // Submit like Enter does: fires the submit event (history save + HTMX
    // hx-trigger="submit"); falls back to a normal GET to /search?q=…
    function submitSearch(inp) {
        var form = document.getElementById('sp-search-form');
        if (form && typeof form.requestSubmit === 'function') {
            form.requestSubmit();
        } else if (form && window.htmx) {
            addEntry(inp.value);
            window.htmx.trigger(form, 'submit');
        } else {
            window.location.href = '/search?q=' + encodeURIComponent(inp.value);
        }
        inp.blur();
    }

    function toggleClearBtn(visible) {
        var btn = document.getElementById('sp-search-clear-btn');
        if (btn) btn.classList.toggle('visible', visible);
    }

    // ── DOM wiring ─────────────────────────────────────────────
    document.addEventListener('DOMContentLoaded', function () {
        var inp  = document.getElementById('sp-search-input');
        var drop = document.getElementById('sp-search-history-drop');
        var form = document.getElementById('sp-search-form');
        var clrBtn = document.getElementById('sp-search-clear-btn');

        if (!inp) return; // not on 3panel layout

        inp.addEventListener('focus', function () {
            renderDropdown(inp.value);
        });

        inp.addEventListener('input', function () {
            renderDropdown(inp.value);
            toggleClearBtn(inp.value.length > 0);
        });

        // Close dropdown on outside click
        document.addEventListener('click', function (ev) {
            if (drop && !drop.contains(ev.target) && ev.target !== inp) {
                drop.classList.remove('open');
            }
        });

        inp.addEventListener('keydown', function (ev) {
            if (ev.key === 'Escape') {
                drop && drop.classList.remove('open');
                inp.blur();
            }
        });

        // Add to history on form submit
        if (form) {
            form.addEventListener('submit', function () {
                if (inp.value.trim()) addEntry(inp.value.trim());
            });
            // HTMX fires before-request after the submit event, so history is saved first
        }

        // Clear button — wipes the input field only, not history
        if (clrBtn) {
            clrBtn.addEventListener('click', function () {
                inp.value = '';
                toggleClearBtn(false);
                drop && drop.classList.remove('open');
                inp.focus();
            });
        }

    });

    // Expose for console debugging / integration testing
    window._mcSearch = {
        history: loadHistory,
        add:     addEntry,
        clear:   clearHistory,
    };
}());
