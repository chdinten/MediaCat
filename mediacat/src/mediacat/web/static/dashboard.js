(function () {
    var overlay = document.getElementById('dash-fade-overlay');
    if (!overlay) return;

    // In the 3panel layout sp-center scrolls independently; use that as the
    // scroll source. In the classic layout the window scrolls.
    var scrollEl = document.getElementById('sp-center') || window;

    function getScrollY() {
        return scrollEl === window ? window.scrollY : scrollEl.scrollTop;
    }

    function updateFade() {
        var scrollY = getScrollY();
        // Use the scroll container's visible height as "one screen"
        var vh = scrollEl === window ? window.innerHeight : scrollEl.clientHeight;
        // Start fading at 1 screen height, fully hidden at 1.6 screens
        var fadeStart = vh;
        var fadeEnd = vh * 1.6;
        overlay.style.opacity = scrollY <= fadeStart ? 0
            : scrollY >= fadeEnd ? 1
            : (scrollY - fadeStart) / (fadeEnd - fadeStart);
    }

    scrollEl.addEventListener('scroll', updateFade, { passive: true });
    updateFade();
}());
