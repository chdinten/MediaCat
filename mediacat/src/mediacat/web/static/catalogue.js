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

document.addEventListener('DOMContentLoaded', function () {
    var openBtn = document.getElementById('catalogue-merge-open-btn');
    if (openBtn) openBtn.addEventListener('click', catalogueMergeOpen);

    var clearBtn = document.getElementById('catalogue-merge-clear-btn');
    if (clearBtn) clearBtn.addEventListener('click', catalogueMergeClear);

    var hdrBtn = document.getElementById('merge-dup-btn');
    if (hdrBtn) hdrBtn.addEventListener('click', function () { catalogueMergeNav(this); });

    // Auto-open artist panel when navigating back via breadcrumb (?artist=...)
    var params = new URLSearchParams(window.location.search);
    var artistParam = params.get('artist');
    var albumPanel = document.getElementById('album-panel');
    if (artistParam && albumPanel) {
        var enc = encodeURIComponent(artistParam);
        var card = Array.from(document.querySelectorAll('.artist-card-header'))
            .find(function (el) { return el.dataset.artist === artistParam; });
        if (card) {
            card.closest('.artist-card').classList.add('artist-card--active');
        }
        fetch('/catalogue/albums?artist=' + enc)
            .then(function (r) {
                if (!r.ok) { return; }
                return r.text();
            })
            .then(function (html) {
                if (!html) { return; }
                albumPanel.innerHTML = html;
                if (window.htmx) { htmx.process(albumPanel); }
            });
    }
});
