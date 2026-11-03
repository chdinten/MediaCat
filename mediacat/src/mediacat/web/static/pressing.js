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

    /* Confirm dialogs (data-confirm) are handled by forms.js. */

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

    /* ── 8b. Sales history help dialog ── */
    var salesHelpDlg   = document.getElementById('sales-help-dialog');
    var salesHelpOpen  = document.getElementById('sales-help-open');
    var salesHelpClose = document.getElementById('sales-help-close');
    if (salesHelpDlg && salesHelpOpen) {
        salesHelpOpen.addEventListener('click', function () { salesHelpDlg.showModal(); });
        if (salesHelpClose) {
            salesHelpClose.addEventListener('click', function () { salesHelpDlg.close(); });
        }
        salesHelpDlg.addEventListener('click', function (e) {
            if (e.target === salesHelpDlg) salesHelpDlg.close();
        });
    }

    /* ── 9. Genre edit toggle ── */
    var genreToggle = document.querySelector('.genre-edit-toggle');
    var genreForm   = document.querySelector('.genre-edit-form');
    var genreCancel = document.querySelector('.genre-edit-cancel');
    if (genreToggle && genreForm) {
        genreToggle.addEventListener('click', function () {
            genreForm.style.display = '';
            genreToggle.style.display = 'none';
        });
        if (genreCancel) {
            genreCancel.addEventListener('click', function () {
                genreForm.style.display = 'none';
                genreToggle.style.display = '';
            });
        }
    }

    /* ── 10. Available genre pill → add/remove chip (delegated) ── */
    document.addEventListener('click', function (e) {
        var btn = e.target.closest('.genre-available-btn');
        if (!btn) return;
        var widget = document.querySelector('.genre-tag-input');
        if (!widget) return;
        var value = btn.dataset.genre;
        /* Toggle: if chip exists, remove it; otherwise add it */
        var chips = widget.querySelectorAll('.genre-chip');
        for (var i = 0; i < chips.length; i++) {
            if (chips[i].dataset.value.toLowerCase() === value.toLowerCase()) {
                chips[i].remove();
                btn.classList.remove('genre-available-btn--active');
                return;
            }
        }
        /* Add chip */
        var chip = document.createElement('span');
        chip.className = 'genre-chip';
        chip.dataset.value = value;
        chip.textContent = value;
        var removeBtn = document.createElement('button');
        removeBtn.type = 'button';
        removeBtn.className = 'genre-chip-remove';
        removeBtn.setAttribute('aria-label', 'Remove');
        removeBtn.textContent = '×';
        removeBtn.addEventListener('click', function () {
            chip.remove();
            btn.classList.remove('genre-available-btn--active');
        });
        chip.appendChild(removeBtn);
        widget.insertBefore(chip, widget.querySelector('.genre-typeahead'));
        btn.classList.add('genre-available-btn--active');
    });

    /* ── 11. Import genres button → submit hidden form ── */
    document.addEventListener('click', function (e) {
        var btn = e.target.closest('.genre-import-btn');
        if (!btn) return;
        var form = btn.closest('.genre-edit-form').querySelector('.import-genres-form');
        if (form) form.requestSubmit ? form.requestSubmit() : form.submit();
    });

    /* ── 12. Image drop zone ── */
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

