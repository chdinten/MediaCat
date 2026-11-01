/* forms.js — Attach X-CSRF-Token header to all native HTML form POSTs.
 *
 * HTMX already sends X-CSRF-Token via hx-headers on <body>; this script
 * handles the plain <form method="post"> case without changing any markup.
 *
 * On 303 redirect the browser is sent to the final URL.
 * On 4xx/2xx HTML responses the page content is replaced in place.
 */
(function () {
  'use strict';

  function getCsrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : '';
  }

  document.addEventListener('DOMContentLoaded', function () {
    document.addEventListener('submit', function (e) {
      const form = e.target;
      if (!form || form.method.toLowerCase() !== 'post') return;
      /* Skip forms that HTMX manages (it adds its own X-CSRF-Token header). */
      if (form.hasAttribute('hx-post') || form.closest('[hx-boost]')) return;

      e.preventDefault();
      const token = getCsrfToken();
      /* getAttribute, not form.action: a control named "action" inside the
         form shadows the property and would yield the element, not a URL. */
      const url = form.getAttribute('action') || window.location.href;

      /* Sync genre chip widget to hidden field before capturing FormData */
      const genreWidget = form.querySelector('.genre-tag-input');
      if (genreWidget) {
        const hidden = form.querySelector('input[name="genres"]');
        if (hidden) {
          hidden.value = Array.from(genreWidget.querySelectorAll('.genre-chip'))
            .map(function (c) { return c.dataset.value; })
            .join(', ');
        }
      }

      /* Include the clicked submit button's name=value (forms with several
         submit buttons, e.g. "Save" vs "Save & write to Discogs"). */
      const body = new FormData(form);
      const submitter = e.submitter;
      if (submitter && submitter.name && !body.has(submitter.name)) {
        body.append(submitter.name, submitter.value);
      }

      fetch(url, {
        method: 'POST',
        headers: { 'X-CSRF-Token': token },
        body: body,
      }).then(function (res) {
        if (res.redirected) {
          window.location.href = res.url;
        } else {
          res.text().then(function (html) {
            document.open();
            document.write(html);
            document.close();
          });
        }
      }).catch(function (err) {
        console.error('[MediaCat] form submit error:', err);
      });
    });
  });
}());

/* Discography reload — show loading state while Spotify enrichment runs.
 *
 * htmx:beforeRequest fires on the triggering element; evt.detail.target is
 * the swap target (.disco-wrap).  The entire .disco-wrap is replaced on
 * response (outerHTML swap), so no cleanup is needed after the request.
 */
document.addEventListener('htmx:beforeRequest', function (evt) {
  var target = evt.detail && evt.detail.target;
  if (!target || !target.classList.contains('disco-wrap')) return;
  target.classList.add('disco-loading');
  var btn = evt.detail.elt;
  if (btn && btn.tagName === 'BUTTON') {
    btn.innerHTML = '<span class="disco-spin">↻</span> Fetching…';
    btn.disabled = true;
  }
});
