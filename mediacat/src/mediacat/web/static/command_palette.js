'use strict';

(function () {
    var STORAGE_KEY = 'mc-search-history';
    var MAX_HISTORY = 8;

    // ── Command registries ────────────────────────────────────────

    // Roles that may add pressings (GET /catalogue/new answers 403 to viewers).
    var EDITORS = ['admin', 'reviewer', 'contributor'];

    // role: one role name or a list of them; no role = everyone.
    function allowed(c, role) {
        return !c.role || [].concat(c.role).indexOf(role) !== -1;
    }

    var STATIC_COMMANDS = [
        { label: 'Add pressing',     url: '/catalogue/new', role: EDITORS },
        { label: 'Browse catalogue', url: '/catalogue' },
        { label: 'Artists',          url: '/artists' },
        { label: 'Reviews',          url: '/reviews' },
        { label: 'Admin',            url: '/admin', role: 'admin' },
        { label: 'Help',             url: '/help' },
    ];

    // Slash commands: type /cmd [arg] for direct navigation or contextual search.
    // argUrl uses {arg} placeholder; null means the command takes no argument.
    var SLASH_COMMANDS = [
        { name: 'artist',    aliases: ['a'],         url: '/artists',       argUrl: '/search?q={arg}&scope=artists', argHint: 'artist name',   desc: 'Browse artists' },
        { name: 'album',     aliases: ['al'],        url: '/catalogue',     argUrl: '/search?q={arg}&scope=albums', argHint: 'album title',  desc: 'Search album titles' },
        { name: 'track',     aliases: ['t', 'song'], url: '/catalogue',     argUrl: '/search?q={arg}&scope=tracks', argHint: 'song title',   desc: 'Search song titles' },
        { name: 'catalogue', aliases: ['c', 'cat'],  url: '/catalogue',     argUrl: '/catalogue?q={arg}', argHint: 'album or title', desc: 'Browse catalogue' },
        { name: 'review',    aliases: ['r'],          url: '/reviews',       argUrl: '/search?q={arg}', argHint: 'search term',   desc: 'Browse reviews' },
        { name: 'add',       aliases: [],             url: '/catalogue/new', argUrl: null, argHint: null, desc: 'Add new pressing', role: EDITORS },
        { name: 'admin',     aliases: [],             url: '/admin',         argUrl: null, argHint: null, desc: 'Admin panel', role: 'admin' },
        { name: 'help',      aliases: ['h', '?'],     url: '/help',          argUrl: '/help?q={arg}', argHint: 'question', desc: 'Questions and answers' },
    ];

    // ── Helpers ───────────────────────────────────────────────────

    function getUserRole() {
        return document.body.dataset.userRole || '';
    }

    function allowedSlashCommands() {
        var role = getUserRole();
        return SLASH_COMMANDS.filter(function (c) { return allowed(c, role); });
    }

    function allowedStaticCommands() {
        var role = getUserRole();
        return STATIC_COMMANDS.filter(function (c) { return allowed(c, role); });
    }

    function historyCommands() {
        try {
            var items = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]');
            return items.slice(0, MAX_HISTORY).map(function (e) {
                return { label: '🕐 ' + e.term, url: '/search?q=' + encodeURIComponent(e.term) };
            });
        } catch (_) { return []; }
    }

    function escHtml(s) {
        return String(s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    // Find a slash command by exact name or alias (ES5: no Array.find)
    function findCmd(token) {
        var t = token.toLowerCase();
        var results = allowedSlashCommands().filter(function (c) {
            return c.name === t || c.aliases.indexOf(t) !== -1;
        });
        return results[0] || null;
    }

    // ── Filter logic ──────────────────────────────────────────────
    //
    // Returns { commands: [...], tabComplete: string|null }
    // tabComplete is the text Tab should insert (e.g. '/artist ') when set.

    function filterCommands(rawQuery) {
        var q = rawQuery;  // preserve case for arg

        // ── Slash command mode ──────────────────────────────────
        if (q.charAt(0) === '/') {
            var rest = q.slice(1);
            var spaceIdx = rest.indexOf(' ');

            if (spaceIdx !== -1) {
                // Argument mode: "/cmd arg" or "/cmd " (waiting for arg)
                var cmdToken = rest.slice(0, spaceIdx);
                var arg = rest.slice(spaceIdx + 1);
                var matched = findCmd(cmdToken);

                if (matched) {
                    if (arg.trim() && matched.argUrl) {
                        var navUrl = matched.argUrl.replace('{arg}', encodeURIComponent(arg.trim()));
                        return {
                            commands: [{
                                label: 'Search for “' + arg.trim() + '”',
                                url: navUrl,
                                badge: '/' + matched.name,
                            }],
                            tabComplete: null,
                        };
                    }
                    // Space typed but no arg yet — prompt
                    return {
                        commands: [{
                            label: matched.argHint
                                ? 'Type ' + matched.argHint + '…'
                                : '/' + matched.name + ' — ' + matched.desc,
                            url: matched.url,
                            badge: '/' + matched.name,
                            _prompt: true,
                        }],
                        tabComplete: null,
                    };
                }
                // Unknown command — fall through to normal mode
            } else {
                // Prefix mode: "/a", "/art", …
                var prefix = rest.toLowerCase();
                var matches = allowedSlashCommands().filter(function (c) {
                    return c.name.indexOf(prefix) === 0 ||
                           c.aliases.filter(function (a) { return a.indexOf(prefix) === 0; }).length > 0;
                });

                var tabComplete = null;
                if (matches.length === 1) {
                    tabComplete = '/' + matches[0].name + ' ';
                }

                return {
                    commands: matches.map(function (c) {
                        var argPart = c.argHint ? ' <' + c.argHint + '>' : '';
                        return {
                            label: '/' + c.name + argPart,
                            url: c.url,
                            badge: c.desc,
                            _cmd: c,
                        };
                    }),
                    tabComplete: tabComplete,
                };
            }
        }

        // ── Normal mode ─────────────────────────────────────────
        var lower = q.toLowerCase().trim();
        var all = allowedStaticCommands().concat(historyCommands());
        var filtered = lower
            ? all.filter(function (c) {
                return c.label.toLowerCase().indexOf(lower) !== -1 ||
                       c.url.toLowerCase().indexOf(lower) !== -1;
              })
            : all;
        return { commands: filtered, tabComplete: null };
    }

    // ── State ─────────────────────────────────────────────────────
    var overlay, input, list;
    var activeIndex  = -1;
    var _deleting    = false;   // suppress inline autocomplete on backspace/delete
    var _tabComplete = null;    // current tab-completion string

    // ── Build DOM ─────────────────────────────────────────────────
    function build() {
        overlay = document.createElement('div');
        overlay.id = 'cp-overlay';
        overlay.setAttribute('role', 'dialog');
        overlay.setAttribute('aria-modal', 'true');
        overlay.setAttribute('aria-label', 'Command palette');
        overlay.hidden = true;

        var panel = document.createElement('div');
        panel.className = 'cp-panel';

        input = document.createElement('input');
        input.type = 'text';
        input.id = 'cp-input';
        input.className = 'cp-input';
        input.placeholder = 'Go to… or type / for commands';
        input.setAttribute('autocomplete', 'off');
        input.setAttribute('spellcheck', 'false');

        list = document.createElement('div');
        list.id = 'cp-list';
        list.className = 'cp-list';
        list.setAttribute('role', 'listbox');

        panel.appendChild(input);
        panel.appendChild(list);
        overlay.appendChild(panel);
        document.body.appendChild(overlay);
    }

    // ── Render ────────────────────────────────────────────────────
    function renderList(query) {
        var result = filterCommands(query);
        _tabComplete = result.tabComplete;
        var cmds = result.commands;
        activeIndex = cmds.length ? 0 : -1;

        if (!cmds.length) {
            list.innerHTML = '<div class="cp-empty">No matches — try /artist, /catalogue…</div>';
            return;
        }

        list.innerHTML = cmds.map(function (c, i) {
            var cls = 'cp-item' +
                (i === 0 ? ' cp-item--active' : '') +
                (c._prompt ? ' cp-item--prompt' : '');
            return '<div class="' + cls + '" ' +
                'data-url="' + escHtml(c.url) + '" ' +
                'data-index="' + i + '" ' +
                'role="option" ' +
                'aria-selected="' + (i === 0 ? 'true' : 'false') + '">' +
                '<span class="cp-item-label">' + escHtml(c.label) + '</span>' +
                '<span class="cp-item-badge">' + escHtml(c.badge || c.url) + '</span>' +
            '</div>';
        }).join('');

        // Append tab-completion hint to first item if applicable
        if (_tabComplete) {
            var first = list.querySelector('.cp-item');
            if (first) {
                var hint = document.createElement('span');
                hint.className = 'cp-item-tab-hint';
                hint.textContent = 'Tab';
                first.appendChild(hint);
            }
        }

        list.querySelectorAll('.cp-item').forEach(function (el) {
            el.addEventListener('mouseenter', function () {
                setActive(parseInt(el.dataset.index, 10));
            });
            el.addEventListener('mousedown', function (ev) {
                ev.preventDefault();
                var cmd = result.commands[parseInt(el.dataset.index, 10)];
                if (cmd && cmd._cmd) {
                    // Slash command with no arg typed yet — expand into input
                    acceptCommand(cmd._cmd);
                } else {
                    navigate(el.dataset.url);
                }
            });
        });
    }

    function setActive(idx) {
        var items = list.querySelectorAll('.cp-item');
        if (idx < 0 || idx >= items.length) return;
        items.forEach(function (el, i) {
            el.classList.toggle('cp-item--active', i === idx);
            el.setAttribute('aria-selected', i === idx ? 'true' : 'false');
        });
        activeIndex = idx;
        items[idx].scrollIntoView({ block: 'nearest' });
    }

    // ── Inline autocomplete (single match in slash prefix mode) ───
    function tryInlineComplete(typedValue) {
        if (_deleting) return;
        if (typedValue.charAt(0) !== '/' || typedValue.indexOf(' ') !== -1) return;

        var prefix = typedValue.slice(1).toLowerCase();
        var matches = allowedSlashCommands().filter(function (c) {
            return c.name.indexOf(prefix) === 0;
        });
        if (matches.length !== 1) return;

        var full = '/' + matches[0].name;
        if (full.length <= typedValue.length) return;  // already complete

        input.value = full;
        input.setSelectionRange(typedValue.length, full.length);
    }

    // ── Accept a slash command (expand in input, ready for arg) ───
    function acceptCommand(cmd) {
        if (cmd.argHint) {
            input.value = '/' + cmd.name + ' ';
            input.setSelectionRange(input.value.length, input.value.length);
            renderList(input.value);
            input.focus();
        } else {
            navigate(cmd.url);
        }
    }

    // ── Open / close ──────────────────────────────────────────────
    function open() {
        overlay.hidden = false;
        input.value = '';
        _tabComplete = null;
        renderList('');
        input.focus();
    }

    function close() {
        overlay.hidden = true;
    }

    function navigate(url) {
        close();
        window.location.href = url;
    }

    // ── Wire events ───────────────────────────────────────────────
    document.addEventListener('DOMContentLoaded', function () {
        build();

        document.addEventListener('keydown', function (ev) {
            // Global toggle
            if ((ev.ctrlKey || ev.metaKey) && ev.key === 'k') {
                ev.preventDefault();
                overlay.hidden ? open() : close();
                return;
            }
            if (overlay.hidden) return;

            if (ev.key === 'Escape') {
                close();
                return;
            }

            if (ev.key === 'Tab') {
                ev.preventDefault();
                if (_tabComplete) {
                    input.value = _tabComplete;
                    input.setSelectionRange(input.value.length, input.value.length);
                    renderList(input.value);
                }
                return;
            }

            if (ev.key === 'ArrowDown') {
                ev.preventDefault();
                // Accept inline autocomplete before moving focus to list
                if (input.selectionStart !== input.selectionEnd) {
                    input.setSelectionRange(input.value.length, input.value.length);
                }
                var items = list.querySelectorAll('.cp-item');
                setActive(Math.min(activeIndex + 1, items.length - 1));
                return;
            }

            if (ev.key === 'ArrowUp') {
                ev.preventDefault();
                setActive(Math.max(activeIndex - 1, 0));
                return;
            }

            if (ev.key === 'Enter') {
                var active = list.querySelector('.cp-item--active');
                if (!active) return;
                var idx = parseInt(active.dataset.index, 10);
                var result = filterCommands(input.value);
                var cmd = result.commands[idx];
                if (cmd && cmd._cmd) {
                    acceptCommand(cmd._cmd);
                } else {
                    navigate(active.dataset.url);
                }
                return;
            }

            // Track deletion so inline autocomplete is suppressed for this keystroke
            if (ev.key === 'Backspace' || ev.key === 'Delete') {
                _deleting = true;
            }
        });

        input.addEventListener('input', function () {
            var val = input.value;
            tryInlineComplete(val);
            renderList(input.value);
            _deleting = false;
        });

        overlay.addEventListener('mousedown', function (ev) {
            if (ev.target === overlay) close();
        });

        var hint = document.getElementById('cp-hint');
        if (hint) {
            hint.addEventListener('click', function (ev) {
                ev.preventDefault();
                open();
            });
        }
    });
}());
