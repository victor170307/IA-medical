/* ==========================================================================
   Référentiel Médicaments BDPM — Lot 2A : Interactions côté navigateur (app.js)
   - Bascule thème clair / sombre mémorisée localement (zéro tracking recherche)
   - Copie rapide des codes CIS, CIP13 et du lien canonique de la fiche
   - Validation stricte des chemins internes de retour (anti open-redirect)
   - Sommaire sticky synchronisé au défilement + navigation mobile
   - Impression / export PDF propre (ouverture automatique des blocs dépliables)
   - Recherche en direct avec gestion visible des erreurs réseau
   ========================================================================== */

(function () {
    "use strict";

    var THEME_STORAGE_KEY = "bdpm_theme_preference";
    var toastTimer = null;

    /* ----------------------------------------------------------------------
       1. Utilitaires d'échappement HTML et de notification accessible (Toast)
       ---------------------------------------------------------------------- */
    function escapeHtml(value) {
        if (value === null || value === undefined) return "";
        return String(value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function showToast(message) {
        var toast = document.getElementById("ui-toast");
        if (!toast) return;
        toast.textContent = message;
        toast.classList.add("is-visible");
        if (toastTimer) clearTimeout(toastTimer);
        toastTimer = setTimeout(function () {
            toast.classList.remove("is-visible");
        }, 2400);
    }

    /* ----------------------------------------------------------------------
       2. Validation stricte d'une adresse interne de retour
          (Ne se contente JAMAIS d'un simple startsWith("/") afin de bloquer
          les redirections ouvertes de type "//domaine-externe" ou "/\...")
       ---------------------------------------------------------------------- */
    function sanitizeInternalReturnPath(rawCandidate) {
        if (!rawCandidate || typeof rawCandidate !== "string") return null;
        var trimmed = rawCandidate.trim();
        if (!trimmed || trimmed.length > 400) return null;

        // Rejet immédiat des caractères de contrôle et des préfixes ambigus
        if (/[\r\n\t\0]/.test(trimmed)) return null;
        if (trimmed.indexOf("//") === 0 || trimmed.indexOf("/\\") === 0) return null;
        if (!trimmed.startsWith("/")) return null;

        try {
            var parsed = new URL(trimmed, window.location.origin);
            if (parsed.origin !== window.location.origin) return null;
            if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;

            // Liste blanche stricte des routes internes autorisées pour un retour
            var isHome = parsed.pathname === "/";
            var isMedDetail = /^\/medicaments\/\d{8}$/.test(parsed.pathname);
            if (!isHome && !isMedDetail) return null;

            if (isHome) {
                var cleanParams = new URLSearchParams();
                var q = parsed.searchParams.get("q");
                var fuzzy = parsed.searchParams.get("fuzzy");
                if (q && q.trim()) {
                    cleanParams.set("q", q.trim().slice(0, 200));
                }
                if (fuzzy === "true" || fuzzy === "false") {
                    cleanParams.set("fuzzy", fuzzy);
                }
                var qs = cleanParams.toString();
                return qs ? "/?" + qs : "/";
            }

            return parsed.pathname;
        } catch (err) {
            return null;
        }
    }

    function buildCurrentSearchReturnPath(queryValue, fuzzyChecked) {
        var q = (queryValue || "").trim();
        if (!q) return "/";
        var params = new URLSearchParams();
        params.set("q", q);
        params.set("fuzzy", fuzzyChecked ? "true" : "false");
        return "/?" + params.toString();
    }

    function buildDetailHref(codeCis, returnPath) {
        var base = "/medicaments/" + encodeURIComponent(codeCis);
        var safeReturn = sanitizeInternalReturnPath(returnPath);
        if (safeReturn && safeReturn !== "/") {
            return base + "?return_to=" + encodeURIComponent(safeReturn);
        }
        return base;
    }

    /* ----------------------------------------------------------------------
       3. Mode Clair / Mode Sombre (sans collecte de données personnelles)
       ---------------------------------------------------------------------- */
    function applyTheme(theme, persist) {
        var normalized = theme === "dark" ? "dark" : "light";
        document.documentElement.setAttribute("data-theme", normalized);
        document.documentElement.style.colorScheme = normalized;

        var toggleBtn = document.getElementById("theme-toggle");
        if (toggleBtn) {
            var isDark = normalized === "dark";
            toggleBtn.setAttribute("aria-pressed", isDark ? "true" : "false");
            var labelSpan = toggleBtn.querySelector(".theme-toggle-label");
            if (labelSpan) {
                labelSpan.textContent = isDark ? "Mode clair" : "Mode sombre";
            }
        }

        if (persist) {
            try {
                localStorage.setItem(THEME_STORAGE_KEY, normalized);
            } catch (err) {
                /* Stockage local désactivé : le thème reste actif pour la page courante */
            }
        }
    }

    function initThemeToggle() {
        var current = document.documentElement.getAttribute("data-theme") || "light";
        applyTheme(current, false);

        var toggleBtn = document.getElementById("theme-toggle");
        if (!toggleBtn) return;

        toggleBtn.addEventListener("click", function () {
            var active = document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
            var next = active === "dark" ? "light" : "dark";
            applyTheme(next, true);
        });
    }

    /* ----------------------------------------------------------------------
       4. Copie rapide (codes CIS, codes CIP13 et lien de la fiche)
       ---------------------------------------------------------------------- */
    function copyTextToClipboard(text) {
        if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
            return navigator.clipboard.writeText(text);
        }
        return new Promise(function (resolve, reject) {
            try {
                var area = document.createElement("textarea");
                area.value = text;
                area.setAttribute("readonly", "");
                area.style.position = "fixed";
                area.style.opacity = "0";
                document.body.appendChild(area);
                area.select();
                var ok = document.execCommand("copy");
                document.body.removeChild(area);
                if (ok) resolve();
                else reject(new Error("Copie impossible"));
            } catch (e) {
                reject(e);
            }
        });
    }

    function markButtonCopied(btn) {
        var originalText = btn.getAttribute("data-original-label") || btn.textContent;
        if (!btn.getAttribute("data-original-label")) {
            btn.setAttribute("data-original-label", originalText);
        }
        btn.classList.add("is-copied");
        btn.textContent = "Copié ✓";
        setTimeout(function () {
            btn.classList.remove("is-copied");
            btn.textContent = originalText;
        }, 1800);
    }

    function initCopyInteractions() {
        document.addEventListener("click", function (event) {
            var copyBtn = event.target.closest("[data-copy-value]");
            if (copyBtn) {
                event.preventDefault();
                var value = copyBtn.getAttribute("data-copy-value") || "";
                var kind = copyBtn.getAttribute("data-copy-label") || "Code";
                if (!value) return;

                copyTextToClipboard(value)
                    .then(function () {
                        markButtonCopied(copyBtn);
                        showToast(kind + " « " + value + " » copié dans le presse-papiers.");
                    })
                    .catch(function () {
                        showToast("Impossible de copier automatiquement sur ce navigateur.");
                    });
                return;
            }

            var shareBtn = event.target.closest("#copy-page-link");
            if (shareBtn) {
                event.preventDefault();
                var canonicalPath = shareBtn.getAttribute("data-canonical-path") || window.location.pathname;
                var safePath = sanitizeInternalReturnPath(canonicalPath) || window.location.pathname;
                var fullUrl = window.location.origin + safePath;

                copyTextToClipboard(fullUrl)
                    .then(function () {
                        markButtonCopied(shareBtn);
                        showToast("Lien de la fiche copié dans le presse-papiers.");
                    })
                    .catch(function () {
                        showToast("Impossible de copier le lien automatiquement.");
                    });
            }
        });
    }

    /* ----------------------------------------------------------------------
       5. Retour intelligent aux résultats avec conservation du contexte
       ---------------------------------------------------------------------- */
    function initSmartBackLink() {
        var backLink = document.getElementById("smart-back-link");
        if (!backLink) return;

        var urlParams = new URLSearchParams(window.location.search);
        var rawReturn = urlParams.get("return_to");
        var safeTarget = sanitizeInternalReturnPath(rawReturn);

        // Repli sur document.referrer uniquement s'il provient de la même origine et de la page de recherche
        if (!safeTarget && document.referrer) {
            try {
                var refUrl = new URL(document.referrer);
                if (refUrl.origin === window.location.origin && refUrl.pathname === "/") {
                    safeTarget = sanitizeInternalReturnPath(refUrl.pathname + refUrl.search);
                }
            } catch (err) {
                safeTarget = null;
            }
        }

        if (safeTarget) {
            backLink.setAttribute("href", safeTarget);
            try {
                var parsedTarget = new URL(safeTarget, window.location.origin);
                var rememberedQuery = parsedTarget.searchParams.get("q");
                var labelNode = backLink.querySelector(".back-link-text");
                if (rememberedQuery && labelNode) {
                    labelNode.textContent = "Retour aux résultats (« " + rememberedQuery + " »)";
                }
            } catch (err) {
                /* conserve le libellé par défaut */
            }
        } else {
            backLink.setAttribute("href", "/");
        }
    }

    /* ----------------------------------------------------------------------
       6. Impression / Enregistrement PDF propre
       ---------------------------------------------------------------------- */
    function initPrintFeature() {
        var printBtn = document.getElementById("print-sheet-btn");
        var closedDetailsBeforePrint = [];

        window.addEventListener("beforeprint", function () {
            closedDetailsBeforePrint = [];
            document.querySelectorAll(".detail-main details:not([open])").forEach(function (el) {
                closedDetailsBeforePrint.push(el);
                el.setAttribute("open", "");
            });
        });

        window.addEventListener("afterprint", function () {
            closedDetailsBeforePrint.forEach(function (el) {
                el.removeAttribute("open");
            });
            closedDetailsBeforePrint = [];
        });

        if (printBtn) {
            printBtn.addEventListener("click", function () {
                window.print();
            });
        }
    }

    /* ----------------------------------------------------------------------
       7. Sommaire latéral sticky & barre de sections mobile synchronisés
       ---------------------------------------------------------------------- */
    function initSectionNavigation() {
        var navLinks = Array.prototype.slice.call(
            document.querySelectorAll(".sticky-toc a[href^='#'], .mobile-section-nav a[href^='#']")
        );
        if (!navLinks.length || typeof IntersectionObserver === "undefined") return;

        var sectionIds = [];
        navLinks.forEach(function (link) {
            var id = (link.getAttribute("href") || "").slice(1);
            if (id && sectionIds.indexOf(id) === -1) {
                sectionIds.push(id);
            }
        });

        var sections = sectionIds
            .map(function (id) { return document.getElementById(id); })
            .filter(Boolean);

        if (!sections.length) return;

        function setActiveSection(activeId) {
            navLinks.forEach(function (link) {
                var matches = link.getAttribute("href") === "#" + activeId;
                link.classList.toggle("is-active", matches);
                if (matches) {
                    link.setAttribute("aria-current", "location");
                } else {
                    link.removeAttribute("aria-current");
                }
            });
        }

        var observer = new IntersectionObserver(
            function (entries) {
                var visible = entries
                    .filter(function (e) { return e.isIntersecting; })
                    .sort(function (a, b) { return a.boundingClientRect.top - b.boundingClientRect.top; });
                if (visible.length > 0) {
                    setActiveSection(visible[0].target.id);
                }
            },
            { rootMargin: "-95px 0px -65% 0px", threshold: 0.05 }
        );

        sections.forEach(function (sec) {
            observer.observe(sec);
        });
        setActiveSection(sections[0].id);
    }

    /* ----------------------------------------------------------------------
       8. Recherche dynamique avec cartes Modern Medical & gestion réseau
       ---------------------------------------------------------------------- */
    function renderMedicationCard(item, returnPath) {
        var detailHref = buildDetailHref(item.code_cis, returnPath);

        var commBadge = item.etat_commercialisation === "Commercialisée"
            ? '<span class="badge badge-active">Commercialisée</span>'
            : '<span class="badge badge-neutral">' + escapeHtml(item.etat_commercialisation || "Statut non précisé") + "</span>";

        var survBadge = item.surveillance_renforcee === "Oui"
            ? '<span class="badge badge-vigilance">▼ Surveillance renforcée</span>'
            : "";

        var approxBadge = item.match_type === "approximative_orthographique"
            ? '<span class="badge badge-vigilance">Correspondance approchée</span>'
            : "";

        var alertBadge = item.important_info_count > 0
            ? '<span class="badge badge-alert">' + escapeHtml(item.important_info_count) + " info(s) sécurité ANSM</span>"
            : "";

        var metaParts = [
            '<span class="meta-item"><strong>CIS :</strong> <code class="mono">' + escapeHtml(item.code_cis) + '</code> ' +
            '<button type="button" class="copy-btn" data-copy-value="' + escapeHtml(item.code_cis) + '" data-copy-label="Code CIS" aria-label="Copier le code CIS ' + escapeHtml(item.code_cis) + '">Copier</button></span>'
        ];
        if (item.forme_pharmaceutique) {
            metaParts.push('<span class="meta-item"><strong>Forme :</strong> ' + escapeHtml(item.forme_pharmaceutique) + "</span>");
        }
        if (item.voies_administration) {
            metaParts.push('<span class="meta-item"><strong>Voie(s) :</strong> ' + escapeHtml(item.voies_administration) + "</span>");
        }
        if (item.titulaires) {
            metaParts.push('<span class="meta-item"><strong>Titulaire :</strong> ' + escapeHtml(item.titulaires) + "</span>");
        }

        var genericBlock = "";
        if (item.groupes_generiques && item.groupes_generiques.length > 0) {
            var genLines = item.groupes_generiques.map(function (g) {
                return (
                    '<div class="generic-line">' +
                    "<span>" + escapeHtml(g.libelle_groupe) + "</span>" +
                    '<span class="badge badge-generic">' + escapeHtml(g.type_generique_libelle) + "</span>" +
                    "</div>"
                );
            }).join("");
            genericBlock =
                '<div class="med-generic-group">' +
                '<strong>Groupe(s) générique(s) (<code>CIS_GENER_bdpm.txt</code>) :</strong>' +
                genLines +
                "</div>";
        }

        var compoBlock = "";
        if (item.compositions && item.compositions.length > 0) {
            var lis = item.compositions.map(function (c) {
                var dosageText = c.dosage
                    ? '— <strong class="mono">' + escapeHtml(c.dosage) + "</strong>"
                    : "— <em>dosage non renseigné dans la source</em>";
                var refText = c.reference_dosage
                    ? " (pour " + escapeHtml(c.reference_dosage) + ")"
                    : "";
                return '<li><span class="subst-name">' + escapeHtml(c.substance) + "</span> " + dosageText + refText + "</li>";
            }).join("");
            compoBlock =
                '<div class="med-substances">' +
                '<strong>Substance(s) et dosage(s) déclarés (<code>CIS_COMPO_bdpm.txt</code>) :</strong>' +
                "<ul>" + lis + "</ul>" +
                "</div>";
        } else {
            compoBlock =
                '<p class="missing-data">' +
                '<em>Aucune substance active renseignée dans <code>CIS_COMPO_bdpm.txt</code> pour ce code CIS.</em>' +
                "</p>";
        }

        return (
            '<article class="med-card">' +
            '<div class="med-card-top">' +
            '<div class="med-card-heading">' +
            '<h3 class="med-title"><a href="' + escapeHtml(detailHref) + '">' + escapeHtml(item.denomination) + "</a></h3>" +
            '<p class="med-meta">' + metaParts.join("") + "</p>" +
            "</div>" +
            '<div class="med-badges">' + commBadge + survBadge + approxBadge + alertBadge + "</div>" +
            "</div>" +
            genericBlock +
            compoBlock +
            '<div class="med-card-actions">' +
            '<a href="' + escapeHtml(detailHref) + '" class="btn-secondary">Consulter la fiche complète →</a>' +
            '<a href="' + escapeHtml(item.official_fiche_url) + '" target="_blank" rel="noopener noreferrer" class="link-external">Notice &amp; RCP officiels (BDPM) ↗</a>' +
            "</div>" +
            "</article>"
        );
    }

    function renderSearchPayload(payload, returnPath) {
        var html = "";
        if (payload.status === "fuzzy_match") {
            html +=
                '<section class="alert-box alert-warning" role="alert">' +
                "<h2>Attention — Résultats proposés par approximation orthographique</h2>" +
                "<p>" + escapeHtml(payload.warning) + "</p>" +
                "</section>";
        } else if (payload.status === "no_results") {
            html +=
                '<section class="alert-box alert-error" role="status">' +
                "<h2>Aucun résultat trouvé</h2>" +
                "<p>" + escapeHtml(payload.warning) + "</p>" +
                "</section>";
        }

        if (payload.results && payload.results.length > 0) {
            var plural = payload.count > 1 ? "s" : "";
            var cards = payload.results.map(function (item) {
                return renderMedicationCard(item, returnPath);
            }).join("");
            html +=
                '<section class="results-section">' +
                '<div class="results-header">' +
                '<h2><span class="mono">' + escapeHtml(payload.count) + "</span> résultat" + plural + " pour « " + escapeHtml(payload.query) + " »</h2>" +
                '<p class="provenance-tag">Sources directes : <code>CIS_bdpm.txt</code>, <code>CIS_COMPO_bdpm.txt</code> et <code>CIS_GENER_bdpm.txt</code></p>' +
                "</div>" +
                '<div class="results-list">' + cards + "</div>" +
                "</section>";
        }
        return html;
    }

    function initLiveSearch() {
        var searchInput = document.getElementById("search-input");
        var clearBtn = document.getElementById("search-clear");
        var fuzzyCheckbox = document.getElementById("fuzzy-checkbox");
        var container = document.getElementById("dynamic-results-container");
        var statusIndicator = document.getElementById("live-status-indicator");
        var errorBanner = document.getElementById("network-error-banner");
        var retryBtn = document.getElementById("network-retry-btn");

        if (!searchInput || !container || !fuzzyCheckbox) return;

        var initialHtml = container.innerHTML;
        var debounceTimer = null;
        var activeController = null;

        function syncClearButton() {
            if (!clearBtn) return;
            clearBtn.hidden = searchInput.value.length === 0;
        }

        function hideNetworkError() {
            if (errorBanner) errorBanner.hidden = true;
        }

        function showNetworkError() {
            if (errorBanner) errorBanner.hidden = false;
        }

        async function performLiveSearch() {
            var q = searchInput.value.trim();
            var fuzzy = fuzzyCheckbox.checked;
            syncClearButton();
            hideNetworkError();

            // Mise à jour propre de l'URL courante (sans enregistrer d'historique persistant)
            var currentReturnPath = buildCurrentSearchReturnPath(q, fuzzy);
            window.history.replaceState({}, "", currentReturnPath);

            if (!q) {
                if (activeController) activeController.abort();
                if (statusIndicator) statusIndicator.textContent = "";
                container.innerHTML = initialHtml;
                return;
            }

            if (q.length < 2 && !/^\d+$/.test(q)) {
                if (statusIndicator) {
                    statusIndicator.textContent = "Saisissez au moins 2 caractères (ou un chiffre) pour filtrer…";
                }
                return;
            }

            if (activeController) activeController.abort();
            activeController = new AbortController();
            if (statusIndicator) statusIndicator.textContent = "Recherche en cours…";

            try {
                var apiUrl = "/api/search?q=" + encodeURIComponent(q) + "&fuzzy=" + (fuzzy ? "true" : "false") + "&limit=30";
                var resp = await fetch(apiUrl, { signal: activeController.signal });
                if (!resp.ok) {
                    throw new Error("HTTP " + resp.status);
                }
                var payload = await resp.json();
                container.innerHTML = renderSearchPayload(payload, currentReturnPath);
                if (statusIndicator) {
                    statusIndicator.textContent = payload.count + " résultat(s) affiché(s)";
                }
            } catch (err) {
                if (err && err.name === "AbortError") return;
                if (statusIndicator) {
                    statusIndicator.textContent = "Erreur de connexion au serveur local";
                }
                showNetworkError();
            }
        }

        searchInput.addEventListener("input", function () {
            syncClearButton();
            clearTimeout(debounceTimer);
            debounceTimer = setTimeout(performLiveSearch, 180);
        });

        fuzzyCheckbox.addEventListener("change", function () {
            if (searchInput.value.trim()) {
                performLiveSearch();
            }
        });

        if (clearBtn) {
            clearBtn.addEventListener("click", function () {
                searchInput.value = "";
                syncClearButton();
                searchInput.focus();
                performLiveSearch();
            });
        }

        if (retryBtn) {
            retryBtn.addEventListener("click", function () {
                performLiveSearch();
            });
        }

        document.querySelectorAll(".chip-btn[data-query]").forEach(function (btn) {
            btn.addEventListener("click", function () {
                searchInput.value = btn.getAttribute("data-query") || "";
                syncClearButton();
                searchInput.focus();
                performLiveSearch();
            });
        });

        syncClearButton();
    }

    /* ----------------------------------------------------------------------
       Initialisation au chargement du DOM
       ---------------------------------------------------------------------- */
    document.addEventListener("DOMContentLoaded", function () {
        initThemeToggle();
        initCopyInteractions();
        initSmartBackLink();
        initPrintFeature();
        initSectionNavigation();
        initLiveSearch();
    });
})();