/* ── Price fetch loading indicator ─────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('form[action*="/fetch-price"]').forEach(function (form) {
        form.addEventListener('submit', function (e) {
            e.preventDefault();
            var btn = form.querySelector('button[type="submit"]');
            if (btn) {
                btn.disabled = true;
                btn.textContent = '⏳ Fetching price…';
                btn.style.opacity = '0.7';
            }

            /* Insert a status banner near the price history section */
            var priceSection = document.querySelector('.price-history-section, .price-history-empty');
            var statusEl = document.getElementById('price-fetch-status');
            if (!statusEl) {
                statusEl = document.createElement('div');
                statusEl.id = 'price-fetch-status';
                statusEl.style.cssText = 'margin:0.75rem 0;padding:0.6rem 0.9rem;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);font-size:0.85rem;color:var(--text-muted);display:flex;align-items:center;gap:0.6rem';
                statusEl.innerHTML =
                    '<span class="ocr-spinner" style="width:16px;height:16px;border-width:2px"></span>' +
                    '<span>Fetching current market price from Discogs — this may take a moment…</span>';
                if (priceSection) {
                    priceSection.insertAdjacentElement('beforebegin', statusEl);
                } else if (btn) {
                    btn.closest('form').insertAdjacentElement('afterend', statusEl);
                }
            }

            var fd = new FormData(form);
            fetch(form.action, {
                method: 'POST',
                headers: { 'X-CSRF-Token': mcCsrf() },
                body: fd,
                redirect: 'follow',
            }).then(function (res) {
                window.location.href = res.url || window.location.pathname;
            }).catch(function (err) {
                if (btn) { btn.disabled = false; btn.textContent = '$ Fetch price'; btn.style.opacity = ''; }
                if (statusEl) {
                    statusEl.innerHTML = '<span style="color:var(--danger)">Network error: ' + err.message + '</span>';
                }
            });
        });
    });
});

/* ── Star rating widget ─────────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('.star-widget').forEach(function (widget) {
        var inputId = widget.dataset.input;
        var formId  = widget.dataset.form;
        var input   = document.getElementById(inputId);
        var current = parseFloat(widget.dataset.current) || 0;
        var halves  = widget.querySelectorAll('.star-half');

        function render(val) {
            halves.forEach(function (h) {
                h.classList.toggle('filled', parseFloat(h.dataset.value) <= val);
            });
        }

        render(current);

        halves.forEach(function (h) {
            h.addEventListener('click', function () {
                var val = parseFloat(h.dataset.value);
                current = val;
                input.value = val * 2; /* store as 0–10 */
                render(val);
                /* Submit directly — don't rely on event bubbling to forms.js */
                var form = document.getElementById(formId);
                if (!form) return;
                var fd = new FormData(form);
                fetch(form.action, {
                    method: 'POST',
                    headers: { 'X-CSRF-Token': mcCsrf() },
                    body: fd,
                    redirect: 'follow',
                }).then(function (res) {
                    window.location.href = res.url || form.action;
                }).catch(function (err) {
                    console.error('[MediaCat] rating save error:', err);
                });
            });
            h.addEventListener('mouseenter', function () { render(parseFloat(h.dataset.value)); });
        });
        widget.addEventListener('mouseleave', function () { render(current); });
    });

    /* ── Persist disc-section open/closed state per token ── */
    var tokenUuid = document.getElementById('token-uuid');
    if (tokenUuid) {
        var storageKey = 'disc-state-' + tokenUuid.textContent.trim();
        var saved = {};
        try { saved = JSON.parse(localStorage.getItem(storageKey) || '{}'); } catch (_) {}
        document.querySelectorAll('.disc-section').forEach(function (el) {
            if (el.id in saved) {
                if (saved[el.id]) el.setAttribute('open', '');
                else               el.removeAttribute('open');
            }
            el.addEventListener('toggle', function () {
                saved[el.id] = el.open;
                try { localStorage.setItem(storageKey, JSON.stringify(saved)); } catch (_) {}
            });
        });
    }

    /* ── Genre tag-input widget ─────────────────────────────────────────── */
    document.querySelectorAll('.genre-tag-input').forEach(function (widget) {
        function addChip(value) {
            value = value.trim();
            if (!value) return;
            var existing = widget.querySelectorAll('.genre-chip');
            for (var i = 0; i < existing.length; i++) {
                if (existing[i].dataset.value.toLowerCase() === value.toLowerCase()) return;
            }
            var chip = document.createElement('span');
            chip.className = 'genre-chip';
            chip.dataset.value = value;
            chip.textContent = value;
            var btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'genre-chip-remove';
            btn.setAttribute('aria-label', 'Remove');
            btn.textContent = '×';
            btn.addEventListener('click', function () { chip.remove(); });
            chip.appendChild(btn);
            widget.insertBefore(chip, widget.querySelector('.genre-typeahead'));
        }

        var typeahead = widget.querySelector('.genre-typeahead');
        typeahead.addEventListener('keydown', function (e) {
            if (e.key === 'Enter' || e.key === ',') {
                var val = typeahead.value.replace(/,$/, '').trim();
                if (val) {
                    e.preventDefault();
                    addChip(val);
                    typeahead.value = '';
                } else if (e.key === ',') {
                    e.preventDefault(); // comma with nothing: swallow quietly
                }
                // Enter with empty typeahead: fall through → browser submits form
            } else if (e.key === 'Backspace' && typeahead.value === '') {
                var chips = widget.querySelectorAll('.genre-chip');
                if (chips.length) chips[chips.length - 1].remove();
            }
        });
        typeahead.addEventListener('change', function () {
            if (typeahead.value) { addChip(typeahead.value); typeahead.value = ''; }
        });
        widget.querySelectorAll('.genre-chip-remove').forEach(function (btn) {
            btn.addEventListener('click', function () { btn.closest('.genre-chip').remove(); });
        });
    });

    /* ── Catalogue filter: sync genre chips before GET form submit ───────── */
    var catForm = document.getElementById('catalogue-search-form');
    if (catForm) {
        catForm.addEventListener('submit', function () {
            var widget = catForm.querySelector('.genre-tag-input');
            var hidden = catForm.querySelector('input[name="genres"]');
            if (widget && hidden) {
                hidden.value = Array.from(widget.querySelectorAll('.genre-chip'))
                    .map(function (c) { return c.dataset.value; })
                    .join(',');
            }
        });
    }

    /* ── Genre form: direct submit with chip sync ───────────────────────── */
    document.querySelectorAll('.genre-form').forEach(function (form) {
        form.addEventListener('submit', function (e) {
            e.preventDefault();
            e.stopPropagation(); /* prevent forms.js double-handling */
            var widget = form.querySelector('.genre-tag-input');
            if (widget) {
                var hidden = form.querySelector('input[name="genres"]');
                if (hidden) {
                    hidden.value = Array.from(widget.querySelectorAll('.genre-chip'))
                        .map(function (c) { return c.dataset.value; })
                        .join(', ');
                }
            }
            var fd = new FormData(form);
            fetch(form.action, {
                method: 'POST',
                headers: { 'X-CSRF-Token': mcCsrf() },
                body: fd,
                redirect: 'follow',
            }).then(function (res) {
                window.location.href = res.url || window.location.pathname;
            }).catch(function (err) {
                console.error('[MediaCat] genre save error:', err);
            });
        });
    });
});


