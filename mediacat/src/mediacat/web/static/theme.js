/* MediaCat theme toggle — dark (default) / light.
   Runs before first paint to avoid flash of wrong theme. */
(function () {
    var saved = localStorage.getItem('mc-theme');
    var theme = saved === 'light' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', theme);

    function toggle() {
        var current = document.documentElement.getAttribute('data-theme');
        var next = current === 'light' ? 'dark' : 'light';
        document.documentElement.setAttribute('data-theme', next);
        localStorage.setItem('mc-theme', next);
    }

    document.addEventListener('DOMContentLoaded', function () {
        var btn = document.getElementById('theme-toggle');
        if (btn) btn.addEventListener('click', toggle);

        // Navbar scroll transparency effect
        var navbar = document.getElementById('navbar');
        if (navbar) {
            function updateNavbar() {
                if (window.scrollY > 32) {
                    navbar.classList.add('navbar--scrolled');
                } else {
                    navbar.classList.remove('navbar--scrolled');
                }
            }
            updateNavbar();
            window.addEventListener('scroll', updateNavbar, { passive: true });
        }
    });
})();
