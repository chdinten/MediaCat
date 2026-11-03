'use strict';

function _mergeCheckedIds() {
    return Array.from(
        document.querySelectorAll('#catalogue-pressing-list .pressing-merge-check:checked')
    ).map(function (c) { return c.getAttribute('data-id'); });
}

function catalogueMergeOpen() {
    var ids = _mergeCheckedIds();
    if (!ids.length) return;
    var url = '/catalogue/merge?winner_id=' + ids[0];
    if (ids[1]) url += '&loser_id=' + ids[1];
    window.location.href = url;
}

function catalogueMergeNav(link) {
    var ids = _mergeCheckedIds();
    link.href = ids.length
        ? '/catalogue/merge?winner_id=' + ids[0] + (ids[1] ? '&loser_id=' + ids[1] : '')
        : '/catalogue/merge';
    return true;
}

function catalogueMergeClear() {
    document.querySelectorAll('#catalogue-pressing-list .pressing-merge-check')
        .forEach(function (c) { c.checked = false; });
}

/* Delegated: the 3panel browser swaps these buttons in with the centre panel. */
document.addEventListener('click', function (e) {
    var t = e.target;
    if (!t || !t.closest) return;
    if (t.closest('#catalogue-merge-open-btn')) { catalogueMergeOpen(); return; }
    if (t.closest('#catalogue-merge-clear-btn')) { catalogueMergeClear(); return; }
    var hdr = t.closest('#merge-dup-btn');
    if (hdr) catalogueMergeNav(hdr);
});

/* Classic layout: reopen the artist (and album) named in the URL, e.g. when
 * coming back through the pressing page's breadcrumb (?artist=…&album=…).
 * The 3panel browser renders the selection on the server instead. */
document.addEventListener('DOMContentLoaded', function () {
    var params = new URLSearchParams(window.location.search);
    var artistParam = params.get('artist');
    var albumParam = params.get('album');
    var albumPanel = document.getElementById('album-panel');
    if (!artistParam || !albumPanel) return;
    var card = Array.from(document.querySelectorAll('.artist-card-header'))
        .find(function (el) { return el.dataset.artist === artistParam; });
    if (card) {
        card.closest('.artist-card').classList.add('artist-card--active');
    }
    fetch('/catalogue/albums?artist=' + encodeURIComponent(artistParam))
        .then(function (r) { return r.ok ? r.text() : ''; })
        .then(function (html) {
            if (!html) return;
            albumPanel.innerHTML = html;
            if (window.htmx) htmx.process(albumPanel);
            if (albumParam !== null) openAlbum(artistParam, albumParam);
        });
});

function openAlbum(artist, album) {
    var pressingPanel = document.getElementById('pressing-panel');
    if (!pressingPanel) return;
    Array.from(document.querySelectorAll('.album-list-row')).forEach(function (row) {
        var title = row.querySelector('.album-list-title');
        if (title && title.textContent.trim() === (album || 'Untitled')) {
            row.classList.add('album-list-row--active');
        }
    });
    fetch('/catalogue/pressings?artist=' + encodeURIComponent(artist) +
          '&title=' + encodeURIComponent(album))
        .then(function (r) { return r.ok ? r.text() : ''; })
        .then(function (html) {
            if (!html) return;
            pressingPanel.innerHTML = html;
            if (window.htmx) htmx.process(pressingPanel);
        });
}
