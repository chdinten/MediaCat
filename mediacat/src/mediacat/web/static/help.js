/* Help page (/help): filter the questions, open the answer named in the URL.
 *
 * Search matches every typed word against the question, the answer and the
 * entry's hidden keywords (accents and case ignored).  ?q= pre-fills the box
 * (the command palette's "/help <words>"); #<id> opens and scrolls to that answer.
 */
(function () {
    'use strict';

    function fold(s) {
        return (s || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
    }

    function init() {
        var input = document.getElementById('help-search');
        if (!input) return;
        var entries = Array.prototype.slice.call(document.querySelectorAll('.help-entry'));
        var sections = document.querySelectorAll('.help-section:not(.help-dev)');
        var count = document.getElementById('help-count');
        var none = document.getElementById('help-none');
        var haystack = entries.map(function (el) {
            return fold(el.textContent + ' ' + (el.dataset.keywords || ''));
        });

        function filter() {
            var words = fold(input.value).split(/\s+/).filter(Boolean);
            var shown = 0;
            entries.forEach(function (el, i) {
                var hit = words.every(function (w) { return haystack[i].indexOf(w) !== -1; });
                el.hidden = !hit;
                if (words.length) el.open = hit;
                if (hit) shown++;
            });
            sections.forEach(function (sec) {
                sec.hidden = !sec.querySelector('.help-entry:not([hidden])');
            });
            none.hidden = shown > 0;
            count.textContent = words.length
                ? shown + (shown === 1 ? ' answer' : ' answers') + ' found'
                : '';
        }

        function openFromHash() {
            var id = decodeURIComponent(location.hash.slice(1));
            var el = id && document.getElementById(id);
            if (!el || !el.classList.contains('help-entry')) return;
            if (el.hidden) { input.value = ''; filter(); }
            el.open = true;
            el.scrollIntoView({ block: 'start' });
        }

        input.addEventListener('input', function () {
            if (!input.value) entries.forEach(function (el) { el.open = false; });
            filter();
        });
        document.querySelector('[data-help-expand]').addEventListener('click', function () {
            entries.forEach(function (el) { if (!el.hidden) el.open = true; });
        });
        document.querySelector('[data-help-collapse]').addEventListener('click', function () {
            entries.forEach(function (el) { el.open = false; });
        });
        window.addEventListener('hashchange', openFromHash);

        filter();
        openFromHash();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
