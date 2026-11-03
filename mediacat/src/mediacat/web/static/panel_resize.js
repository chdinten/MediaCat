'use strict';

/**
 * Resizable 3-column panels with per-page localStorage persistence.
 * Also handles layout-mode switching via [data-sp-layout] sentinel elements
 * placed in HTMX responses.
 */
(function () {
    var STORAGE_PREFIX = 'mc-panels-';
    var MIN_W = 160;
    var MAX_W = 560;

    // ── Per-page storage ───────────────────────────────────────
    function pageKey() {
        return STORAGE_PREFIX + window.location.pathname;
    }

    function loadWidths() {
        try { return JSON.parse(localStorage.getItem(pageKey()) || '{}'); }
        catch (_) { return {}; }
    }

    function saveWidths(widths) {
        try { localStorage.setItem(pageKey(), JSON.stringify(widths)); }
        catch (_) {}
    }

    function applyWidths(layout, widths) {
        if (widths.left)  layout.style.setProperty('--left-w',  widths.left  + 'px');
        if (widths.right) layout.style.setProperty('--right-w', widths.right + 'px');
    }

    // ── Resize handle wiring ───────────────────────────────────
    function wireHandle(handleId, side) {
        var handle = document.getElementById(handleId);
        if (!handle) return;

        var startX, startW;

        function onMove(e) {
            var delta = e.clientX - startX;
            var newW = (side === 'left') ? startW + delta : startW - delta;
            newW = Math.max(MIN_W, Math.min(MAX_W, newW));
            var layout = document.getElementById('sp-layout-root');
            if (layout) layout.style.setProperty('--' + side + '-w', newW + 'px');
        }

        function onUp() {
            document.removeEventListener('mousemove', onMove);
            document.removeEventListener('mouseup',  onUp);
            document.body.style.cursor = '';
            document.body.style.userSelect = '';

            var panel = document.getElementById('sp-' + side);
            if (panel) {
                var widths = loadWidths();
                widths[side] = panel.offsetWidth;
                saveWidths(widths);
            }
        }

        handle.addEventListener('mousedown', function (e) {
            e.preventDefault();
            var panel = document.getElementById('sp-' + side);
            startX = e.clientX;
            startW = panel ? panel.offsetWidth : (side === 'left' ? 260 : 320);
            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup',  onUp);
            document.body.style.cursor = 'col-resize';
            document.body.style.userSelect = 'none';
        });
    }

    // ── Layout mode switching ──────────────────────────────────
    function applyLayoutMode(mode) {
        var layout = document.getElementById('sp-layout-root');
        if (!layout) return;
        layout.className = 'sp-layout sp-layout--' + mode;
        applyWidths(layout, loadWidths());
    }

    function checkSentinel() {
        var el = document.querySelector('[data-sp-layout]');
        if (!el) return;
        var mode = el.dataset.spLayout;
        el.removeAttribute('data-sp-layout');
        applyLayoutMode(mode);
    }

    // ── Init ───────────────────────────────────────────────────
    function init() {
        var layout = document.getElementById('sp-layout-root');
        if (!layout) return;
        applyWidths(layout, loadWidths());
        wireHandle('sp-resize-left',  'left');
        wireHandle('sp-resize-right', 'right');
        checkSentinel();
    }

    document.addEventListener('DOMContentLoaded', init);
    // Back can restore an htmx history snapshot of the panels, which replaces
    // the resize handles: wire the new ones.
    document.addEventListener('htmx:historyRestore', init);

    // Re-check sentinel after every HTMX settle (panel content swap, navigation)
    document.addEventListener('htmx:afterSettle', function () {
        checkSentinel();
    });

    // Expose for console debugging
    window._mcPanels = {
        load:   loadWidths,
        save:   saveWidths,
        reset:  function () { localStorage.removeItem(pageKey()); init(); },
        mode:   applyLayoutMode,
    };
}());
