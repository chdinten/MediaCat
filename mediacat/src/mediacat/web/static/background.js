/* background.js — looping background video (partials/page_background.html and
 * the admin preview).
 *
 * The video pauses while the tab is hidden, so it costs no CPU in the
 * background, and stays paused (CSS hides it, the still image shows) when the
 * system asks for reduced motion.  If the browser refuses to autoplay, the
 * poster — the same still image — simply stays.
 */
(function () {
  'use strict';

  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)');

  function videos() {
    return Array.prototype.slice.call(document.querySelectorAll('video.js-bg-video'));
  }

  function update() {
    var run = !document.hidden && !reduce.matches;
    videos().forEach(function (v) {
      if (run) {
        var p = v.play();
        if (p && p.catch) p.catch(function () { /* autoplay refused: poster stays */ });
      } else {
        v.pause();
      }
    });
  }

  document.addEventListener('visibilitychange', update);
  if (reduce.addEventListener) reduce.addEventListener('change', update);
  document.addEventListener('DOMContentLoaded', update);
  if (document.readyState !== 'loading') update();
}());
