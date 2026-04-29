/* pressing.js — interactive behaviour for the pressing detail page.
 * Loaded via <script src="/static/pressing.js"> on pressing_detail.html.
 * Requires a <meta name="csrf-token"> in <head> (set by base.html).
 *
 * No inline event handlers (onclick=) are used — all wiring is done here
 * to comply with CSP script-src 'self' which blocks inline handlers.
 */
'use strict';

function mcCsrf() {
    try { return document.querySelector('meta[name="csrf-token"]').content; } catch (e) { return ''; }
}

/* ── 1. AI analysis ─────────────────────────────────────────────────────── */
window.mcAnalyse = function (btn, url, colId) {
    var col = colId ? document.getElementById(colId) : null;
    btn.disabled = true;
    btn.textContent = 'Analysing…';
    btn.style.opacity = '0.7';
    if (col) {
        col.innerHTML =
            '<div class="ocr-progress">' +
            '<div class="ocr-spinner"></div>' +
            '<div><strong>AI analysis running</strong><br>' +
            '<span class="text-muted" style="font-size:0.85rem">' +
            'This can take up to 2 minutes — please wait…' +
            '</span></div></div>';
    }
    fetch(url, { method: 'POST', headers: { 'X-CSRF-Token': mcCsrf() } })
        .then(function (res) {
            if (res.redirected || res.ok) {
                window.location.href = res.url || window.location.pathname;
            } else {
                res.text().then(function (body) {
                    var msg = 'HTTP ' + res.status;
                    try { msg = JSON.parse(body).detail || msg; } catch (e2) { }
                    btn.disabled = false;
                    btn.textContent = '⚙ Analyse with AI';
                    btn.style.opacity = '';
                    if (col) col.innerHTML = '<div class="ocr-empty" style="color:var(--danger)">Error: ' + msg + '</div>';
                });
            }
        })
        .catch(function (err) {
            btn.disabled = false;
            btn.textContent = '⚙ Analyse with AI';
            btn.style.opacity = '';
            if (col) col.innerHTML = '<div class="ocr-empty" style="color:var(--danger)">Network error: ' + err.message + '</div>';
        });
};

/* ── 2. Image crop tool ─────────────────────────────────────────────────── */
var _crop = { tokenId: '', imageId: '', x0: 0, y0: 0, x1: 0, y1: 0, active: false, hasSel: false };

window.mcOpenCrop = function (tokenId, imageId) {
    _crop.tokenId = tokenId;
    _crop.imageId = imageId;
    _crop.hasSel  = false;
    _crop.active  = false;

    var modal   = document.getElementById('crop-modal');
    var img     = document.getElementById('crop-img');
    var canvas  = document.getElementById('crop-canvas');
    var saveBtn = document.getElementById('crop-save-btn');
    var status  = document.getElementById('crop-status');
    var info    = document.getElementById('crop-sel-info');

    saveBtn.disabled    = true;
    saveBtn.textContent = 'Save as new image';
    status.style.display = 'none';
    info.style.display   = 'none';
    canvas.getContext('2d').clearRect(0, 0, canvas.width, canvas.height);

    modal.showModal();

    img.onload = function () {
        canvas.width        = img.offsetWidth;
        canvas.height       = img.offsetHeight;
        canvas.style.width  = img.offsetWidth  + 'px';
        canvas.style.height = img.offsetHeight + 'px';
        canvas.getContext('2d').clearRect(0, 0, canvas.width, canvas.height);
    };
    img.src = '/catalogue/' + tokenId + '/images/' + imageId;
};

window.mcCloseCrop = function () {
    document.getElementById('crop-modal').close();
};

function _cropDraw() {
    var canvas = document.getElementById('crop-canvas');
    var img    = document.getElementById('crop-img');
    var info   = document.getElementById('crop-sel-info');
    var ctx    = canvas.getContext('2d');
    var x = Math.min(_crop.x0, _crop.x1), y = Math.min(_crop.y0, _crop.y1);
    var w = Math.abs(_crop.x1 - _crop.x0), h = Math.abs(_crop.y1 - _crop.y0);

    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (w < 2 || h < 2) return;

    ctx.fillStyle = 'rgba(0,0,0,0.55)';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.clearRect(x, y, w, h);

    ctx.strokeStyle = '#6366f1';
    ctx.lineWidth   = 2;
    ctx.setLineDash([6, 3]);
    ctx.strokeRect(x, y, w, h);
    ctx.setLineDash([]);

    var sx = img.naturalWidth  / img.offsetWidth;
    var sy = img.naturalHeight / img.offsetHeight;
    info.textContent  = Math.round(w * sx) + ' × ' + Math.round(h * sy) + ' px selected';
    info.style.display = 'block';
}

