/* bio_research.js — "Research in progress" banner with a running timer while
 * an artist biography is looked up (MusicBrainz, Wikipedia, Discogs and, for
 * web pages, the local AI).  The lookup runs on the server and is saved even
 * if the user leaves, so the banner says so.  Cached bios answer at once; the
 * banner only appears once a request has taken longer than SHOW_AFTER_MS.
 */
(function () {
    'use strict';

    var SHOW_AFTER_MS = 1200;

    function fmt(seconds) {
        var m = Math.floor(seconds / 60);
        var s = seconds % 60;
        return m + ':' + (s < 10 ? '0' : '') + s;
    }

    function stop(elt) {
        var st = elt && elt._bioResearch;
        if (!st) return;
        clearInterval(st.interval);
        clearTimeout(st.showTimer);
        st.banner.remove();
        elt._bioResearch = null;
    }

    document.addEventListener('htmx:beforeRequest', function (evt) {
        var elt = evt.detail && evt.detail.elt;
        if (!elt || !elt.closest || !elt.closest('.js-bio-refresh, .js-bio-load')) return;
        var host = elt.closest('.artist-detail-bio') || elt.parentNode;
        if (!host) return;
        stop(elt);
        host.querySelectorAll('.bio-research').forEach(function (b) { b.remove(); });

        var banner = document.createElement('div');
        banner.className = 'bio-research';
        banner.setAttribute('role', 'status');
        banner.hidden = true;
        banner.innerHTML =
            '<span class="bio-research-spin" aria-hidden="true"></span>' +
            '<div><strong>Research in progress</strong> ' +
            '<span class="bio-research-time">0:00</span>' +
            '<div class="bio-research-sub">Checking MusicBrainz, Wikipedia and Discogs. ' +
            'Reading an official website with the AI can take up to a minute. ' +
            'You can leave this page: the result is saved and shown next time.</div></div>';
        host.insertBefore(banner, host.firstChild);

        var started = Date.now();
        var timeEl = banner.querySelector('.bio-research-time');
        elt._bioResearch = {
            banner: banner,
            showTimer: setTimeout(function () { banner.hidden = false; }, SHOW_AFTER_MS),
            interval: setInterval(function () {
                if (!banner.isConnected) { stop(elt); return; }
                timeEl.textContent = fmt(Math.floor((Date.now() - started) / 1000));
            }, 1000),
        };
        /* The response usually replaces the button that sent the request, and
         * htmx then fires afterRequest on the detached button where it never
         * reaches document; the XHR's loadend fires in every case (success,
         * error, abort). */
        var xhr = evt.detail.xhr;
        if (xhr && xhr.addEventListener) {
            xhr.addEventListener('loadend', function () { stop(elt); });
        }
    });

    document.addEventListener('htmx:afterRequest', function (evt) {
        stop(evt.detail && evt.detail.elt);
    });
}());