/* ── Discogs: album sheet PDF, runout copy, apply variant ───────────────── */
function mcCopyText(text, btn) {
    function done() {
        if (!btn) return;
        var orig = btn.textContent;
        btn.textContent = 'Copied!';
        setTimeout(function () { btn.textContent = orig; }, 1400);
    }
    if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(done);
        return;
    }
    var ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    done();
}

/* Ask for the save location first (the picker needs the click's user
 * activation), then fetch the PDF and write it.  Browsers without the File
 * System Access API (Firefox, Safari) fall back to a normal download. */
async function mcSavePdf(btn) {
    var url = btn.dataset.url;
    var name = btn.dataset.filename || 'album.pdf';
    var label = btn.textContent;
    var handle = null;
    if (window.showSaveFilePicker) {
        try {
            handle = await window.showSaveFilePicker({
                suggestedName: name,
                types: [{ description: 'PDF document', accept: { 'application/pdf': ['.pdf'] } }],
            });
        } catch (e) {
            if (e.name === 'AbortError') return;   /* user cancelled */
            handle = null;
        }
    }
    btn.disabled = true;
    btn.textContent = 'Creating PDF…';
    try {
        var res = await fetch(url, { credentials: 'same-origin' });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        var blob = await res.blob();
        if (handle) {
            var writable = await handle.createWritable();
            await writable.write(blob);
            await writable.close();
        } else {
            var a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = name;
            document.body.appendChild(a);
            a.click();
            a.remove();
            setTimeout(function () { URL.revokeObjectURL(a.href); }, 10000);
        }
        btn.textContent = 'Saved ✓';
    } catch (err) {
        console.error('[MediaCat] PDF export failed:', err);
        btn.textContent = 'PDF failed: ' + err.message;
    } finally {
        btn.disabled = false;
        setTimeout(function () { btn.textContent = label; }, 2500);
    }
}