window.mcSaveCrop = function () {
    if (!_crop.hasSel) return;
    var img     = document.getElementById('crop-img');
    var saveBtn = document.getElementById('crop-save-btn');
    var status  = document.getElementById('crop-status');
    var region  = document.getElementById('crop-region-select').value;

    var x  = Math.min(_crop.x0, _crop.x1), y  = Math.min(_crop.y0, _crop.y1);
    var w  = Math.abs(_crop.x1 - _crop.x0), h  = Math.abs(_crop.y1 - _crop.y0);
    var sx = img.naturalWidth  / img.offsetWidth;
    var sy = img.naturalHeight / img.offsetHeight;
    var px = Math.round(x * sx), py = Math.round(y * sy);
    var pw = Math.round(w * sx), ph = Math.round(h * sy);
    if (pw < 4 || ph < 4) return;

    var out = document.createElement('canvas');
    out.width  = pw;
    out.height = ph;
    out.getContext('2d').drawImage(img, px, py, pw, ph, 0, 0, pw, ph);

    saveBtn.disabled    = true;
    saveBtn.textContent = 'Saving…';
    status.textContent  = 'Uploading cropped image…';
    status.className    = 'upload-status';
    status.style.display = 'block';

    out.toBlob(function (blob) {
        if (!blob) {
            status.textContent = 'Crop failed — could not read image pixels.';
            status.className   = 'upload-status upload-status--warn';
            saveBtn.disabled   = false;
            saveBtn.textContent = 'Save as new image';
            return;
        }
        var fd = new FormData();
        fd.append('file', blob, 'crop.jpg');
        fd.append('region', region);
        fetch('/catalogue/' + _crop.tokenId + '/images', {
            method:   'POST',
            headers:  { 'X-CSRF-Token': mcCsrf() },
            body:     fd,
            redirect: 'manual',
        }).then(function (res) {
            if (res.ok || res.type === 'opaqueredirect') {
                status.textContent = 'Saved! Reloading…';
                status.className   = 'upload-status upload-status--ok';
                setTimeout(function () { window.location.reload(); }, 900);
            } else {
                status.textContent = 'Upload failed (HTTP ' + res.status + ')';
                status.className   = 'upload-status upload-status--warn';
                saveBtn.disabled   = false;
                saveBtn.textContent = 'Save as new image';
            }
        }).catch(function (err) {
            status.textContent = 'Network error: ' + err.message;
            status.className   = 'upload-status upload-status--warn';
            saveBtn.disabled   = false;
            saveBtn.textContent = 'Save as new image';
        });
    }, 'image/jpeg', 0.92);
};

/* ── 3. Matrix field correction modal ──────────────────────────────────── */
window.mcOpenCorrect = function (fieldKey, side, label, currentValue) {
    var dlg      = document.getElementById('mf-correct-modal');
    var title    = document.getElementById('mf-correct-title');
    var hint     = document.getElementById('mf-correct-hint');
    var fldInput = document.getElementById('mf-correct-field');
    var sideInput = document.getElementById('mf-correct-side');
    var valInput = document.getElementById('mf-correct-value');
    var reasonText = document.getElementById('mf-correct-reason-text');

    title.textContent = 'Correct: ' + label;
    hint.textContent  = currentValue
        ? 'Current value: ' + currentValue
        : 'No value recorded yet — you can set one below.';
    fldInput.value  = fieldKey;
    sideInput.value = side;
    valInput.value  = currentValue || '';
    reasonText.value = '';

    /* Reset radio buttons */
    var radios = dlg.querySelectorAll('input[type="radio"]');
    radios.forEach(function (r) { r.checked = false; });

    dlg.showModal();
    valInput.focus();
    valInput.select();
};

window.mcCloseCorrect = function () {
    var dlg = document.getElementById('mf-correct-modal');
    if (dlg) dlg.close();
};

