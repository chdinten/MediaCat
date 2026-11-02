/* admin.js — live polling for collection import job status */
(function () {
  'use strict';

  /* Map job status → indicator class */
  var STATUS_CLASS = {
    running:   'status-running',
    pending:   'status-pending',
    paused:    'status-paused',
    completed: 'status-completed',
    failed:    'status-failed',
  };

  function setIndicator(card, status) {
    var dot = card.querySelector('.import-status-dot');
    if (!dot) return;
    dot.className = 'import-status-dot ' + (STATUS_CLASS[status] || 'status-pending');
    dot.title = status;
  }

  function ensureProgressBar(card) {
    var wrap = card.querySelector('.import-progress-wrap');
    if (wrap) return wrap;
    wrap = document.createElement('div');
    wrap.className = 'import-progress-wrap';
    wrap.innerHTML =
      '<div class="import-progress-bar"><div class="import-progress-fill" style="width:0%"></div></div>' +
      '<span class="import-progress-label">—</span>';
    /* Insert before the error div or actions div */
    var actions = card.querySelector('.import-job-actions');
    if (actions) {
      card.insertBefore(wrap, actions);
    } else {
      card.appendChild(wrap);
    }
    return wrap;
  }

  function pollJob(jobId) {
    var card = document.getElementById('job-' + jobId);
    if (!card) return;
    var url = '/admin/import/status/' + jobId;

    var interval = setInterval(function () {
      fetch(url, { headers: { 'Accept': 'application/json' } })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          var status = data.status || '';

          /* Update status badge text */
          var badge = card.querySelector('.badge');
          if (badge) {
            badge.textContent = status;
            badge.className = 'badge badge-' + status;
          }

          /* Update colour indicator */
          setIndicator(card, status);

          /* Update live-status text */
          var statusEl = card.querySelector('[data-job-id]');
          if (statusEl) {
            statusEl.textContent =
              status +
              (data.imported_count != null
                ? ' — ' + data.imported_count + ' imported, ' +
                  data.skipped_count + ' skipped, ' +
                  data.failed_count + ' failed'
                : '');
          }

          /* Create / update progress bar */
          if (data.total_releases) {
            var wrap = ensureProgressBar(card);
            var fill = wrap.querySelector('.import-progress-fill');
            var label = wrap.querySelector('.import-progress-label');
            var done = (data.imported_count || 0) + (data.skipped_count || 0) + (data.failed_count || 0);
            var pct = Math.round((done / data.total_releases) * 100);
            if (fill) fill.style.width = pct + '%';
            if (label) {
              label.textContent = done + ' / ' + data.total_releases +
                ' (' + (data.imported_count || 0) + ' imported · ' +
                (data.skipped_count || 0) + ' skipped · ' +
                (data.failed_count || 0) + ' failed)';
            }
          }

          /* Update error message if present */
          if (data.error_message) {
            var errEl = card.querySelector('.import-error-msg');
            if (!errEl) {
              errEl = document.createElement('div');
              errEl.className = 'import-error-msg text-muted';
              errEl.style.cssText = 'font-size:0.8rem;margin-top:0.4rem;color:var(--text-danger)';
              var actions = card.querySelector('.import-job-actions');
              if (actions) card.insertBefore(errEl, actions);
              else card.appendChild(errEl);
            }
            errEl.textContent = data.error_message;
          }

          if (status === 'completed' || status === 'failed' || status === 'paused') {
            clearInterval(interval);
            window.location.reload();
          }
        })
        .catch(function () { /* ignore transient fetch errors */ });
    }, 3000);
  }

  /* ── Housekeeping: checkbox selection tracking ── */

  function hkUpdateDeleteBar() {
    var checked = document.querySelectorAll('#hk-results .hk-check:checked');
    var bar = document.getElementById('hk-delete-bar');
    var countEl = document.getElementById('hk-selected-count');
    if (!bar) return;
    if (checked.length > 0) {
      bar.style.display = 'flex';
      if (countEl) countEl.textContent = checked.length + ' pressing(s) selected';
    } else {
      bar.style.display = 'none';
    }
  }

  function hkBindResults() {
    /* Delegate checkbox changes from #hk-results */
    var results = document.getElementById('hk-results');
    if (!results) return;
    results.addEventListener('change', function (e) {
      if (e.target && e.target.classList.contains('hk-check')) {
        hkUpdateDeleteBar();
      }
      /* Select-all checkbox */
      var allBox = document.getElementById('hk-check-all');
      if (e.target === allBox) {
        results.querySelectorAll('.hk-check').forEach(function (cb) {
          cb.checked = allBox.checked;
        });
        hkUpdateDeleteBar();
      }
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    /* Auto-start polling for any active job card on the page */
    document.querySelectorAll('[data-job-id]').forEach(function (el) {
      pollJob(el.dataset.jobId);
    });

    /* Toggle new user form */
    var toggleBtn = document.getElementById('create-user-toggle');
    if (toggleBtn) {
      toggleBtn.addEventListener('click', function () {
        var form = document.getElementById('create-user-form');
        if (form) form.classList.toggle('hidden');
      });
    }

    /* Housekeeping */
    hkBindResults();
  });

  /* Re-bind after HTMX swaps in new search results */
  document.addEventListener('htmx:afterSwap', function (e) {
    if (e.detail && e.detail.target && e.detail.target.id === 'hk-results') {
      hkUpdateDeleteBar();
    }
  });
}());