document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('.js-print-sheet').forEach(function (btn) {
        btn.addEventListener('click', function () { mcSavePdf(btn); });
    });

    document.addEventListener('click', function (e) {
        var copyBtn = e.target.closest('.js-dc-copy');
        if (copyBtn) {
            mcCopyText(copyBtn.dataset.copy || '', copyBtn);
            return;
        }
        var applyBtn = e.target.closest('.js-dc-apply-variant');
        if (!applyBtn) return;
        var editBox = document.getElementById('dc-edit');
        if (!editBox) return;
        /* First ticked code per field wins (a variant rarely lists a side twice). */
        var fill = {};
        applyBtn.closest('.dc-variant').querySelectorAll('.js-dc-pick:checked').forEach(function (cb) {
            if (!(cb.dataset.key in fill)) fill[cb.dataset.key] = cb.dataset.value;
        });
        editBox.querySelectorAll('.dc-filled').forEach(function (el) { el.classList.remove('dc-filled'); });
        var applied = 0;
        Object.keys(fill).forEach(function (key) {
            var input = editBox.querySelector('[data-key="' + key + '"]');
            if (!input) return;
            input.value = fill[key];
            input.classList.add('dc-filled');
            applied += 1;
        });
        var copyCard = document.getElementById('discogs-copy');
        mcSetCard(copyCard, true);
        copyCard.scrollIntoView({ behavior: 'smooth', block: 'start' });
        if (!applied) {
            window.alert('No runout fields (e.g. "Runout / Matrix LP1 - A") were found in your Discogs collection fields.');
        }
    });

    /* Runout tick-boxes: a selection belongs to one variant, so ticking a
     * code in another variant clears the previous one. */
    var editBox = document.getElementById('dc-edit');
    document.querySelectorAll('.js-dc-pick').forEach(function (cb) {
        var hasField = editBox && editBox.querySelector('[data-key="' + cb.dataset.key + '"]');
        if (!hasField) {
            cb.disabled = true;
            cb.title = 'Your Discogs collection has no field for this side';
        }
    });
    document.querySelectorAll('.dc-variant--pick').forEach(mcUpdatePick);

    document.addEventListener('change', function (e) {
        /* Manufacturing card: show the chosen variant, drop ticks on the others. */
        var choose = e.target.closest('.js-dc-variant-select');
        if (choose) {
            choose.closest('.dc-mfg-variants').querySelectorAll('.dc-variant--choice').forEach(function (v) {
                var shown = v.dataset.choice === choose.value;
                v.hidden = !shown;
                if (!shown) {
                    v.querySelectorAll('.js-dc-pick, .js-dc-pick-all').forEach(function (cb) { cb.checked = false; });
                    mcUpdatePick(v);
                }
            });
            return;
        }
        var pick = e.target.closest('.js-dc-pick, .js-dc-pick-all');
        if (!pick) return;
        var variant = pick.closest('.dc-variant');
        if (pick.classList.contains('js-dc-pick-all')) {
            variant.querySelectorAll('.js-dc-pick:not(:disabled)').forEach(function (cb) {
                cb.checked = pick.checked;
            });
        }
        if (pick.checked) {
            document.querySelectorAll('.dc-variant--pick').forEach(function (other) {
                if (other === variant) return;
                other.querySelectorAll('.js-dc-pick, .js-dc-pick-all').forEach(function (cb) { cb.checked = false; });
                mcUpdatePick(other);
            });
        }
        mcUpdatePick(variant);
    });
});