/* ── DOMContentLoaded: wire all event listeners ─────────────────────────── */
document.addEventListener('DOMContentLoaded', function () {

    /* ── Correction modal buttons ── */
    var correctCloseBtn  = document.getElementById('mf-correct-close-btn');
    var correctCancelBtn = document.getElementById('mf-correct-cancel-btn');
    if (correctCloseBtn)  correctCloseBtn.addEventListener('click',  mcCloseCorrect);
    if (correctCancelBtn) correctCancelBtn.addEventListener('click', mcCloseCorrect);

    var correctDlg = document.getElementById('mf-correct-modal');
    if (correctDlg) {
        correctDlg.addEventListener('click',  function (e) { if (e.target === correctDlg) mcCloseCorrect(); });
        correctDlg.addEventListener('cancel', function (e) { e.preventDefault(); mcCloseCorrect(); });
    }

    /* ── Edit buttons on matrix breakdown rows (delegated) ── */
    document.addEventListener('click', function (e) {
        var btn = e.target.closest('.js-mf-edit-btn');
        if (!btn) return;
        mcOpenCorrect(
            btn.dataset.field,
            btn.dataset.side,
            btn.dataset.label,
            btn.dataset.current
        );
    });

    /* ── 4. Confirm dialogs for Archive / Delete ── */
    document.addEventListener('click', function (e) {
        var btn = e.target.closest('button[data-confirm]');
        if (!btn) return;
        if (!window.confirm(btn.getAttribute('data-confirm'))) {
            e.preventDefault();
            e.stopPropagation();
        }
    }, true);   /* capture phase so we can cancel before the submit fires */

    /* ── 4. Copy Token ID ── */
    var copyBtn = document.getElementById('copy-token-id-btn');
    if (copyBtn) {
        copyBtn.addEventListener('click', function () {
            var text = (document.getElementById('token-uuid') || {}).textContent || '';
            text = text.trim();
            if (navigator.clipboard) {
                navigator.clipboard.writeText(text).then(function () {
                    copyBtn.textContent = 'Copied!';
                    setTimeout(function () { copyBtn.textContent = 'Copy'; }, 1500);
                });
            } else {
                var ta = document.createElement('textarea');
                ta.value = text;
                document.body.appendChild(ta);
                ta.select();
                document.execCommand('copy');
                document.body.removeChild(ta);
                copyBtn.textContent = 'Copied!';
                setTimeout(function () { copyBtn.textContent = 'Copy'; }, 1500);
            }
        });
    }

    /* ── 5. Analyse with AI buttons (delegated — many per page) ── */
    document.addEventListener('click', function (e) {
        var btn = e.target.closest('.js-analyse-btn');
        if (!btn) return;
        mcAnalyse(btn, btn.dataset.analyseUrl, btn.dataset.colId);
    });

    /* ── 6. Crop region buttons (delegated — many per page) ── */
    document.addEventListener('click', function (e) {
        var btn = e.target.closest('.js-crop-btn');
        if (!btn) return;
        mcOpenCrop(btn.dataset.tokenId, btn.dataset.imageId);
    });

    /* ── 7. Crop modal canvas mouse events ── */
    var canvas = document.getElementById('crop-canvas');
    if (canvas) {
        canvas.addEventListener('mousedown', function (e) {
            e.preventDefault();
            var r    = canvas.getBoundingClientRect();
            _crop.x0 = _crop.x1 = e.clientX - r.left;
            _crop.y0 = _crop.y1 = e.clientY - r.top;
            _crop.active = true;
            _crop.hasSel = false;
            document.getElementById('crop-save-btn').disabled = true;
        });
        canvas.addEventListener('mousemove', function (e) {
            if (!_crop.active) return;
            var r    = canvas.getBoundingClientRect();
            _crop.x1 = e.clientX - r.left;
            _crop.y1 = e.clientY - r.top;
            _cropDraw();
        });
        function endDrag() {
            if (!_crop.active) return;
            _crop.active = false;
            if (Math.abs(_crop.x1 - _crop.x0) > 8 && Math.abs(_crop.y1 - _crop.y0) > 8) {
                _crop.hasSel = true;
                document.getElementById('crop-save-btn').disabled = false;
            }
        }
        canvas.addEventListener('mouseup',    endDrag);
        canvas.addEventListener('mouseleave', endDrag);
    }

    /* ── 8. Crop modal close / save / cancel buttons ── */
    var saveBtn   = document.getElementById('crop-save-btn');
    var closeBtn  = document.getElementById('crop-close-btn');
    var cancelBtn = document.getElementById('crop-cancel-btn');
    if (saveBtn)   saveBtn.addEventListener('click',   mcSaveCrop);
    if (closeBtn)  closeBtn.addEventListener('click',  mcCloseCrop);
    if (cancelBtn) cancelBtn.addEventListener('click', mcCloseCrop);

    /* Close on backdrop click or Escape */
    var dlg = document.getElementById('crop-modal');
    if (dlg) {
        dlg.addEventListener('click',  function (e) { if (e.target === dlg) mcCloseCrop(); });
        dlg.addEventListener('cancel', function (e) { e.preventDefault(); mcCloseCrop(); });
    }

    /* ── 9. Image drop zone ── */
    var zone   = document.getElementById('drop-zone');
    var fileIn = document.getElementById('img-file');
    var region = document.getElementById('region-select');
    var status = document.getElementById('upload-status');

    /* Sync hidden region select from the visible one */
    var visReg = document.getElementById('region-select-visible');
    if (visReg && region) {
        visReg.addEventListener('change', function () { region.value = this.value; });
    }

    if (zone && fileIn) {
        var tokenId = zone.dataset.tokenId;

        function showStatus(msg, cls) {
            status.textContent = msg;
            status.className   = 'upload-status' + (cls ? ' upload-status--' + cls : '');
            status.style.display = 'block';
        }

        /* Document-level drag handling: preventDefault fires for all elements,
         * zone highlight is toggled by containment check.
         * This avoids the "file opens in new tab" failure that occurs when the
         * drag cursor is over a child element that has no zone-level listener. */
        document.addEventListener('dragover', function (e) {
            e.preventDefault();
            if (zone.contains(e.target)) {
                zone.classList.add('drop-zone--active');
            }
        });
        document.addEventListener('dragleave', function (e) {
            if (zone.classList.contains('drop-zone--active') && !zone.contains(e.relatedTarget)) {
                zone.classList.remove('drop-zone--active');
            }
        });
        document.addEventListener('drop', function (e) {
            e.preventDefault();
            zone.classList.remove('drop-zone--active');
            if (!zone.contains(e.target)) return;
            var files = Array.from(e.dataTransfer.files).filter(function (f) { return f.type.startsWith('image/'); });
            if (!files.length) { showStatus('No image files detected.', 'warn'); return; }
            doUpload(files);
        });

        zone.addEventListener('click', function (e) { if (e.target.tagName !== 'LABEL') fileIn.click(); });
        fileIn.addEventListener('change', function () {
            if (!this.files || !this.files.length) return;
            doUpload(Array.from(this.files));
            this.value = '';
        });

        function doUpload(files) {
            showStatus('Uploading ' + files.length + ' file' + (files.length > 1 ? 's' : '') + '…');
            zone.classList.add('drop-zone--uploading');
            var done = 0, ok = 0;
            files.forEach(function (file) {
                var fd = new FormData();
                fd.append('file', file);
                fd.append('region', region ? region.value : 'cover_front');
                fetch('/catalogue/' + tokenId + '/images', {
                    method:   'POST',
                    headers:  { 'X-CSRF-Token': mcCsrf() },
                    body:     fd,
                    redirect: 'manual',
                }).then(function (res) {
                    if (res.ok || res.type === 'opaqueredirect') ok++;
                    done++;
                    if (done === files.length) finish(ok, files.length);
                }).catch(function () {
                    done++;
                    if (done === files.length) finish(ok, files.length);
                });
            });
        }

        function finish(ok, total) {
            zone.classList.remove('drop-zone--uploading');
            showStatus(
                ok === total
                    ? ok + ' image' + (ok > 1 ? 's' : '') + ' uploaded — reloading…'
                    : ok + ' of ' + total + ' uploaded — reloading…',
                ok === total ? 'ok' : 'warn'
            );
            setTimeout(function () { window.location.reload(); }, ok === total ? 800 : 1400);
        }
    }
});
