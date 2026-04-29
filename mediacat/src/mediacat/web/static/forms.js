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
      const url = form.action || window.location.href;

      fetch(url, {
        method: 'POST',
        headers: { 'X-CSRF-Token': token },
        body: new FormData(form),
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
