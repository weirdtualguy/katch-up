
    (function () {
        var chips = Array.prototype.slice.call(document.querySelectorAll('.chip'));
        var cards = Array.prototype.slice.call(document.querySelectorAll('#discussionGrid .card'));
        var searchInput = document.getElementById('searchInput');
        var resultCount = document.getElementById('resultCount');
        var emptyState = document.getElementById('emptyState');
        var clearFiltersBtn = document.getElementById('clearFilters');
        var discussionGrid = document.getElementById('discussionGrid');
        var activeCategory = 'all';

        function normalize(s) { return (s || '').toLowerCase(); }

        function applyFilters() {
            var query = normalize(searchInput ? searchInput.value : '');
            var visibleCount = 0;

            cards.forEach(function (card) {
                var category = card.getAttribute('data-category') || '';
                var matchesCategory = activeCategory === 'all' || category === activeCategory;
                var matchesSearch = query === '' || normalize(card.textContent).indexOf(query) !== -1;
                var visible = matchesCategory && matchesSearch;
                card.hidden = !visible;
                if (visible) visibleCount++;
            });

            if (resultCount) {
                resultCount.textContent = visibleCount + (visibleCount === 1 ? ' discussion' : ' discussions');
            }
            if (emptyState) emptyState.hidden = visibleCount !== 0;
            if (discussionGrid) discussionGrid.hidden = visibleCount === 0;
        }

        chips.forEach(function (chip) {
            chip.addEventListener('click', function () {
                chips.forEach(function (c) {
                    c.classList.remove('active');
                    c.setAttribute('aria-pressed', 'false');
                });
                chip.classList.add('active');
                chip.setAttribute('aria-pressed', 'true');
                activeCategory = chip.getAttribute('data-category');
                applyFilters();
            });
        });

        if (searchInput) searchInput.addEventListener('input', applyFilters);

        if (clearFiltersBtn) {
            clearFiltersBtn.addEventListener('click', function () {
                activeCategory = 'all';
                if (searchInput) searchInput.value = '';
                chips.forEach(function (c) {
                    var isAll = c.getAttribute('data-category') === 'all';
                    c.classList.toggle('active', isAll);
                    c.setAttribute('aria-pressed', isAll ? 'true' : 'false');
                });
                applyFilters();
            });
        }

        // Header search: typing still does the instant same-page quick
        // filter above (when a #discussionGrid exists on this page); Enter
        // always jumps to the full archive-wide search page. KATCHUP_PREFIX
        // is set by a tiny inline script on every page (see page_shell) so
        // this one shared file works at any directory depth.
        if (searchInput) {
            searchInput.addEventListener('keydown', function (e) {
                if (e.key !== 'Enter') return;
                var q = searchInput.value.trim();
                var prefix = window.KATCHUP_PREFIX || '';
                window.location.href = prefix + 'search/' + (q ? '?q=' + encodeURIComponent(q) : '');
            });
        }
    })();

    // ---------------------------------------------------------------------
    // Search results page. No-ops entirely if #searchResults isn't on the
    // page, so this is safe to ship in the one shared JS file loaded
    // everywhere rather than a second per-page script.
    // ---------------------------------------------------------------------
    (function () {
        var resultsEl = document.getElementById('searchResults');
        if (!resultsEl) return;

        var indexScript = document.getElementById('searchIndexData');
        var items = [];
        try { items = indexScript ? JSON.parse(indexScript.textContent) : []; } catch (e) { items = []; }

        var input = document.getElementById('searchPageInput');
        var countEl = document.getElementById('searchResultsCount');
        var emptyEl = document.getElementById('searchEmptyState');
        var browseLink = document.getElementById('searchBrowseAll');

        function normalize(s) { return (s || '').toLowerCase(); }

        function escapeHtml(s) {
            return (s || '').replace(/[&<>"']/g, function (c) {
                return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
            });
        }

        // Deterministic, explainable ranking: a 7-slot tuple compared most-
        // to-least significant (title exact match, title word hits, body
        // exact match, body word hits, category match, recency, importance
        // score). Title always outranks body; exact phrase always outranks
        // scattered word hits within the same field; recency and the
        // existing importance score only ever break ties among results
        // that are already textually comparable -- popularity alone can
        // never push an unrelated story above a real textual match.
        function scoreItem(item, queryLower, queryWords) {
            var titleLower = normalize(item.t);
            var bodyLower = normalize(item.s);
            var catLower = normalize(item.c);

            var titleExact = titleLower.indexOf(queryLower) !== -1 ? 1 : 0;
            var titleWordHits = queryWords.filter(function (w) { return titleLower.indexOf(w) !== -1; }).length;
            var bodyExact = bodyLower.indexOf(queryLower) !== -1 ? 1 : 0;
            var bodyWordHits = queryWords.filter(function (w) { return bodyLower.indexOf(w) !== -1; }).length;
            var categoryMatch = (catLower === queryLower || catLower.indexOf(queryLower) !== -1) ? 1 : 0;

            return {
                item: item,
                relevant: titleExact || titleWordHits || bodyExact || bodyWordHits || categoryMatch,
                tuple: [titleExact, titleWordHits, bodyExact, bodyWordHits, categoryMatch, item.r || 0, item.imp || 0]
            };
        }

        function compareTuples(a, b) {
            for (var i = 0; i < a.length; i++) {
                if (b[i] !== a[i]) return b[i] - a[i];
            }
            return 0;
        }

        function renderResults(query) {
            var queryLower = normalize(query.trim());
            if (!queryLower) {
                resultsEl.innerHTML = '';
                resultsEl.hidden = true;
                if (countEl) countEl.textContent = '';
                if (emptyEl) emptyEl.hidden = true;
                return;
            }

            var queryWords = queryLower.split(/\s+/).filter(Boolean);
            var scored = items.map(function (item) { return scoreItem(item, queryLower, queryWords); })
                .filter(function (r) { return r.relevant; })
                .sort(function (a, b) { return compareTuples(a.tuple, b.tuple); });

            resultsEl.hidden = false;
            if (countEl) {
                countEl.textContent = scored.length + (scored.length === 1 ? ' result' : ' results') + ' for "' + query.trim() + '"';
            }

            if (scored.length === 0) {
                resultsEl.innerHTML = '';
                if (emptyEl) {
                    emptyEl.hidden = false;
                    var q = emptyEl.querySelector('[data-query]');
                    if (q) q.textContent = query.trim();
                }
                return;
            }
            if (emptyEl) emptyEl.hidden = true;

            resultsEl.innerHTML = scored.map(function (r) {
                var item = r.item;
                return '<a class="card-link search-result-item" href="' + item.u + '">' +
                    '<article class="card">' +
                    '<div class="card-meta"><span class="pill pill-category-inline">' + escapeHtml(item.c) + '</span></div>' +
                    '<h3 class="card-title">' + escapeHtml(item.t) + '</h3>' +
                    '<p class="summary">' + escapeHtml(item.s) + '</p>' +
                    '<div class="card-footer"><span class="card-meta-item">' + escapeHtml(item.d) + '</span></div>' +
                    '</article></a>';
            }).join('');
        }

        function debounce(fn, wait) {
            var t;
            return function () {
                var args = arguments;
                clearTimeout(t);
                t = setTimeout(function () { fn.apply(null, args); }, wait);
            };
        }

        var debouncedRender = debounce(function () { renderResults(input.value); }, 120);

        if (input) {
            input.addEventListener('input', debouncedRender);
            var params = new URLSearchParams(window.location.search);
            var initialQuery = params.get('q') || '';
            if (initialQuery) {
                input.value = initialQuery;
                renderResults(initialQuery);
            }
            input.focus();
        }
        if (browseLink) {
            browseLink.addEventListener('click', function () {
                if (input) { input.value = ''; }
                renderResults('');
            });
        }
    })();