/* Sync a variant's "All" box and copy button with its ticked codes. */
function mcUpdatePick(variant) {
    var boxes = Array.prototype.slice.call(variant.querySelectorAll('.js-dc-pick:not(:disabled)'));
    var ticked = boxes.filter(function (cb) { return cb.checked; }).length;
    var all = variant.querySelector('.js-dc-pick-all');
    var btn = variant.querySelector('.js-dc-apply-variant');
    if (all) {
        all.disabled = !boxes.length;
        all.checked = boxes.length > 0 && ticked === boxes.length;
        all.indeterminate = ticked > 0 && ticked < boxes.length;
    }
    if (btn) {
        btn.disabled = !ticked;
        btn.textContent = ticked && ticked < boxes.length
            ? 'Copy ' + ticked + ' to My copy' : 'Copy to My copy';
    }
    variant.classList.toggle('is-picking', ticked > 0);
}

/* ── Detail cards ───────────────────────────────────────────────────────────
 * A closed card shows its key details; opening one makes it span the full
 * grid width (CSS .is-open). Only one card is open at a time. Clicking the
 * header button toggles; clicking anywhere else on a closed card opens it. */
function mcSetCard(card, open) {
    if (!card) return;
    if (open) {
        document.querySelectorAll('.dc-card.is-open').forEach(function (c) {
            if (c !== card) mcSetCard(c, false);
        });
    }
    card.classList.toggle('is-open', open);
    var btn = card.querySelector('.js-card-toggle');
    if (btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false');
}

document.addEventListener('DOMContentLoaded', function () {
    document.addEventListener('click', function (e) {
        var toggle = e.target.closest('.js-card-toggle');
        if (toggle) {
            var tc = toggle.closest('.dc-card');
            var opening = !tc.classList.contains('is-open');
            mcSetCard(tc, opening);
            if (opening) tc.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            return;
        }
        var card = e.target.closest('.dc-card');
        if (!card || card.classList.contains('is-open')) return;
        if (e.target.closest('a, button, input, select, textarea, label, form, summary')) return;
        if (window.getSelection && String(window.getSelection())) return;  /* selecting text */
        mcSetCard(card, true);
    });

    document.addEventListener('keydown', function (e) {
        if (e.key !== 'Escape' || document.querySelector('dialog[open]')) return;
        var open = document.querySelector('.dc-card.is-open');
        if (open) mcSetCard(open, false);
    });
});

/* ── Image lightbox ─────────────────────────────────────────────────────────
 * Thumbnails (.image-thumb-link) open in a dialog above the page instead of a
 * bare image tab. Arrows / ← → step through the images of the same group
 * (gallery or runout list); Esc, × or a click beside the image closes it.
 * Ctrl/⌘/Shift/middle click keep the browser's normal "open in new tab". */
var mcLightbox = null;

function mcLightboxBuild() {
    var dlg = document.createElement('dialog');
    dlg.className = 'mc-lightbox';
    dlg.setAttribute('aria-label', 'Image viewer');
    dlg.innerHTML =
        '<div class="mc-lb-bar">' +
        '  <span class="mc-lb-caption"></span>' +
        '  <a class="mc-lb-open btn btn-ghost btn-xs" target="_blank" rel="noopener">Open full size ↗</a>' +
        '  <button type="button" class="mc-lb-close" aria-label="Close">×</button>' +
        '</div>' +
        '<button type="button" class="mc-lb-nav mc-lb-prev" aria-label="Previous image">‹</button>' +
        '<div class="mc-lb-stage"><img class="mc-lb-img" alt=""></div>' +
        '<button type="button" class="mc-lb-nav mc-lb-next" aria-label="Next image">›</button>';
    document.body.appendChild(dlg);
    dlg.addEventListener('click', function (e) {
        /* the dialog itself (backdrop) or the empty stage around the image */
        if (e.target.closest('.mc-lb-close') || e.target === dlg ||
            e.target.classList.contains('mc-lb-stage')) { dlg.close(); return; }
        if (e.target.closest('.mc-lb-prev')) mcLightboxStep(-1);
        if (e.target.closest('.mc-lb-next')) mcLightboxStep(1);
    });
    dlg.addEventListener('keydown', function (e) {
        if (e.key === 'ArrowLeft') { e.preventDefault(); mcLightboxStep(-1); }
        if (e.key === 'ArrowRight') { e.preventDefault(); mcLightboxStep(1); }
    });
    dlg.addEventListener('close', function () {
        dlg.querySelector('.mc-lb-img').removeAttribute('src');
    });
    return { dlg: dlg, links: [], index: 0 };
}

function mcLightboxShow(i) {
    var lb = mcLightbox;
    var n = lb.links.length;
    lb.index = (i + n) % n;
    var link = lb.links[lb.index];
    var thumb = link.querySelector('img');
    var label = thumb ? thumb.alt : '';
    var img = lb.dlg.querySelector('.mc-lb-img');
    img.src = link.href;
    img.alt = label;
    lb.dlg.querySelector('.mc-lb-caption').textContent = label + (n > 1 ? '  ·  ' + (lb.index + 1) + ' / ' + n : '');
    lb.dlg.querySelector('.mc-lb-open').href = link.href;
    lb.dlg.querySelectorAll('.mc-lb-nav').forEach(function (b) { b.hidden = n < 2; });
}

function mcLightboxStep(delta) {
    if (mcLightbox && mcLightbox.links.length > 1) mcLightboxShow(mcLightbox.index + delta);
}

document.addEventListener('click', function (e) {
    var link = e.target.closest('a.image-thumb-link');
    if (!link || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    if (!mcLightbox) mcLightbox = mcLightboxBuild();
    var group = link.closest('.image-grid, .runout-image-list, .dc-preview-images') || document;
    mcLightbox.links = Array.prototype.slice.call(group.querySelectorAll('a.image-thumb-link'));
    mcLightboxShow(Math.max(0, mcLightbox.links.indexOf(link)));
    mcLightbox.dlg.showModal();
    mcLightbox.dlg.querySelector('.mc-lb-close').focus();
});

/* ── Add pressing: Table / Cards view of the search results ─────────────────
 * The cards load Discogs images and matrix codes lazily (hx-trigger="intersect
 * once"), so nothing is fetched until the cards view is shown.  The choice is
 * remembered per browser. */
(function () {
    var KEY = 'mc-find-view';

    function setView(box, view) {
        var cards = box.querySelector('.find-cards');
        var table = box.querySelector('.find-results-table-wrap');
        if (!cards || !table) return;
        cards.hidden = view !== 'cards';
        table.hidden = view === 'cards';
        box.querySelectorAll('.js-find-view').forEach(function (b) {
            var on = b.dataset.view === view;
            b.classList.toggle('is-active', on);
            b.setAttribute('aria-pressed', on ? 'true' : 'false');
        });
    }

    document.addEventListener('click', function (e) {
        var btn = e.target.closest('.js-find-view');
        if (!btn) return;
        setView(btn.closest('.js-find-results'), btn.dataset.view);
        try { localStorage.setItem(KEY, btn.dataset.view); } catch (_) { /* blocked */ }
    });

    document.addEventListener('DOMContentLoaded', function () {
        var saved = null;
        try { saved = localStorage.getItem(KEY); } catch (_) { /* blocked */ }
        if (saved !== 'cards') return;
        document.querySelectorAll('.js-find-results').forEach(function (box) { setView(box, 'cards'); });
    });
}());

/* ── Back link and previous / next pressing ─────────────────────────────────
 * "← Back to …" (data-mc-back) points at the page the pressing was opened
 * from.  When that is exactly the previous history entry, use the browser's
 * Back instead of a new visit: the back/forward cache then restores the view
 * as it was, and Back from there still leaves the catalogue.  J / K step to
 * the next / previous pressing of the album (rel="next" / rel="prev").
 */
(function () {
    'use strict';

    document.addEventListener('click', function (e) {
        var a = e.target.closest ? e.target.closest('a[data-mc-back]') : null;
        if (!a || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
        if (document.referrer && document.referrer === a.href && window.history.length > 1) {
            e.preventDefault();
            window.history.back();
        }
    });

    document.addEventListener('keydown', function (e) {
        if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey) return;
        var t = e.target;
        if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
        var rel = e.key === 'j' ? 'next' : (e.key === 'k' ? 'prev' : '');
        var link = rel && document.querySelector('.detail-nav a[rel="' + rel + '"]');
        if (link) {
            e.preventDefault();
            window.location.href = link.href;
        }
    });
}());
