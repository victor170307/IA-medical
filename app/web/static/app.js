/* ==========================================================================
   Référentiel Médicaments BDPM — Lot 2A + Lot 2B : Interactions navigateur
   - Bascule thème clair / sombre mémorisée localement (zéro tracking recherche)
   - Copie rapide des codes CIS, CIP13, codes substance SA et lien de fiche
   - Validation stricte des chemins internes de retour (anti open-redirect)
   - Sommaire sticky synchronisé au défilement + navigation mobile
   - Impression / export PDF propre (ouverture automatique des blocs dépliables)
   - Recherche dynamique combinée aux filtres, pagination réelle et gestion réseau
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
    function sanitizePositiveIntString(raw, minVal, maxVal) {
        if (!raw || !/^\d+$/.test(raw)) return null;
        var num = parseInt(raw, 10);
        if (isNaN(num) || num < minVal || num > maxVal) return null;
        return String(num);
    }

    function sanitizeInternalReturnPath(rawCandidate) {
        if (!rawCandidate || typeof rawCandidate !== "string") return null;
        var trimmed = rawCandidate.trim();
        if (!trimmed || trimmed.length > 600) return null;

        // Rejet immédiat des caractères de contrôle et des préfixes ambigus
        if (/[\r\n\t\0]/.test(trimmed)) return null;
        if (trimmed.indexOf("//") === 0 || trimmed.indexOf("/\\") === 0) return null;
        if (!trimmed.startsWith("/")) return null;

        try {
            var parsed = new URL(trimmed, window.location.origin);
            if (parsed.origin !== window.location.origin) return null;
            if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;

            var p = parsed.pathname;
            var cleanParams = new URLSearchParams();

            // Liste blanche stricte des routes internes autorisées et de leurs paramètres
            if (p === "/") {
                var q = parsed.searchParams.get("q");
                var fuzzy = parsed.searchParams.get("fuzzy");
                var page = sanitizePositiveIntString(parsed.searchParams.get("page"), 1, 10000);
                var perPage = sanitizePositiveIntString(parsed.searchParams.get("per_page"), 5, 100);
                var comm = parsed.searchParams.get("commercialise_only");
                var forme = parsed.searchParams.get("forme");
                var genericRole = parsed.searchParams.get("generic_role");
                var presc = parsed.searchParams.get("prescription_only");
                var ansm = parsed.searchParams.get("with_ansm_info");

                if (q && q.trim()) cleanParams.set("q", q.trim().slice(0, 200));
                if (fuzzy === "true" || fuzzy === "false") cleanParams.set("fuzzy", fuzzy);
                if (page && page !== "1") cleanParams.set("page", page);
                if (perPage && perPage !== "20") cleanParams.set("per_page", perPage);
                if (comm === "true") cleanParams.set("commercialise_only", "true");
                if (forme && forme.trim()) cleanParams.set("forme", forme.trim().slice(0, 120));
                if (genericRole === "in_group" || genericRole === "generic" || genericRole === "princeps") {
                    cleanParams.set("generic_role", genericRole);
                }
                if (presc === "true") cleanParams.set("prescription_only", "true");
                if (ansm === "true") cleanParams.set("with_ansm_info", "true");

                var homeQs = cleanParams.toString();
                return homeQs ? "/?" + homeQs : "/";
            }

            if (p === "/catalogue") {
                var lettre = parsed.searchParams.get("lettre");
                var catQ = parsed.searchParams.get("q");
                var catPage = sanitizePositiveIntString(parsed.searchParams.get("page"), 1, 10000);
                var catPerPage = sanitizePositiveIntString(parsed.searchParams.get("per_page"), 1, 100);
                var catComm = parsed.searchParams.get("commercialise_only");
                if (lettre && (/^[A-Za-z]$/.test(lettre) || lettre === "0-9")) {
                    cleanParams.set("lettre", lettre.toUpperCase());
                }
                if (catQ && catQ.trim()) cleanParams.set("q", catQ.trim().slice(0, 200));
                if (catPage && catPage !== "1") cleanParams.set("page", catPage);
                if (catPerPage && catPerPage !== "20") cleanParams.set("per_page", catPerPage);
                if (catComm === "true") cleanParams.set("commercialise_only", "true");
                var catQs = cleanParams.toString();
                return catQs ? "/catalogue?" + catQs : "/catalogue";
            }

            if (p === "/substances") {
                var subQ = parsed.searchParams.get("q");
                var subPage = sanitizePositiveIntString(parsed.searchParams.get("page"), 1, 10000);
                if (subQ && subQ.trim()) cleanParams.set("q", subQ.trim().slice(0, 200));
                if (subPage && subPage !== "1") cleanParams.set("page", subPage);
                var subQs = cleanParams.toString();
                return subQs ? "/substances?" + subQs : "/substances";
            }

            if (/^\/substances\/[A-Za-z0-9_-]{1,32}$/.test(p)) {
                var sdPage = sanitizePositiveIntString(parsed.searchParams.get("page"), 1, 10000);
                var sdComm = parsed.searchParams.get("commercialise_only");
                if (sdPage && sdPage !== "1") cleanParams.set("page", sdPage);
                if (sdComm === "true") cleanParams.set("commercialise_only", "true");
                var sdQs = cleanParams.toString();
                return sdQs ? p + "?" + sdQs : p;
            }

            if (/^\/generiques\/[A-Za-z0-9_-]{1,32}$/.test(p)) {
                return p;
            }

            if (/^\/medicaments\/\d{8}$/.test(p)) {
                return p;
            }

            return null;
        } catch (err) {
            return null;
        }
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
       4. Copie rapide (codes CIS, CIP13, code substance SA et lien de fiche)
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
       5. Retour intelligent avec conservation du contexte (recherche, filtres,
          pagination, catalogue A–Z, fiche substance ou groupe générique)
       ---------------------------------------------------------------------- */
    function initSmartBackLink() {
        var backLink = document.getElementById("smart-back-link");
        if (!backLink) return;

        var urlParams = new URLSearchParams(window.location.search);
        var rawReturn = urlParams.get("return_to");
        var safeTarget = sanitizeInternalReturnPath(rawReturn);

        // Repli sur document.referrer uniquement s'il provient de la même origine et d'une route autorisée
        if (!safeTarget && document.referrer) {
            try {
                var refUrl = new URL(document.referrer);
                if (refUrl.origin === window.location.origin) {
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
                var labelNode = backLink.querySelector(".back-link-text");
                if (labelNode) {
                    if (parsedTarget.pathname === "/") {
                        var rememberedQuery = parsedTarget.searchParams.get("q");
                        var rememberedPage = parsedTarget.searchParams.get("page");
                        if (rememberedQuery) {
                            labelNode.textContent = "Retour aux résultats (« " + rememberedQuery + " »" +
                                (rememberedPage && rememberedPage !== "1" ? ", p. " + rememberedPage : "") + ")";
                        } else if (parsedTarget.search) {
                            labelNode.textContent = "Retour aux résultats filtrés" +
                                (rememberedPage && rememberedPage !== "1" ? " (page " + rememberedPage + ")" : "");
                        }
                    } else if (parsedTarget.pathname === "/catalogue") {
                        var letter = parsedTarget.searchParams.get("lettre") || "A";
                        var catQuery = parsedTarget.searchParams.get("q");
                        var catPg = parsedTarget.searchParams.get("page");
                        var parts = ["lettre " + letter];
                        if (catQuery) parts.push("« " + catQuery + " »");
                        if (catPg && catPg !== "1") parts.push("p. " + catPg);
                        labelNode.textContent = "Retour au catalogue (" + parts.join(", ") + ")";
                    } else if (parsedTarget.pathname.indexOf("/substances/") === 0) {
                        labelNode.textContent = "Retour à la fiche substance active";
                    } else if (parsedTarget.pathname.indexOf("/generiques/") === 0) {
                        labelNode.textContent = "Retour au groupe générique";
                    }
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
       8. Recherche dynamique combinée aux filtres & pagination réelle (Lot 2B)
       ---------------------------------------------------------------------- */
    function renderMedicationCard(item, returnPath) {
        var detailHref = buildDetailHref(item.code_cis, returnPath);

        var commBadge = item.etat_commercialisation === "Commercialisée"
            ? '<span class="badge badge-active">Commercialisée</span>'
            : '<span class="badge badge-neutral">' + escapeHtml(item.etat_commercialisation || "Statut non précisé") + "</span>";

        var genericRoleBadge = "";
        if (item.generic_status === "princeps") {
            genericRoleBadge = '<span class="badge badge-princeps">Princeps (référence)</span>';
        } else if (item.generic_status === "generic") {
            genericRoleBadge = '<span class="badge badge-generic">Générique</span>';
        } else if (item.generic_status === "mixed") {
            genericRoleBadge = '<span class="badge badge-generic">Groupe générique (rôles multiples)</span>';
        } else if (item.generic_status === "in_group") {
            genericRoleBadge = '<span class="badge badge-generic">Groupe générique</span>';
        }

        var prescBadge = item.is_prescription_required
            ? '<span class="badge badge-vigilance">Sur prescription</span>'
            : "";

        var survBadge = item.surveillance_renforcee === "Oui"
            ? '<span class="badge badge-vigilance">▼ Surveillance renforcée</span>'
            : "";

        var approxBadge = item.match_type === "approximative_orthographique"
            ? '<span class="badge badge-vigilance">Correspondance approchée</span>'
            : "";

        var ansmCount = item.ansm_info_count !== undefined ? item.ansm_info_count : item.important_info_count;
        var ansmBadge = ansmCount > 0
            ? '<span class="badge badge-ansm">' + escapeHtml(ansmCount) + " info(s) ANSM disponible(s)</span>"
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
                var badgeClass = g.type_generique_code === "0" ? "badge-princeps" : "badge-generic";
                return (
                    '<div class="generic-line">' +
                    '<a href="/generiques/' + encodeURIComponent(g.identifiant_groupe) + '">' + escapeHtml(g.libelle_groupe) + "</a>" +
                    '<span class="badge ' + badgeClass + '">' + escapeHtml(g.type_generique_libelle) + "</span>" +
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
                var substHtml = (c.nature_composant === "SA" && c.code_substance)
                    ? '<a href="/substances/' + encodeURIComponent(c.code_substance) + '" class="subst-name">' + escapeHtml(c.substance) + "</a>"
                    : '<span class="subst-name">' + escapeHtml(c.substance) + "</span>";
                var dosageText = c.dosage
                    ? '— <strong class="mono">' + escapeHtml(c.dosage) + "</strong>"
                    : "— <em>dosage non renseigné dans la source</em>";
                var refText = c.reference_dosage
                    ? " (pour " + escapeHtml(c.reference_dosage) + ")"
                    : "";
                return "<li>" + substHtml + " " + dosageText + refText + "</li>";
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
            '<div class="med-badges">' + commBadge + genericRoleBadge + prescBadge + survBadge + approxBadge + ansmBadge + "</div>" +
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

    function renderPaginationHtml(pagination, buildPageUrl) {
        if (!pagination || pagination.total_pages <= 1) return "";
        var prevHtml = pagination.has_prev
            ? '<a href="' + escapeHtml(buildPageUrl(pagination.prev_page)) + '" class="btn-secondary" data-page="' + pagination.prev_page + '">← Page précédente</a>'
            : '<span class="btn-secondary is-disabled" aria-disabled="true">← Page précédente</span>';
        var nextHtml = pagination.has_next
            ? '<a href="' + escapeHtml(buildPageUrl(pagination.next_page)) + '" class="btn-secondary" data-page="' + pagination.next_page + '">Page suivante →</a>'
            : '<span class="btn-secondary is-disabled" aria-disabled="true">Page suivante →</span>';
        var plural = pagination.total_count > 1 ? "s" : "";

        return (
            '<nav class="pagination-bar" aria-label="Pagination des résultats">' +
            '<div class="pagination-summary">' +
            'Page <strong class="mono">' + escapeHtml(pagination.page) + "</strong> sur " +
            '<strong class="mono">' + escapeHtml(pagination.total_pages) + "</strong> — " +
            '<span class="mono">' + escapeHtml(pagination.total_count) + "</span> résultat" + plural + " au total" +
            "</div>" +
            '<div class="pagination-actions">' + prevHtml + nextHtml + "</div>" +
            "</nav>"
        );
    }

    function renderSearchPayload(payload, returnPath, buildPageUrl) {
        var html = "";
        var pag = payload.pagination || { page: 1, total_pages: 1, total_count: payload.count || 0 };
        var totalCount = payload.total_count !== undefined ? payload.total_count : payload.count;

        if (payload.status === "fuzzy_match") {
            html +=
                '<section class="alert-box alert-warning" role="alert">' +
                "<h2>Attention — Résultats proposés par approximation orthographique</h2>" +
                "<p>" + escapeHtml(payload.warning) + "</p>" +
                "</section>";
        } else if (payload.status === "page_out_of_range") {
            html +=
                '<section class="alert-box alert-warning" role="status">' +
                "<h2>Page demandée inexistante</h2>" +
                "<p>" + escapeHtml(payload.warning) + "</p>" +
                '<div class="pagination-actions" style="margin-top: 0.75rem;">' +
                '<a href="' + escapeHtml(buildPageUrl(1)) + '" class="btn-secondary" data-page="1">Revenir à la page 1</a>' +
                (pag.total_pages > 1
                    ? '<a href="' + escapeHtml(buildPageUrl(pag.total_pages)) + '" class="btn-secondary" data-page="' + pag.total_pages + '">Aller à la dernière page (' + escapeHtml(pag.total_pages) + ")</a>"
                    : "") +
                "</div>" +
                "</section>";
        } else if (payload.status === "no_results") {
            html +=
                '<section class="alert-box alert-error" role="status">' +
                "<h2>Aucun résultat trouvé</h2>" +
                "<p>" + escapeHtml(payload.warning) + "</p>" +
                "</section>";
        }

        if (payload.results && payload.results.length > 0) {
            var plural = totalCount > 1 ? "s" : "";
            var isCommOnly = Boolean(payload.filters && payload.filters.commercialise_only);
            var unitLabel = isCommOnly
                ? "spécialité" + plural + " commercialisée" + plural
                : "résultat" + plural;
            var headingScope = payload.query
                ? "pour « " + escapeHtml(payload.query) + " »"
                : "correspondant aux filtres actifs";
            var pageMeta = pag.total_pages > 1
                ? ' <span class="results-page-meta">(page <span class="mono">' + escapeHtml(pag.page) + '</span> / <span class="mono">' + escapeHtml(pag.total_pages) + "</span>)</span>"
                : "";
            var meta = payload.metadata || {};
            var totalDb = meta.specialites_count || "0";
            var commDb = meta.commercialisees_count || "0";
            var contextSummary = isCommOnly
                ? 'Filtre actif : commercialisées uniquement (parmi <span class="mono">' + escapeHtml(commDb) + '</span> commercialisées sur <span class="mono">' + escapeHtml(totalDb) + "</span> référencées en base)"
                : 'Base BDPM : <span class="mono">' + escapeHtml(totalDb) + '</span> spécialités référencées (dont <span class="mono">' + escapeHtml(commDb) + "</span> commercialisées)";
            var cards = payload.results.map(function (item) {
                return renderMedicationCard(item, returnPath);
            }).join("");
            html +=
                '<section class="results-section">' +
                '<div class="results-header">' +
                '<h2><span class="mono">' + escapeHtml(totalCount) + "</span> " + unitLabel + " " + headingScope + pageMeta + "</h2>" +
                '<p class="provenance-tag">' + contextSummary + "</p>" +
                "</div>" +
                '<div class="results-list">' + cards + "</div>" +
                renderPaginationHtml(pag, buildPageUrl) +
                "</section>";
        }
        return html;
    }

    function initLiveSearch() {
        var searchInput = document.getElementById("search-input");
        var clearBtn = document.getElementById("search-clear");
        var fuzzyCheckbox = document.getElementById("fuzzy-checkbox");
        var genericRoleSelect = document.getElementById("filter-generic-role");
        var formeSelect = document.getElementById("filter-forme");
        var perPageSelect = document.getElementById("filter-per-page");
        var commCheckbox = document.getElementById("filter-commercialise");
        var prescCheckbox = document.getElementById("filter-prescription");
        var ansmCheckbox = document.getElementById("filter-ansm");
        var resetLink = document.getElementById("reset-filters-link");

        var container = document.getElementById("dynamic-results-container");
        var statusIndicator = document.getElementById("live-status-indicator");
        var errorBanner = document.getElementById("network-error-banner");
        var retryBtn = document.getElementById("network-retry-btn");

        if (!searchInput || !container || !fuzzyCheckbox) return;

        var initialHtml = container.innerHTML;
        var debounceTimer = null;
        var activeController = null;
        var currentPage = 1;

        function readState(pageOverride) {
            return {
                q: searchInput.value.trim(),
                fuzzy: fuzzyCheckbox.checked,
                generic_role: genericRoleSelect ? genericRoleSelect.value : "",
                forme: formeSelect ? formeSelect.value : "",
                per_page: perPageSelect ? parseInt(perPageSelect.value, 10) || 20 : 20,
                commercialise_only: commCheckbox ? commCheckbox.checked : false,
                prescription_only: prescCheckbox ? prescCheckbox.checked : false,
                with_ansm_info: ansmCheckbox ? ansmCheckbox.checked : false,
                page: pageOverride || currentPage || 1,
            };
        }

        function hasAnyFilter(state) {
            return Boolean(
                state.commercialise_only ||
                state.forme ||
                state.generic_role ||
                state.prescription_only ||
                state.with_ansm_info
            );
        }

        function buildBrowserPath(state, targetPage) {
            var p = new URLSearchParams();
            if (state.q) p.set("q", state.q);
            if (!state.fuzzy) p.set("fuzzy", "false");
            else if (state.q) p.set("fuzzy", "true");
            if (state.per_page !== 20) p.set("per_page", String(state.per_page));
            if (state.commercialise_only) p.set("commercialise_only", "true");
            if (state.forme) p.set("forme", state.forme);
            if (state.generic_role) p.set("generic_role", state.generic_role);
            if (state.prescription_only) p.set("prescription_only", "true");
            if (state.with_ansm_info) p.set("with_ansm_info", "true");
            if (targetPage && targetPage > 1) p.set("page", String(targetPage));
            var qs = p.toString();
            return qs ? "/?" + qs : "/";
        }

        function syncControlsVisibility() {
            var state = readState(1);
            if (clearBtn) clearBtn.hidden = searchInput.value.length === 0;
            if (resetLink) resetLink.hidden = !(state.q || hasAnyFilter(state));
        }

        function hideNetworkError() {
            if (errorBanner) errorBanner.hidden = true;
        }

        function showNetworkError() {
            if (errorBanner) errorBanner.hidden = false;
        }

        async function performLiveSearch(targetPage) {
            currentPage = targetPage || 1;
            var state = readState(currentPage);
            syncControlsVisibility();
            hideNetworkError();

            var browserPath = buildBrowserPath(state, currentPage);
            window.history.replaceState({}, "", browserPath);

            if (!state.q && !hasAnyFilter(state)) {
                if (activeController) activeController.abort();
                if (statusIndicator) statusIndicator.textContent = "";
                container.innerHTML = initialHtml;
                return;
            }

            if (state.q && state.q.length < 2 && !/^\d+$/.test(state.q) && !hasAnyFilter(state)) {
                if (statusIndicator) {
                    statusIndicator.textContent = "Saisissez au moins 2 caractères (ou un chiffre) pour filtrer…";
                }
                return;
            }

            if (activeController) activeController.abort();
            activeController = new AbortController();
            if (statusIndicator) statusIndicator.textContent = "Recherche en cours…";

            try {
                var apiParams = new URLSearchParams();
                apiParams.set("q", state.q);
                apiParams.set("fuzzy", state.fuzzy ? "true" : "false");
                apiParams.set("page", String(state.page));
                apiParams.set("per_page", String(state.per_page));
                if (state.commercialise_only) apiParams.set("commercialise_only", "true");
                if (state.forme) apiParams.set("forme", state.forme);
                if (state.generic_role) apiParams.set("generic_role", state.generic_role);
                if (state.prescription_only) apiParams.set("prescription_only", "true");
                if (state.with_ansm_info) apiParams.set("with_ansm_info", "true");

                var resp = await fetch("/api/search?" + apiParams.toString(), {
                    signal: activeController.signal,
                });
                if (!resp.ok) throw new Error("HTTP " + resp.status);
                var payload = await resp.json();

                var buildPageUrl = function (pNum) {
                    return buildBrowserPath(state, pNum);
                };
                container.innerHTML = renderSearchPayload(payload, browserPath, buildPageUrl);

                if (statusIndicator) {
                    var total = payload.total_count !== undefined ? payload.total_count : payload.count;
                    statusIndicator.textContent = total + " résultat(s) trouvé(s)";
                }
            } catch (err) {
                if (err && err.name === "AbortError") return;
                if (statusIndicator) statusIndicator.textContent = "Erreur de connexion au serveur local";
                showNetworkError();
            }
        }

        searchInput.addEventListener("input", function () {
            syncControlsVisibility();
            clearTimeout(debounceTimer);
            debounceTimer = setTimeout(function () {
                performLiveSearch(1);
            }, 180);
        });

        [fuzzyCheckbox, genericRoleSelect, formeSelect, perPageSelect, commCheckbox, prescCheckbox, ansmCheckbox].forEach(function (ctrl) {
            if (!ctrl) return;
            ctrl.addEventListener("change", function () {
                performLiveSearch(1);
            });
        });

        if (clearBtn) {
            clearBtn.addEventListener("click", function () {
                searchInput.value = "";
                syncControlsVisibility();
                searchInput.focus();
                performLiveSearch(1);
            });
        }

        if (retryBtn) {
            retryBtn.addEventListener("click", function () {
                performLiveSearch(currentPage);
            });
        }

        document.querySelectorAll(".chip-btn[data-query]").forEach(function (btn) {
            btn.addEventListener("click", function () {
                searchInput.value = btn.getAttribute("data-query") || "";
                syncControlsVisibility();
                searchInput.focus();
                performLiveSearch(1);
            });
        });

        // Interception des clics de pagination générés dynamiquement pour une navigation fluide
        container.addEventListener("click", function (event) {
            var pageLink = event.target.closest("a[data-page]");
            if (!pageLink) return;
            var targetPage = parseInt(pageLink.getAttribute("data-page") || "1", 10);
            if (!isNaN(targetPage) && targetPage >= 1) {
                event.preventDefault();
                performLiveSearch(targetPage);
                window.scrollTo({ top: 0, behavior: "smooth" });
            }
        });

        syncControlsVisibility();
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
