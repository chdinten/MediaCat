/* panel_state.js — keep each panel's scroll position per URL (3panel layout).
 *
 * The panels scroll inside a fixed-height shell, so the browser's own scroll
 * restoration, which only tracks the window, cannot bring a long list back to
 * where it was.  Positions are saved per URL in sessionStorage when the page is
 * left or htmx snapshots it for history, and put back when the page or the
 * snapshot comes back.  Without a saved position the selected row
 * (aria-current="true") is scrolled into view instead.
 */
(function () {
    'use strict';

    /* Scrolling elements: the catalogue browser's own areas, then the panels. */
    var PANES = ['mc-index-list', 'mc-artist-top', 'mc-versions', 'mc-start',
                 'sp-left', 'sp-center', 'sp-right'];

    function key() {
        return 'mc-scroll:' + window.location.pathname + window.location.search;
    }

    function save() {
        var pos = {};
        PANES.forEach(function (id) {
            var el = document.getElementById(id);
            if (el) pos[id] = el.scrollTop;
        });
        try { sessionStorage.setItem(key(), JSON.stringify(pos)); } catch (e) { /* storage blocked */ }
    }

    function restore() {
        var pos = null;
        try { pos = JSON.parse(sessionStorage.getItem(key())); } catch (e) { /* storage blocked */ }
        PANES.forEach(function (id) {
            var el = document.getElementById(id);
            if (!el) return;
            if (pos && Object.prototype.hasOwnProperty.call(pos, id)) {
                el.scrollTop = pos[id];
                return;
            }
            var selected = el.querySelector('[aria-current="true"]');
            if (selected) selected.scrollIntoView({ block: id === 'mc-index-list' ? 'center' : 'nearest' });
        });
    }

    window.addEventListener('pagehide', save);
    window.addEventListener('pageshow', restore);
    document.addEventListener('htmx:beforeHistorySave', save);
    document.addEventListener('htmx:historyRestore', restore);
}());
