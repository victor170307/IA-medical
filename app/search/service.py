"""Service de recherche de médicaments et de consultation des fiches détaillées BDPM.

Ce module respecte strictement les règles de sécurité et de transparence du projet :
- Aucune donnée médicale n'est inventée lorsque un champ est absent.
- Une recherche vide ou sans correspondance est signalée clairement.
- La recherche tolérante aux fautes de frappe (orthographique) est distinguée
  de la recherche exacte/normalisée et ne prétend jamais que deux médicaments
  au nom proche sont médicalement équivalents.
- Chaque donnée est rattachée à son fichier source officiel de la BDPM.
"""

from __future__ import annotations

from difflib import SequenceMatcher
import math
from pathlib import Path
import re
import sqlite3

from app.config import BDPM_BASE_URL, BDPM_SOURCE_NAME, DEFAULT_DB_PATH
from app.database import get_connection, init_db
from app.search.normalizer import normalize_text, tokenize_query

GENERIC_TYPE_LABELS = {
    "0": "Spécialité de référence (princeps)",
    "1": "Générique",
    "2": "Générique par complémentarité posologique",
    "4": "Générique substituable",
}

PRINCEPS_TYPE_CODE = "0"
GENERIC_MEMBER_TYPE_CODES = ("1", "2", "4")
VALID_GENERIC_ROLES = {"in_group", "generic", "princeps"}

# Critère SQL officiel pour identifier une spécialité soumise à prescription médicale
# d'après les libellés réels de CIS_CPD_bdpm.txt (exclut les lignes d'usage hospitalier
# ou professionnel seul sans mention de liste I/II, stupéfiants, prescription ou ordonnance).
PRESCRIPTION_SQL_CONDITION = """
EXISTS (
    SELECT 1 FROM conditions_prescription cpd
    WHERE cpd.code_cis = s.code_cis
      AND (
          LOWER(cpd.condition_prescription) IN ('liste i', 'liste ii', 'stupéfiants', 'stupefiants')
          OR (
              (
                  LOWER(cpd.condition_prescription) LIKE '%prescription%'
                  OR LOWER(cpd.condition_prescription) LIKE '%ordonnance%'
              )
              AND LOWER(cpd.condition_prescription) NOT LIKE '%facultativ%'
          )
      )
)
"""

# Cache mémoire des termes candidats (mot -> poids de fréquence) pour accélérer la tolérance orthographique
_FUZZY_CANDIDATES_CACHE: dict[tuple[int, int], dict[str, int]] = {}


def _escape_like_pattern(value: str) -> str:
    """Échappe les caractères jokers SQLite LIKE ('\\', '%', '_') pour qu'ils soient
    traités comme des caractères littéraux avec `ESCAPE '\\'`.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _extract_official_sa_dosage_entries(compositions: list[dict]) -> list[dict]:
    """Extrait les paires officielles (code_substance, substance, dosage) des lignes
    de nature `SA` issues de `CIS_COMPO_bdpm.txt`, uniquement lorsqu'elles sont
    complètes, fiables et dépourvues de toute ambiguïté.

    Règles strictes :
    - Ne modifie, ne simplifie, ne déduit et n'invente aucun dosage.
    - Utilise exclusivement les champs officiels de chaque ligne `SA` (`code_substance`,
      `substance`, `dosage`, `designation_element`, `reference_dosage`).
    - Retourne `[]` dès qu'une ambiguïté existe :
      * aucune substance active `SA` ou plus de 3 substances actives distinctes ;
      * présence de plusieurs éléments pharmaceutiques distincts (ex. comprimé jour / comprimé nuit) ;
      * substance active `SA` sans `code_substance`, sans dénomination ou sans dosage officiel ;
      * plusieurs dosages différents pour un même `code_substance` ;
      * références de dosage incohérentes entre les substances (ex. dose délivrée vs dose mesurée).
    """
    sa_rows = [comp for comp in compositions if comp.get("nature_composant") == "SA"]
    if not sa_rows:
        return []

    distinct_elements = {
        (comp.get("designation_element") or "").strip().lower()
        for comp in sa_rows
    }
    if len(distinct_elements) > 1:
        return []

    entries_by_code: dict[str, dict] = {}
    ordered_entries: list[dict] = []

    for comp in sa_rows:
        code_sub = (comp.get("code_substance") or "").strip()
        sub_name = (comp.get("substance") or "").strip()
        raw_dosage = (comp.get("dosage") or "").strip()
        ref_dosage = (comp.get("reference_dosage") or "").strip()

        if not code_sub or not sub_name or not raw_dosage:
            return []

        existing = entries_by_code.get(code_sub)
        if existing is not None:
            if (
                existing["dosage"] != raw_dosage
                or existing["substance"] != sub_name
                or re.sub(r"\s+", "", existing["reference_dosage"].lower())
                != re.sub(r"\s+", "", ref_dosage.lower())
            ):
                return []
            continue

        entry = {
            "code_substance": code_sub,
            "substance": sub_name,
            "dosage": raw_dosage,
            "reference_dosage": ref_dosage,
        }
        entries_by_code[code_sub] = entry
        ordered_entries.append(entry)

    if not ordered_entries or len(ordered_entries) > 3:
        return []

    if len(ordered_entries) > 1:
        distinct_refs = {
            re.sub(r"\s+", "", entry["reference_dosage"].lower())
            for entry in ordered_entries
        }
        if len(distinct_refs) > 1:
            return []

    return ordered_entries


def _format_official_sa_dosages(entries: list[dict]) -> list[str]:
    """Formate les dosages officiels validés pour le catalogue et l'API :
    - 1 substance active : dosage officiel simple (ex. `['500 mg']`).
    - 2 ou 3 substances actives : association explicite `SUBSTANCE : dosage`
      (ex. `['LAMIVUDINE : 300 mg', 'ABACAVIR : 600 mg']`).
    - 0 ou ambigu : liste vide `[]`.
    """
    if not entries:
        return []
    if len(entries) == 1:
        return [entries[0]["dosage"]]
    return [f"{entry['substance']} : {entry['dosage']}" for entry in entries]


def _extract_official_sa_dosages(compositions: list[dict]) -> list[str]:
    """Extrait les dosages officiels SA formatés et sans ambiguïté (liste vide si ambigu ou > 3 SA)."""
    return _format_official_sa_dosages(_extract_official_sa_dosage_entries(compositions))


def _is_prescription_condition_text(text: str | None) -> bool:
    """Vérifie si un libellé de CIS_CPD_bdpm.txt correspond à une soumission à prescription."""
    if not text:
        return False
    low = text.strip().lower()
    if low in ("liste i", "liste ii", "stupéfiants", "stupefiants"):
        return True
    if ("prescription" in low or "ordonnance" in low) and "facultativ" not in low:
        return True
    return False


def _normalize_generic_role(generic_role: str | None) -> str:
    """Normalise le filtre de rôle générique ('in_group', 'generic', 'princeps' ou '')."""
    cleaned = (generic_role or "").strip().lower()
    return cleaned if cleaned in VALID_GENERIC_ROLES else ""


def _build_pagination(total_count: int, page: int, per_page: int) -> dict:
    """Construit des métadonnées de pagination cohérentes, y compris pour les pages inexistantes.

    Comportement pour une page hors plage (page > total_pages quand total_count > 0) :
    - `page_out_of_range` vaut True ;
    - aucun résultat inventé ni renvoi silencieux de la page 1 ;
    - `prev_page` pointe vers la dernière page valide pour faciliter le retour.
    """
    safe_per_page = max(1, min(int(per_page), 100))
    safe_page = max(1, int(page))
    total_pages = math.ceil(total_count / safe_per_page) if total_count > 0 else 0
    page_out_of_range = total_count > 0 and safe_page > total_pages
    offset = (safe_page - 1) * safe_per_page
    return {
        "page": safe_page,
        "per_page": safe_per_page,
        "total_count": total_count,
        "total_pages": total_pages,
        "has_prev": total_count > 0 and safe_page > 1,
        "has_next": total_count > 0 and safe_page < total_pages,
        "prev_page": (
            min(safe_page - 1, total_pages)
            if total_pages > 0 and safe_page > 1
            else None
        ),
        "next_page": (safe_page + 1 if safe_page < total_pages else None),
        "page_out_of_range": page_out_of_range,
        "offset": offset,
    }


def _build_filter_clauses(
    *,
    commercialise_only: bool = False,
    forme: str | None = None,
    generic_role: str | None = None,
    prescription_only: bool = False,
    with_ansm_info: bool = False,
) -> tuple[list[str], list[str | int], dict]:
    """Construit des clauses SQL sûres (EXISTS + paramètres ?) pour les filtres combinables.

    Toutes les jointures passent par EXISTS afin qu'une spécialité multi-compositions
    ou présente dans plusieurs groupes génériques ne soit jamais dupliquée.
    """
    clauses: list[str] = []
    params: list[str | int] = []

    clean_forme = (forme or "").strip()
    norm_forme = normalize_text(clean_forme)
    clean_generic_role = _normalize_generic_role(generic_role)

    if commercialise_only:
        clauses.append("s.etat_commercialisation = ?")
        params.append("Commercialisée")

    if norm_forme:
        clauses.append("s.forme_normalisee LIKE ?")
        params.append(f"%{norm_forme}%")

    if clean_generic_role == "in_group":
        clauses.append(
            "EXISTS (SELECT 1 FROM groupes_generiques g WHERE g.code_cis = s.code_cis)"
        )
    elif clean_generic_role == "generic":
        clauses.append(
            "EXISTS (SELECT 1 FROM groupes_generiques g WHERE g.code_cis = s.code_cis AND g.type_generique IN (?, ?, ?))"
        )
        params.extend(GENERIC_MEMBER_TYPE_CODES)
    elif clean_generic_role == "princeps":
        clauses.append(
            "EXISTS (SELECT 1 FROM groupes_generiques g WHERE g.code_cis = s.code_cis AND g.type_generique = ?)"
        )
        params.append(PRINCEPS_TYPE_CODE)

    if prescription_only:
        clauses.append(PRESCRIPTION_SQL_CONDITION)

    if with_ansm_info:
        clauses.append(
            "EXISTS (SELECT 1 FROM infos_importantes ii WHERE ii.code_cis = s.code_cis)"
        )

    active_filters = {
        "commercialise_only": bool(commercialise_only),
        "forme": clean_forme,
        "generic_role": clean_generic_role,
        "prescription_only": bool(prescription_only),
        "with_ansm_info": bool(with_ansm_info),
    }
    return clauses, params, active_filters


def get_available_forms(
    db_path: Path | str = DEFAULT_DB_PATH,
    limit: int = 40,
) -> list[dict]:
    """Retourne les formes pharmaceutiques réellement présentes dans la base SQLite,
    classées par fréquence décroissante, pour alimenter le filtre de recherche.
    """
    meta = get_db_metadata(db_path)
    if meta["ready"] != "true":
        return []

    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT forme_pharmaceutique, COUNT(*) AS cnt
            FROM specialites
            WHERE forme_pharmaceutique IS NOT NULL AND TRIM(forme_pharmaceutique) != ''
            GROUP BY forme_pharmaceutique
            ORDER BY cnt DESC, forme_pharmaceutique ASC
            LIMIT ?
            """,
            (max(1, min(limit, 200)),),
        ).fetchall()
    return [
        {"forme": r["forme_pharmaceutique"], "count": r["cnt"]}
        for r in rows
    ]


def get_db_metadata(db_path: Path | str = DEFAULT_DB_PATH) -> dict[str, str]:
    """Récupère les métadonnées d'importation, la provenance et les compteurs réels SQLite."""
    path_obj = Path(db_path)
    if not path_obj.exists():
        return {
            "ready": "false",
            "specialites_count": "0",
            "commercialisees_count": "0",
            "official_bdpm_date": "Base non initialisée",
            "imported_at_utc": "Aucun import effectué",
            "source_name": BDPM_SOURCE_NAME,
            "source_url": BDPM_BASE_URL,
        }

    init_db(path_obj)
    with get_connection(path_obj) as conn:
        rows = conn.execute("SELECT key, value FROM import_metadata;").fetchall()
        meta = {row["key"]: row["value"] for row in rows}
        count_row = conn.execute(
            """
            SELECT
                COUNT(*) AS total_cnt,
                SUM(CASE WHEN etat_commercialisation = 'Commercialisée' THEN 1 ELSE 0 END) AS comm_cnt
            FROM specialites;
            """
        ).fetchone()
        actual_count = count_row["total_cnt"] if count_row and count_row["total_cnt"] else 0
        comm_count = count_row["comm_cnt"] if count_row and count_row["comm_cnt"] else 0

    return {
        "ready": "true" if actual_count > 0 else "false",
        "specialites_count": str(actual_count),
        "commercialisees_count": str(comm_count),
        "official_bdpm_date": meta.get("official_bdpm_date", "Non précisée"),
        "imported_at_utc": meta.get("imported_at_utc", "Non précisée"),
        "source_name": meta.get("source_name", BDPM_SOURCE_NAME),
        "source_url": meta.get("source_url", BDPM_BASE_URL),
    }


def list_initial_medications(
    db_path: Path | str = DEFAULT_DB_PATH,
    limit: int = 24,
) -> list[dict]:
    """Retourne un aperçu initial de spécialités commercialisées présentes dans la base
    afin d'afficher une liste consultable et filtrable dès l'arrivée sur la page d'accueil.
    """
    meta = get_db_metadata(db_path)
    if meta["ready"] != "true":
        return []

    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM specialites
            WHERE etat_commercialisation = 'Commercialisée'
            ORDER BY denomination ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        if not rows:
            rows = conn.execute(
                "SELECT * FROM specialites ORDER BY denomination ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return _enrich_search_results(conn, rows, match_type="exacte_ou_normalisee")


def search_medications(
    query: str | None,
    db_path: Path | str = DEFAULT_DB_PATH,
    limit: int = 30,
    allow_fuzzy: bool = True,
    *,
    page: int = 1,
    per_page: int | None = None,
    commercialise_only: bool = False,
    forme: str | None = None,
    generic_role: str | None = None,
    prescription_only: bool = False,
    with_ansm_info: bool = False,
) -> dict:
    """Recherche paginée et filtrable de médicaments dans la base SQLite BDPM.

    Étapes :
    1. Si la requête est vide et qu'aucun filtre n'est actif -> retourne 0 résultat (`status='empty_query'`).
    2. Si la requête est vide mais qu'au moins un filtre est actif -> parcourt la base filtrée et paginée.
    3. Recherche exacte/normalisée sur le code CIS, le code CIP13/CIP7, la dénomination,
       les substances actives, les dosages, la forme pharmaceutique, les voies d'administration,
       les titulaires, les présentations et les groupes génériques (`CIS_GENER_bdpm.txt`),
       combinée aux filtres demandés.
    4. Si aucun résultat exact/normalisé n'existe (total_count == 0) et que `allow_fuzzy=True`,
       tente une tolérance orthographique sans jamais altérer les nombres/dosages.
    5. Si des résultats existent mais que `page > total_pages`, renvoie `status='page_out_of_range'`
       avec `results=[]`, `total_count` exact et `pagination['page_out_of_range'] = True`.
    """
    raw_query = (query or "").strip()
    tokens = tokenize_query(raw_query)
    meta = get_db_metadata(db_path)

    effective_per_page = per_page if per_page is not None else limit
    filter_clauses, filter_params, active_filters = _build_filter_clauses(
        commercialise_only=commercialise_only,
        forme=forme,
        generic_role=generic_role,
        prescription_only=prescription_only,
        with_ansm_info=with_ansm_info,
    )
    has_active_filters = any(active_filters.values())

    if not tokens and not has_active_filters:
        pag = _build_pagination(0, page, effective_per_page)
        return {
            "query": raw_query,
            "normalized_query": "",
            "status": "empty_query",
            "match_mode": "none",
            "warning": None,
            "suggested_terms": [],
            "count": 0,
            "total_count": 0,
            "pagination": pag,
            "filters": active_filters,
            "results": [],
            "metadata": meta,
        }

    if meta["ready"] != "true":
        pag = _build_pagination(0, page, effective_per_page)
        return {
            "query": raw_query,
            "normalized_query": " ".join(tokens),
            "status": "database_empty",
            "match_mode": "none",
            "warning": (
                "La base de données locale ne contient encore aucun médicament importé. "
                "Veuillez exécuter la commande d'importation de la BDPM."
            ),
            "suggested_terms": [],
            "count": 0,
            "total_count": 0,
            "pagination": pag,
            "filters": active_filters,
            "results": [],
            "metadata": meta,
        }

    with get_connection(db_path) as conn:
        # Cas A : recherche uniquement par filtres (sans mot-clé saisi)
        if not tokens and has_active_filters:
            rows, total_count = _execute_filtered_only_search(
                conn,
                filter_clauses=filter_clauses,
                filter_params=filter_params,
                page=page,
                per_page=effective_per_page,
            )
            pag = _build_pagination(total_count, page, effective_per_page)
            if total_count == 0:
                return {
                    "query": raw_query,
                    "normalized_query": "",
                    "status": "no_results",
                    "match_mode": "filtered_only",
                    "warning": (
                        "Aucun médicament ne correspond à cette combinaison de filtres dans les données BDPM importées. "
                        "L'application n'invente aucun résultat."
                    ),
                    "suggested_terms": [],
                    "count": 0,
                    "total_count": 0,
                    "pagination": pag,
                    "filters": active_filters,
                    "results": [],
                    "metadata": meta,
                }
            if pag["page_out_of_range"]:
                return {
                    "query": raw_query,
                    "normalized_query": "",
                    "status": "page_out_of_range",
                    "match_mode": "filtered_only",
                    "warning": (
                        f"La page {pag['page']} n'existe pas : cette sélection comporte "
                        f"{pag['total_pages']} page(s) pour {total_count} résultat(s) au total."
                    ),
                    "suggested_terms": [],
                    "count": 0,
                    "total_count": total_count,
                    "pagination": pag,
                    "filters": active_filters,
                    "results": [],
                    "metadata": meta,
                }
            enriched = _enrich_search_results(conn, rows, match_type="exacte_ou_normalisee")
            return {
                "query": raw_query,
                "normalized_query": "",
                "status": "ok",
                "match_mode": "filtered_only",
                "warning": None,
                "suggested_terms": [],
                "count": len(enriched),
                "total_count": total_count,
                "pagination": pag,
                "filters": active_filters,
                "results": enriched,
                "metadata": meta,
            }

        # Cas B : recherche exacte / normalisée (avec éventuels filtres combinés)
        exact_rows, exact_total = _execute_normalized_search(
            conn,
            tokens,
            filter_clauses=filter_clauses,
            filter_params=filter_params,
            page=page,
            per_page=effective_per_page,
        )
        if exact_total > 0:
            pag = _build_pagination(exact_total, page, effective_per_page)
            if pag["page_out_of_range"]:
                return {
                    "query": raw_query,
                    "normalized_query": " ".join(tokens),
                    "status": "page_out_of_range",
                    "match_mode": "exact_or_normalized",
                    "warning": (
                        f"La page {pag['page']} n'existe pas : votre recherche comporte "
                        f"{pag['total_pages']} page(s) pour {exact_total} résultat(s) au total."
                    ),
                    "suggested_terms": [],
                    "count": 0,
                    "total_count": exact_total,
                    "pagination": pag,
                    "filters": active_filters,
                    "results": [],
                    "metadata": meta,
                }
            enriched = _enrich_search_results(conn, exact_rows, match_type="exacte_ou_normalisee")
            return {
                "query": raw_query,
                "normalized_query": " ".join(tokens),
                "status": "ok",
                "match_mode": "exact_or_normalized",
                "warning": None,
                "suggested_terms": [],
                "count": len(enriched),
                "total_count": exact_total,
                "pagination": pag,
                "filters": active_filters,
                "results": enriched,
                "metadata": meta,
            }

        # Cas C : aucune correspondance exacte/normalisée : tentative de tolérance orthographique
        if allow_fuzzy:
            fuzzy_tokens, suggested_terms = _find_fuzzy_token_replacements(conn, tokens)
            if suggested_terms and fuzzy_tokens != tokens:
                fuzzy_rows, fuzzy_total = _execute_normalized_search(
                    conn,
                    fuzzy_tokens,
                    filter_clauses=filter_clauses,
                    filter_params=filter_params,
                    page=page,
                    per_page=effective_per_page,
                )
                if fuzzy_total > 0:
                    pag = _build_pagination(fuzzy_total, page, effective_per_page)
                    if pag["page_out_of_range"]:
                        return {
                            "query": raw_query,
                            "normalized_query": " ".join(tokens),
                            "status": "page_out_of_range",
                            "match_mode": "fuzzy_orthographic",
                            "warning": (
                                f"La page {pag['page']} n'existe pas : cette recherche approchée comporte "
                                f"{pag['total_pages']} page(s) pour {fuzzy_total} résultat(s) au total."
                            ),
                            "suggested_terms": suggested_terms,
                            "count": 0,
                            "total_count": fuzzy_total,
                            "pagination": pag,
                            "filters": active_filters,
                            "results": [],
                            "metadata": meta,
                        }
                    enriched_fuzzy = _enrich_search_results(
                        conn,
                        fuzzy_rows,
                        match_type="approximative_orthographique",
                    )
                    return {
                        "query": raw_query,
                        "normalized_query": " ".join(tokens),
                        "status": "fuzzy_match",
                        "match_mode": "fuzzy_orthographic",
                        "warning": (
                            "Aucun médicament ne correspond exactement à votre saisie « "
                            f"{raw_query} ». Les résultats ci-dessous sont proposés par "
                            "ressemblance orthographique ("
                            + ", ".join(
                                f"« {s['original']} » → « {s['suggested']} »"
                                for s in suggested_terms
                            )
                            + "). Une ressemblance de nom ne prouve jamais que deux "
                            "médicaments sont identiques ou équivalents."
                        ),
                        "suggested_terms": suggested_terms,
                        "count": len(enriched_fuzzy),
                        "total_count": fuzzy_total,
                        "pagination": pag,
                        "filters": active_filters,
                        "results": enriched_fuzzy,
                        "metadata": meta,
                    }

    pag = _build_pagination(0, page, effective_per_page)
    return {
        "query": raw_query,
        "normalized_query": " ".join(tokens),
        "status": "no_results",
        "match_mode": "none",
        "warning": (
            f"Aucun médicament trouvé dans les données BDPM importées pour « {raw_query} ». "
            "L'application n'invente aucun résultat lorsque la donnée est absente."
        ),
        "suggested_terms": [],
        "count": 0,
        "total_count": 0,
        "pagination": pag,
        "filters": active_filters,
        "results": [],
        "metadata": meta,
    }


def _execute_filtered_only_search(
    conn: sqlite3.Connection,
    *,
    filter_clauses: list[str],
    filter_params: list[str | int],
    page: int = 1,
    per_page: int = 30,
) -> tuple[list[sqlite3.Row], int]:
    """Exécute une recherche paginée uniquement basée sur les filtres combinés (sans texte)."""
    where_sql = " AND ".join(filter_clauses) if filter_clauses else "1=1"
    total_count = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM specialites s WHERE {where_sql}",
        filter_params,
    ).fetchone()["cnt"]

    pag = _build_pagination(total_count, page, per_page)
    if total_count == 0 or pag["page_out_of_range"]:
        return [], total_count

    rows = conn.execute(
        f"""
        SELECT s.*
        FROM specialites s
        WHERE {where_sql}
        ORDER BY
            CASE WHEN s.etat_commercialisation = 'Commercialisée' THEN 0 ELSE 1 END ASC,
            s.denomination ASC
        LIMIT ? OFFSET ?
        """,
        [*filter_params, pag["per_page"], pag["offset"]],
    ).fetchall()
    return rows, total_count


def _execute_normalized_search(
    conn: sqlite3.Connection,
    tokens: list[str],
    *,
    filter_clauses: list[str] | None = None,
    filter_params: list[str | int] | None = None,
    page: int = 1,
    per_page: int = 30,
) -> tuple[list[sqlite3.Row], int]:
    """Exécute une recherche SQL paginée et filtrée sans doublons.

    Chaque mot-clé de la requête doit être présent soit dans le code CIS, le code CIP13/CIP7,
    la dénomination, la forme pharmaceutique, la voie d'administration, le titulaire,
    la substance active, le dosage, le libellé de présentation ou le groupe générique.
    """
    normalized_full = " ".join(tokens)
    extra_clauses = list(filter_clauses or [])
    extra_params = list(filter_params or [])

    # Cas 1 : Recherche directe par Code CIS (8 chiffres), CIP7 (7 chiffres) ou CIP13 (13 chiffres)
    direct_clause: str | None = None
    direct_params: list[str | int] = []

    if len(tokens) == 1 and re.match(r"^\d{8}$", tokens[0]):
        direct_clause = "s.code_cis = ?"
        direct_params = [tokens[0]]
    elif len(tokens) == 1 and re.match(r"^\d{7}$", tokens[0]):
        direct_clause = "EXISTS (SELECT 1 FROM presentations p WHERE p.code_cis = s.code_cis AND p.code_cip7 = ?)"
        direct_params = [tokens[0]]
    elif len(tokens) == 1 and re.match(r"^\d{13}$", tokens[0]):
        direct_clause = "EXISTS (SELECT 1 FROM presentations p WHERE p.code_cis = s.code_cis AND p.code_cip13 = ?)"
        direct_params = [tokens[0]]

    if direct_clause is not None:
        all_clauses = [direct_clause, *extra_clauses]
        all_params = [*direct_params, *extra_params]
        where_sql = " AND ".join(all_clauses)
        total_count = conn.execute(
            f"SELECT COUNT(*) AS cnt FROM specialites s WHERE {where_sql}",
            all_params,
        ).fetchone()["cnt"]
        pag = _build_pagination(total_count, page, per_page)
        if total_count == 0 or pag["page_out_of_range"]:
            return [], total_count
        rows = conn.execute(
            f"""
            SELECT s.*
            FROM specialites s
            WHERE {where_sql}
            ORDER BY
                CASE WHEN s.etat_commercialisation = 'Commercialisée' THEN 0 ELSE 1 END ASC,
                s.denomination ASC
            LIMIT ? OFFSET ?
            """,
            [*all_params, pag["per_page"], pag["offset"]],
        ).fetchall()
        return rows, total_count

    # Cas 2 : Recherche multi-critères normalisée (nom + substance + dosage + forme + groupe générique + voie + labo)
    where_clauses: list[str] = []
    params: list[str | int] = []

    for token in tokens:
        if token.isdigit():
            # Pour un nombre (ex: dosage '5', '10', '20', '500', '1000'), on cherche au début
            # d'un jeton numérique (' 5%') pour éviter que '5' ne corresponde au '5' final de '0,05'
            num_token_pat = f"% {token}%"
            where_clauses.append(
                """
                (
                    (' ' || s.denomination_normalisee || ' ') LIKE ?
                    OR s.code_cis = ?
                    OR EXISTS (
                        SELECT 1 FROM compositions c
                        WHERE c.code_cis = s.code_cis
                          AND (' ' || c.dosage_normalise || ' ') LIKE ?
                    )
                    OR EXISTS (
                        SELECT 1 FROM groupes_generiques g
                        WHERE g.code_cis = s.code_cis
                          AND (
                              (' ' || g.libelle_groupe_normalise || ' ') LIKE ?
                              OR g.identifiant_groupe = ?
                          )
                    )
                    OR EXISTS (
                        SELECT 1 FROM presentations p
                        WHERE p.code_cis = s.code_cis
                          AND (
                              p.code_cip13 = ?
                              OR p.code_cip7 = ?
                              OR (' ' || COALESCE(p.libelle_presentation, '') || ' ') LIKE ?
                          )
                    )
                )
                """
            )
            params.extend(
                [
                    num_token_pat,
                    token,
                    num_token_pat,
                    num_token_pat,
                    token,
                    token,
                    token,
                    num_token_pat,
                ]
            )
        else:
            # Tolérance au pluriel sur les mots alphabétiques de 5 lettres ou plus
            # (ex: 'comprimes' -> '%comprime%', 'gelules' -> '%gelule%', 'capsules' -> '%capsule%')
            if len(token) >= 5 and token.endswith("s") and token.isalpha():
                like_pat = f"%{token[:-1]}%"
            else:
                like_pat = f"%{token}%"

            where_clauses.append(
                """
                (
                    s.denomination_normalisee LIKE ?
                    OR s.forme_normalisee LIKE ?
                    OR REPLACE(REPLACE(REPLACE(LOWER(COALESCE(s.voies_administration, '')), 'é', 'e'), 'è', 'e'), 'ê', 'e') LIKE ?
                    OR LOWER(COALESCE(s.titulaires, '')) LIKE ?
                    OR s.code_cis = ?
                    OR EXISTS (
                        SELECT 1 FROM compositions c
                        WHERE c.code_cis = s.code_cis
                          AND (
                              c.substance_normalisee LIKE ?
                              OR c.dosage_normalise LIKE ?
                          )
                    )
                    OR EXISTS (
                        SELECT 1 FROM groupes_generiques g
                        WHERE g.code_cis = s.code_cis
                          AND g.libelle_groupe_normalise LIKE ?
                    )
                    OR EXISTS (
                        SELECT 1 FROM presentations p
                        WHERE p.code_cis = s.code_cis
                          AND REPLACE(REPLACE(REPLACE(REPLACE(LOWER(COALESCE(p.libelle_presentation, '')), 'é', 'e'), 'è', 'e'), 'ê', 'e'), 'î', 'i') LIKE ?
                    )
                )
                """
            )
            params.extend(
                [
                    like_pat,
                    like_pat,
                    like_pat,
                    like_pat,
                    token,
                    like_pat,
                    like_pat,
                    like_pat,
                    like_pat,
                ]
            )

    all_where_clauses = [*where_clauses, *extra_clauses]
    all_where_params = [*params, *extra_params]
    where_sql = " AND ".join(all_where_clauses)

    total_count = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM specialites s WHERE {where_sql}",
        all_where_params,
    ).fetchone()["cnt"]

    pag = _build_pagination(total_count, page, per_page)
    if total_count == 0 or pag["page_out_of_range"]:
        return [], total_count

    sql = f"""
        SELECT
            s.*,
            CASE
                WHEN s.denomination_normalisee = ? THEN 0
                WHEN s.marque_principale_normalisee = ? THEN 1
                WHEN s.denomination_normalisee LIKE ? THEN 2
                WHEN s.denomination_normalisee LIKE ? THEN 3
                WHEN s.etat_commercialisation = 'Commercialisée' THEN 4
                ELSE 5
            END AS rank_priority
        FROM specialites s
        WHERE {where_sql}
        ORDER BY
            rank_priority ASC,
            CASE WHEN s.etat_commercialisation = 'Commercialisée' THEN 0 ELSE 1 END ASC,
            s.denomination ASC
        LIMIT ? OFFSET ?
    """
    first_token = tokens[0]
    prefix_pat = f"{normalized_full}%"
    first_contains_pat = f"%{first_token}%"
    final_params: list[str | int] = [
        normalized_full,
        first_token,
        prefix_pat,
        first_contains_pat,
        *all_where_params,
        pag["per_page"],
        pag["offset"],
    ]
    return conn.execute(sql, final_params).fetchall(), total_count


def _levenshtein_distance(s1: str, s2: str, max_dist: int = 2) -> int:
    """Calcule la distance d'édition de Levenshtein avec arrêt précoce."""
    if abs(len(s1) - len(s2)) > max_dist:
        return max_dist + 1
    if s1 == s2:
        return 0
    if not s1:
        return len(s2)
    if not s2:
        return len(s1)

    previous_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        current_row = [i + 1] + [0] * len(s2)
        min_in_row = current_row[0]
        for j, c2 in enumerate(s2):
            cost = 0 if c1 == c2 else 1
            current_row[j + 1] = min(
                previous_row[j + 1] + 1,      # suppression
                current_row[j] + 1,           # insertion
                previous_row[j] + cost        # substitution
            )
            if current_row[j + 1] < min_in_row:
                min_in_row = current_row[j + 1]
        if min_in_row > max_dist:
            return max_dist + 1
        previous_row = current_row

    return previous_row[len(s2)]


def _common_prefix_length(s1: str, s2: str) -> int:
    """Calcule la longueur du préfixe commun entre deux chaînes."""
    length = 0
    max_len = min(len(s1), len(s2))
    while length < max_len and s1[length] == s2[length]:
        length += 1
    return length


def _get_fuzzy_candidate_terms(conn: sqlite3.Connection) -> dict[str, int]:
    """Récupère (avec mise en cache) l'ensemble des termes officiels normalisés
    et leur poids de fréquence sur la totalité des 15 000+ spécialités médicales :
    - marques principales et mots des dénominations (`CIS_bdpm.txt`) ;
    - substances actives (`CIS_COMPO_bdpm.txt`) ;
    - groupes génériques et princeps de référence (`CIS_GENER_bdpm.txt`) ;
    - titulaires de laboratoire.
    """
    spec_cnt = conn.execute("SELECT COUNT(*) FROM specialites").fetchone()[0]
    gener_cnt = conn.execute("SELECT COUNT(*) FROM groupes_generiques").fetchone()[0]
    cache_key = (spec_cnt, gener_cnt)

    if cache_key in _FUZZY_CANDIDATES_CACHE and spec_cnt > 100:
        return _FUZZY_CANDIDATES_CACHE[cache_key]

    candidate_weights: dict[str, int] = {}

    def add_word(word: str, weight: int = 1) -> None:
        if len(word) >= 4 and word.isalpha():
            candidate_weights[word] = candidate_weights.get(word, 0) + weight

    # 1. Marques principales et mots des dénominations de TOUTES les spécialités en base
    denom_rows = conn.execute(
        "SELECT denomination_normalisee, marque_principale_normalisee, etat_commercialisation FROM specialites WHERE denomination_normalisee != ''"
    ).fetchall()
    for r in denom_rows:
        comm_bonus = 2 if r["etat_commercialisation"] == "Commercialisée" else 1
        marque = r["marque_principale_normalisee"] or ""
        if marque:
            add_word(marque, 4 * comm_bonus)
        for word in r["denomination_normalisee"].split():
            add_word(word, comm_bonus)

    # 2. Toutes les substances actives de composition
    subst_rows = conn.execute(
        "SELECT substance_normalisee AS term FROM compositions WHERE substance_normalisee != ''"
    ).fetchall()
    for r in subst_rows:
        for word in r["term"].split():
            add_word(word, 3)

    # 3. Tous les libellés de groupes génériques et leurs princeps associés
    gener_rows = conn.execute(
        "SELECT libelle_groupe_normalise AS term FROM groupes_generiques WHERE libelle_groupe_normalise != ''"
    ).fetchall()
    for r in gener_rows:
        for word in r["term"].split():
            add_word(word, 3)

    # 4. Tous les titulaires / laboratoires
    lab_rows = conn.execute(
        "SELECT DISTINCT LOWER(titulaires) AS term FROM specialites WHERE titulaires IS NOT NULL"
    ).fetchall()
    for r in lab_rows:
        if r["term"]:
            for word in r["term"].split():
                add_word(word, 1)

    if spec_cnt > 100:
        _FUZZY_CANDIDATES_CACHE[cache_key] = candidate_weights
    return candidate_weights


def _find_fuzzy_token_replacements(
    conn: sqlite3.Connection,
    tokens: list[str],
    min_similarity: float = 0.72,
) -> tuple[list[str], list[dict]]:
    """Cherche pour les mots alphabétiques non reconnus (d'au moins 4 lettres)
    le terme officiel le plus proche parmi l'ensemble du vocabulaire des 15 000+ médicaments
    (algorithme combinant distance de Levenshtein sur le mot complet et sur son préfixe,
    longueur de préfixe commun et pondération par fréquence dans la BDPM).
    """
    candidate_weights = _get_fuzzy_candidate_terms(conn)

    new_tokens: list[str] = []
    suggestions: list[dict] = []

    for token in tokens:
        # On ne corrige pas les nombres (dosages) pour ne jamais altérer un dosage médical saisi !
        if len(token) < 4 or any(ch.isdigit() for ch in token) or token in candidate_weights:
            new_tokens.append(token)
            continue

        best_term: str | None = None
        best_score: float = 0.0
        best_ratio: float = 0.0
        token_len = len(token)

        for candidate, weight in candidate_weights.items():
            cand_len = len(candidate)
            if cand_len < token_len - 2 or cand_len > token_len + 5:
                continue

            pref_len = _common_prefix_length(token, candidate)
            if pref_len == 0 and token_len < 6:
                continue

            # Distance sur le mot entier OU sur le préfixe de même longueur du candidat (ex: "dolidss" -> "doliprane")
            dist_full = _levenshtein_distance(token, candidate, max_dist=3)
            cand_prefix = candidate[:token_len]
            max_pref_dist = 3 if pref_len >= 4 else 2
            dist_pref = (
                _levenshtein_distance(token, cand_prefix, max_dist=max_pref_dist)
                if pref_len >= 3 and cand_len >= token_len
                else 99
            )

            if dist_full > 3 and dist_pref > max_pref_dist:
                continue

            ratio_full = SequenceMatcher(None, token, candidate).ratio()
            ratio_pref = (
                SequenceMatcher(None, token, cand_prefix).ratio()
                if pref_len >= 3 and cand_len >= token_len
                else 0.0
            )
            base_ratio = max(ratio_full, ratio_pref * 0.98)

            freq_bonus = min(weight, 120) * 0.0022
            score = base_ratio + (pref_len * 0.045) + freq_bonus
            if candidate.startswith(token[0]):
                score += 0.02

            if score > best_score:
                best_score = score
                best_ratio = base_ratio
                best_term = candidate

        if best_term and (best_score >= min_similarity or best_ratio >= 0.72):
            new_tokens.append(best_term)
            suggestions.append(
                {
                    "original": token,
                    "suggested": best_term,
                    "similarity_score": round(min(best_ratio, 1.0), 2),
                }
            )
        else:
            new_tokens.append(token)

    return new_tokens, suggestions


def _enrich_search_results(
    conn: sqlite3.Connection,
    spec_rows: list[sqlite3.Row],
    match_type: str,
) -> list[dict]:
    """Ajoute à chaque spécialité trouvée ses substances actives et dosages issus de
    `CIS_COMPO_bdpm.txt`, ses groupes génériques et rôles officiels (`CIS_GENER_bdpm.txt`),
    son statut de prescription (`CIS_CPD_bdpm.txt`) et le nombre d'informations ANSM disponibles.
    """
    if not spec_rows:
        return []

    cis_list = [r["code_cis"] for r in spec_rows]
    placeholders = ",".join("?" for _ in cis_list)

    compo_rows = conn.execute(
        f"""
        SELECT code_cis, designation_element, code_substance, denomination_substance, dosage, reference_dosage, nature_composant
        FROM compositions
        WHERE code_cis IN ({placeholders})
        ORDER BY nature_composant DESC, id ASC
        """,
        cis_list,
    ).fetchall()

    gener_rows = conn.execute(
        f"""
        SELECT code_cis, identifiant_groupe, libelle_groupe, type_generique
        FROM groupes_generiques
        WHERE code_cis IN ({placeholders})
        ORDER BY identifiant_groupe ASC
        """,
        cis_list,
    ).fetchall()

    cpd_rows = conn.execute(
        f"""
        SELECT code_cis, condition_prescription
        FROM conditions_prescription
        WHERE code_cis IN ({placeholders})
        """,
        cis_list,
    ).fetchall()

    info_rows = conn.execute(
        f"""
        SELECT code_cis, COUNT(*) AS ansm_count
        FROM infos_importantes
        WHERE code_cis IN ({placeholders})
        GROUP BY code_cis
        """,
        cis_list,
    ).fetchall()

    compo_by_cis: dict[str, list[dict]] = {cis: [] for cis in cis_list}
    for c in compo_rows:
        compo_by_cis[c["code_cis"]].append(
            {
                "designation_element": c["designation_element"],
                "code_substance": c["code_substance"],
                "substance": c["denomination_substance"],
                "dosage": c["dosage"],
                "reference_dosage": c["reference_dosage"],
                "nature_composant": c["nature_composant"],
            }
        )

    gener_by_cis: dict[str, list[dict]] = {cis: [] for cis in cis_list}
    for g in gener_rows:
        gener_by_cis[g["code_cis"]].append(
            {
                "identifiant_groupe": g["identifiant_groupe"],
                "libelle_groupe": g["libelle_groupe"],
                "type_generique_code": g["type_generique"],
                "type_generique_libelle": GENERIC_TYPE_LABELS.get(
                    g["type_generique"] or "",
                    g["type_generique"] or "Non précisé",
                ),
            }
        )

    prescription_by_cis: dict[str, bool] = {cis: False for cis in cis_list}
    for cpd in cpd_rows:
        if _is_prescription_condition_text(cpd["condition_prescription"]):
            prescription_by_cis[cpd["code_cis"]] = True

    ansm_info_by_cis = {r["code_cis"]: r["ansm_count"] for r in info_rows}

    results: list[dict] = []
    for row in spec_rows:
        cis = row["code_cis"]
        gen_list = gener_by_cis.get(cis, [])
        role_codes = {g["type_generique_code"] for g in gen_list if g["type_generique_code"]}
        has_princeps = PRINCEPS_TYPE_CODE in role_codes
        has_generic = any(code in GENERIC_MEMBER_TYPE_CODES for code in role_codes)
        if has_princeps and has_generic:
            generic_status = "mixed"
        elif has_princeps:
            generic_status = "princeps"
        elif has_generic:
            generic_status = "generic"
        elif gen_list:
            generic_status = "in_group"
        else:
            generic_status = "none"

        ansm_count = ansm_info_by_cis.get(cis, 0)
        compo_list = compo_by_cis.get(cis, [])
        sa_dosage_entries = _extract_official_sa_dosage_entries(compo_list)
        results.append(
            {
                "code_cis": cis,
                "denomination": row["denomination"],
                "forme_pharmaceutique": row["forme_pharmaceutique"],
                "voies_administration": row["voies_administration"],
                "etat_commercialisation": row["etat_commercialisation"],
                "statut_amm": row["statut_amm"],
                "titulaires": row["titulaires"],
                "surveillance_renforcee": row["surveillance_renforcee"],
                "official_fiche_url": row["official_fiche_url"],
                "compositions": compo_list,
                "official_sa_dosage_entries": sa_dosage_entries,
                "official_sa_dosages": _format_official_sa_dosages(sa_dosage_entries),
                "groupes_generiques": gen_list,
                "generic_status": generic_status,
                "has_princeps_role": has_princeps,
                "has_generic_role": has_generic,
                "is_prescription_required": prescription_by_cis.get(cis, False),
                "important_info_count": ansm_count,
                "ansm_info_count": ansm_count,
                "match_type": match_type,
                "source_file": (
                    "CIS_bdpm.txt + CIS_COMPO_bdpm.txt + CIS_GENER_bdpm.txt"
                    if gen_list
                    else "CIS_bdpm.txt + CIS_COMPO_bdpm.txt"
                ),
            }
        )
    return results


def get_medication_detail(
    code_cis: str,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict | None:
    """Récupère la fiche détaillée complète d'un médicament par son code CIS.

    Renvoie `None` si le code CIS n'existe pas dans la base SQLite.
    """
    cleaned_cis = (code_cis or "").strip()
    if not cleaned_cis:
        return None

    meta = get_db_metadata(db_path)
    if meta["ready"] != "true":
        return None

    with get_connection(db_path) as conn:
        spec = conn.execute(
            "SELECT * FROM specialites WHERE code_cis = ?",
            (cleaned_cis,),
        ).fetchone()
        if not spec:
            return None

        compositions = conn.execute(
            """
            SELECT designation_element, code_substance, denomination_substance,
                   dosage, reference_dosage, nature_composant, numero_liaison
            FROM compositions
            WHERE code_cis = ?
            ORDER BY numero_liaison ASC, nature_composant DESC, id ASC
            """,
            (cleaned_cis,),
        ).fetchall()

        presentations = conn.execute(
            """
            SELECT code_cip7, code_cip13, libelle_presentation,
                   statut_administratif, etat_commercialisation,
                   date_declaration_commercialisation, agrement_collectivites,
                   taux_remboursement, prix_hors_honoraire, prix_ttc,
                   honoraire_dispensation, indications_remboursement
            FROM presentations
            WHERE code_cis = ?
            ORDER BY etat_commercialisation ASC, code_cip13 ASC
            """,
            (cleaned_cis,),
        ).fetchall()

        conditions = conn.execute(
            """
            SELECT condition_prescription
            FROM conditions_prescription
            WHERE code_cis = ?
            ORDER BY id ASC
            """,
            (cleaned_cis,),
        ).fetchall()

        generiques = conn.execute(
            """
            SELECT identifiant_groupe, libelle_groupe, type_generique, numero_tri
            FROM groupes_generiques
            WHERE code_cis = ?
            ORDER BY identifiant_groupe ASC
            """,
            (cleaned_cis,),
        ).fetchall()

        # Récupération des autres spécialités appartenant au(x) même(s) groupe(s) générique(s)
        group_peers_by_id: dict[str, list[dict]] = {}
        for g in generiques:
            gid = g["identifiant_groupe"]
            peers = conn.execute(
                """
                SELECT g.code_cis, g.type_generique, s.denomination, s.etat_commercialisation
                FROM groupes_generiques g
                JOIN specialites s ON s.code_cis = g.code_cis
                WHERE g.identifiant_groupe = ? AND g.code_cis != ?
                ORDER BY CAST(COALESCE(g.numero_tri, '999') AS INTEGER) ASC, s.denomination ASC
                """,
                (gid, cleaned_cis),
            ).fetchall()
            group_peers_by_id[gid] = [
                {
                    "code_cis": p["code_cis"],
                    "denomination": p["denomination"],
                    "etat_commercialisation": p["etat_commercialisation"],
                    "type_generique_libelle": GENERIC_TYPE_LABELS.get(
                        p["type_generique"] or "",
                        p["type_generique"] or "Non précisé",
                    ),
                }
                for p in peers
            ]

        infos = conn.execute(
            """
            SELECT date_debut, date_fin, texte_information, lien_ansm
            FROM infos_importantes
            WHERE code_cis = ?
            ORDER BY date_debut DESC
            """,
            (cleaned_cis,),
        ).fetchall()

    return {
        "code_cis": spec["code_cis"],
        "denomination": spec["denomination"],
        "forme_pharmaceutique": spec["forme_pharmaceutique"],
        "voies_administration": (
            spec["voies_administration"].split(";")
            if spec["voies_administration"]
            else []
        ),
        "statut_amm": spec["statut_amm"],
        "type_procedure_amm": spec["type_procedure_amm"],
        "etat_commercialisation": spec["etat_commercialisation"],
        "date_amm": spec["date_amm"],
        "statut_bdm": spec["statut_bdm"],
        "numero_autorisation_europeenne": spec["numero_autorisation_europeenne"],
        "titulaires": spec["titulaires"],
        "surveillance_renforcee": spec["surveillance_renforcee"],
        "compositions": [
            {
                "designation_element": c["designation_element"],
                "code_substance": c["code_substance"],
                "denomination_substance": c["denomination_substance"],
                "dosage": c["dosage"],
                "reference_dosage": c["reference_dosage"],
                "nature_composant": c["nature_composant"],
                "nature_libelle": (
                    "Substance active (SA)"
                    if c["nature_composant"] == "SA"
                    else (
                        "Fraction thérapeutique (FT)"
                        if c["nature_composant"] == "FT"
                        else (c["nature_composant"] or "Non précisé")
                    )
                ),
                "numero_liaison": c["numero_liaison"],
                "source_file": "CIS_COMPO_bdpm.txt",
            }
            for c in compositions
        ],
        "presentations": [
            {
                "code_cip7": p["code_cip7"],
                "code_cip13": p["code_cip13"],
                "libelle_presentation": p["libelle_presentation"],
                "statut_administratif": p["statut_administratif"],
                "etat_commercialisation": p["etat_commercialisation"],
                "date_declaration_commercialisation": p["date_declaration_commercialisation"],
                "agrement_collectivites": p["agrement_collectivites"],
                "taux_remboursement": p["taux_remboursement"],
                "prix_hors_honoraire": p["prix_hors_honoraire"],
                "prix_ttc": p["prix_ttc"],
                "honoraire_dispensation": p["honoraire_dispensation"],
                "indications_remboursement": p["indications_remboursement"],
                "source_file": "CIS_CIP_bdpm.txt",
            }
            for p in presentations
        ],
        "conditions_prescription": [
            {
                "condition": c["condition_prescription"],
                "source_file": "CIS_CPD_bdpm.txt",
            }
            for c in conditions
        ],
        "groupes_generiques": [
            {
                "identifiant_groupe": g["identifiant_groupe"],
                "libelle_groupe": g["libelle_groupe"],
                "type_generique_code": g["type_generique"],
                "type_generique_libelle": GENERIC_TYPE_LABELS.get(
                    g["type_generique"] or "",
                    g["type_generique"] or "Non précisé",
                ),
                "autres_specialites": group_peers_by_id.get(g["identifiant_groupe"], []),
                "source_file": "CIS_GENER_bdpm.txt",
            }
            for g in generiques
        ],
        "infos_importantes": [
            {
                "date_debut": i["date_debut"],
                "date_fin": i["date_fin"],
                "texte_information": i["texte_information"],
                "lien_ansm": i["lien_ansm"],
                "source_file": "CIS_InfoImportantes.txt",
            }
            for i in infos
        ],
        "external_official_links": {
            "fiche_bdpm_url": spec["official_fiche_url"],
            "note": (
                "Le Résumé des Caractéristiques du Produit (RCP) et la notice patient "
                "ne figurent pas dans les fichiers tabulaires bruts de la BDPM. "
                "Ils sont consultables directement sur la fiche officielle ci-dessus."
            ),
        },
        "data_limitations": [
            "Les fichiers téléchargeables de la BDPM ne contiennent ni la posologie détaillée, ni la liste complète des contre-indications, effets indésirables ou interactions médicamenteuses.",
            "Ces informations cliniques figurent dans la notice patient et le RCP accessibles via le lien officiel BDPM ci-dessus.",
            "Cette fiche présente uniquement les données administratives et pharmaceutiques publiques sans interprétation médicale personnalisée.",
        ],
        "provenance": {
            "source_name": meta["source_name"],
            "source_url": meta["source_url"],
            "official_bdpm_date": meta["official_bdpm_date"],
            "imported_at_utc": meta["imported_at_utc"],
            "primary_file": "CIS_bdpm.txt",
        },
        "is_prescription_required": any(
            _is_prescription_condition_text(c["condition_prescription"])
            for c in conditions
        ),
    }


def list_active_substances(
    db_path: Path | str = DEFAULT_DB_PATH,
    *,
    query: str | None = None,
    page: int = 1,
    per_page: int = 30,
) -> dict:
    """Liste les substances actives officielles (nature SA uniquement), regroupées
    strictement par `code_substance` officiel avec pagination cohérente.

    Les différentes appellations officielles d'un même `code_substance` sont regroupées
    sur une seule entrée afin d'éviter tout doublon.
    """
    meta = get_db_metadata(db_path)
    raw_query = (query or "").strip()
    norm_query = normalize_text(raw_query)

    if meta["ready"] != "true":
        pag = _build_pagination(0, page, per_page)
        return {
            "query": raw_query,
            "status": "database_empty",
            "count": 0,
            "total_count": 0,
            "pagination": pag,
            "results": [],
            "metadata": meta,
        }

    with get_connection(db_path) as conn:
        # Sélection des codes substances actives (SA) correspondant à la recherche éventuelle
        if norm_query:
            like_pat = f"%{norm_query}%"
            code_rows = conn.execute(
                """
                SELECT DISTINCT code_substance
                FROM compositions
                WHERE nature_composant = 'SA'
                  AND code_substance IS NOT NULL
                  AND TRIM(code_substance) != ''
                  AND (
                      substance_normalisee LIKE ?
                      OR code_substance = ?
                  )
                """,
                (like_pat, raw_query),
            ).fetchall()
            matching_codes = [r["code_substance"] for r in code_rows]
            if not matching_codes:
                pag = _build_pagination(0, page, per_page)
                return {
                    "query": raw_query,
                    "status": "no_results",
                    "count": 0,
                    "total_count": 0,
                    "pagination": pag,
                    "results": [],
                    "metadata": meta,
                }
            placeholders = ",".join("?" for _ in matching_codes)
            agg_rows = conn.execute(
                f"""
                SELECT
                    c.code_substance,
                    COUNT(DISTINCT c.code_cis) AS specialites_count,
                    COUNT(DISTINCT CASE WHEN s.etat_commercialisation = 'Commercialisée' THEN c.code_cis END) AS commercialisees_count,
                    MIN(c.substance_normalisee) AS sort_key
                FROM compositions c
                JOIN specialites s ON s.code_cis = c.code_cis
                WHERE c.nature_composant = 'SA'
                  AND c.code_substance IN ({placeholders})
                GROUP BY c.code_substance
                ORDER BY specialites_count DESC, sort_key ASC
                """,
                matching_codes,
            ).fetchall()
        else:
            agg_rows = conn.execute(
                """
                SELECT
                    c.code_substance,
                    COUNT(DISTINCT c.code_cis) AS specialites_count,
                    COUNT(DISTINCT CASE WHEN s.etat_commercialisation = 'Commercialisée' THEN c.code_cis END) AS commercialisees_count,
                    MIN(c.substance_normalisee) AS sort_key
                FROM compositions c
                JOIN specialites s ON s.code_cis = c.code_cis
                WHERE c.nature_composant = 'SA'
                  AND c.code_substance IS NOT NULL
                  AND TRIM(c.code_substance) != ''
                GROUP BY c.code_substance
                ORDER BY specialites_count DESC, sort_key ASC
                """
            ).fetchall()

        total_count = len(agg_rows)
        pag = _build_pagination(total_count, page, per_page)
        if total_count == 0:
            return {
                "query": raw_query,
                "status": "no_results",
                "count": 0,
                "total_count": 0,
                "pagination": pag,
                "results": [],
                "metadata": meta,
            }
        if pag["page_out_of_range"]:
            return {
                "query": raw_query,
                "status": "page_out_of_range",
                "warning": (
                    f"La page {pag['page']} n'existe pas : cette liste de substances actives "
                    f"comporte {pag['total_pages']} page(s) ({total_count} substance(s) au total)."
                ),
                "count": 0,
                "total_count": total_count,
                "pagination": pag,
                "results": [],
                "metadata": meta,
            }

        page_rows = agg_rows[pag["offset"] : pag["offset"] + pag["per_page"]]
        page_codes = [r["code_substance"] for r in page_rows]
        page_placeholders = ",".join("?" for _ in page_codes)

        # Récupération de toutes les appellations officielles pour les codes de cette page
        label_rows = conn.execute(
            f"""
            SELECT code_substance, denomination_substance, COUNT(*) AS freq
            FROM compositions
            WHERE nature_composant = 'SA'
              AND code_substance IN ({page_placeholders})
            GROUP BY code_substance, denomination_substance
            ORDER BY code_substance ASC, freq DESC, denomination_substance ASC
            """,
            page_codes,
        ).fetchall()

        labels_by_code: dict[str, list[str]] = {code: [] for code in page_codes}
        for lr in label_rows:
            labels_by_code[lr["code_substance"]].append(lr["denomination_substance"])

        results: list[dict] = []
        for r in page_rows:
            code = r["code_substance"]
            all_labels = labels_by_code.get(code, [])
            main_label = all_labels[0] if all_labels else f"Substance {code}"
            other_labels = all_labels[1:] if len(all_labels) > 1 else []
            results.append(
                {
                    "code_substance": code,
                    "denomination_principale": main_label,
                    "autres_denominations": other_labels,
                    "specialites_count": r["specialites_count"],
                    "commercialisees_count": r["commercialisees_count"],
                    "source_file": "CIS_COMPO_bdpm.txt",
                }
            )

    return {
        "query": raw_query,
        "status": "ok",
        "count": len(results),
        "total_count": total_count,
        "pagination": pag,
        "results": results,
        "metadata": meta,
    }


def get_substance_detail(
    code_substance: str,
    db_path: Path | str = DEFAULT_DB_PATH,
    *,
    page: int = 1,
    per_page: int = 20,
    commercialise_only: bool = False,
) -> dict | None:
    """Récupère la page détaillée d'une substance active officielle (nature SA uniquement)
    identifiée par son `code_substance`, en regroupant toutes ses appellations officielles
    et en listant sans doublon les spécialités associées.
    """
    clean_code = (code_substance or "").strip()
    if not clean_code:
        return None

    meta = get_db_metadata(db_path)
    if meta["ready"] != "true":
        return None

    with get_connection(db_path) as conn:
        label_rows = conn.execute(
            """
            SELECT denomination_substance, COUNT(*) AS freq
            FROM compositions
            WHERE nature_composant = 'SA' AND code_substance = ?
            GROUP BY denomination_substance
            ORDER BY freq DESC, denomination_substance ASC
            """,
            (clean_code,),
        ).fetchall()
        if not label_rows:
            return None

        all_labels = [r["denomination_substance"] for r in label_rows]
        main_label = all_labels[0]
        other_labels = all_labels[1:]

        where_clauses = [
            "EXISTS (SELECT 1 FROM compositions c WHERE c.code_cis = s.code_cis AND c.code_substance = ? AND c.nature_composant = 'SA')"
        ]
        params: list[str | int] = [clean_code]

        if commercialise_only:
            where_clauses.append("s.etat_commercialisation = ?")
            params.append("Commercialisée")

        where_sql = " AND ".join(where_clauses)
        total_count = conn.execute(
            f"SELECT COUNT(*) AS cnt FROM specialites s WHERE {where_sql}",
            params,
        ).fetchone()["cnt"]

        total_all_specialites = conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM specialites s
            WHERE EXISTS (
                SELECT 1 FROM compositions c
                WHERE c.code_cis = s.code_cis
                  AND c.code_substance = ?
                  AND c.nature_composant = 'SA'
            )
            """,
            (clean_code,),
        ).fetchone()["cnt"]

        pag = _build_pagination(total_count, page, per_page)
        enriched: list[dict] = []
        status = "ok"
        warning = None

        if total_count == 0:
            status = "no_results"
            warning = (
                "Aucune spécialité commercialisée n'est actuellement référencée pour cette substance active."
                if commercialise_only
                else "Aucune spécialité associée trouvée."
            )
        elif pag["page_out_of_range"]:
            status = "page_out_of_range"
            warning = (
                f"La page {pag['page']} n'existe pas : cette substance comporte "
                f"{pag['total_pages']} page(s) pour {total_count} spécialité(s)."
            )
        else:
            spec_rows = conn.execute(
                f"""
                SELECT s.*
                FROM specialites s
                WHERE {where_sql}
                ORDER BY
                    CASE WHEN s.etat_commercialisation = 'Commercialisée' THEN 0 ELSE 1 END ASC,
                    s.denomination ASC
                LIMIT ? OFFSET ?
                """,
                [*params, pag["per_page"], pag["offset"]],
            ).fetchall()
            enriched = _enrich_search_results(conn, spec_rows, match_type="exacte_ou_normalisee")

    return {
        "code_substance": clean_code,
        "denomination_principale": main_label,
        "autres_denominations": other_labels,
        "nature_composant": "SA",
        "total_all_specialites": total_all_specialites,
        "commercialise_only": bool(commercialise_only),
        "status": status,
        "warning": warning,
        "count": len(enriched),
        "total_count": total_count,
        "pagination": pag,
        "results": enriched,
        "safety_note": (
            "Le partage d'une même substance active (code officiel SA) ne signifie pas "
            "automatiquement que deux médicaments sont médicalement substituables : "
            "la forme pharmaceutique, le dosage, les excipients ou les indications peuvent différer."
        ),
        "source_file": "CIS_COMPO_bdpm.txt + CIS_bdpm.txt",
        "metadata": meta,
    }


def get_generic_group_detail(
    identifiant_groupe: str,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict | None:
    """Récupère la page complète d'un groupe générique officiel (`CIS_GENER_bdpm.txt`),
    en séparant explicitement les spécialités de référence (princeps) et les médicaments
    génériques, sans aucun doublon par code CIS.
    """
    clean_id = (identifiant_groupe or "").strip()
    if not clean_id:
        return None

    meta = get_db_metadata(db_path)
    if meta["ready"] != "true":
        return None

    with get_connection(db_path) as conn:
        member_rows = conn.execute(
            """
            SELECT
                g.identifiant_groupe,
                g.libelle_groupe,
                g.code_cis,
                g.type_generique,
                g.numero_tri
            FROM groupes_generiques g
            WHERE g.identifiant_groupe = ?
            ORDER BY
                CASE WHEN g.type_generique = '0' THEN 0 ELSE 1 END ASC,
                CAST(COALESCE(g.numero_tri, '9999') AS INTEGER) ASC,
                g.code_cis ASC
            """,
            (clean_id,),
        ).fetchall()
        if not member_rows:
            return None

        libelle_groupe = member_rows[0]["libelle_groupe"]

        # Déduplication par code_cis tout en conservant tous les rôles officiels du groupe
        roles_by_cis: dict[str, list[dict]] = {}
        ordered_cis: list[str] = []
        for m in member_rows:
            cis = m["code_cis"]
            if cis not in roles_by_cis:
                roles_by_cis[cis] = []
                ordered_cis.append(cis)
            code = m["type_generique"] or ""
            role_entry = {
                "type_generique_code": code,
                "type_generique_libelle": GENERIC_TYPE_LABELS.get(code, code or "Non précisé"),
            }
            if role_entry not in roles_by_cis[cis]:
                roles_by_cis[cis].append(role_entry)

        placeholders = ",".join("?" for _ in ordered_cis)
        spec_rows_raw = conn.execute(
            f"SELECT * FROM specialites WHERE code_cis IN ({placeholders})",
            ordered_cis,
        ).fetchall()
        spec_by_cis = {r["code_cis"]: r for r in spec_rows_raw}
        ordered_spec_rows = [spec_by_cis[cis] for cis in ordered_cis if cis in spec_by_cis]
        enriched_list = _enrich_search_results(conn, ordered_spec_rows, match_type="exacte_ou_normalisee")

        princeps: list[dict] = []
        generics: list[dict] = []
        other_members: list[dict] = []

        for item in enriched_list:
            cis = item["code_cis"]
            group_roles = roles_by_cis.get(cis, [])
            role_codes = {r["type_generique_code"] for r in group_roles}
            item["group_roles"] = group_roles
            item["group_role_label"] = " / ".join(r["type_generique_libelle"] for r in group_roles)

            if PRINCEPS_TYPE_CODE in role_codes:
                princeps.append(item)
            elif any(c in GENERIC_MEMBER_TYPE_CODES for c in role_codes):
                generics.append(item)
            else:
                other_members.append(item)

    return {
        "identifiant_groupe": clean_id,
        "libelle_groupe": libelle_groupe,
        "princeps": princeps,
        "generics": generics,
        "other_members": other_members,
        "princeps_count": len(princeps),
        "generics_count": len(generics),
        "total_members": len(princeps) + len(generics) + len(other_members),
        "source_file": "CIS_GENER_bdpm.txt + CIS_bdpm.txt",
        "metadata": meta,
    }


def list_catalogue_medications(
    db_path: Path | str = DEFAULT_DB_PATH,
    *,
    letter: str = "A",
    query: str | None = None,
    page: int = 1,
    per_page: int = 20,
    commercialise_only: bool = False,
) -> dict:
    """Parcourt le catalogue alphabétique A–Z (et 0-9 / Autres) des spécialités BDPM :
    - compteurs A–Z réels depuis SQLite, adaptés au filtre `commercialise_only` et jamais
      faussés par la recherche interne `query` ;
    - recherche interne confinée strictement à la lettre sélectionnée (insensible aux accents,
      à la casse et aux espaces superflus, avec protection SQLite `LIKE ... ESCAPE '\\'`
      contre `%`, `_` et `\\`) ;
    - pagination SQL réelle (20 par page par défaut) ramenant toute page inexistante
      (> total_pages) vers la dernière page disponible tout en conservant filtres et recherche.
    """
    meta = get_db_metadata(db_path)
    alphabet = [chr(c) for c in range(ord("A"), ord("Z") + 1)]
    raw_letter = (letter or "A").strip().upper()
    if raw_letter in alphabet:
        selected_letter = raw_letter
    elif raw_letter in ("0-9", "AUTRES", "#"):
        selected_letter = "0-9"
    else:
        selected_letter = "A"

    raw_query = (query or "").strip()
    requested_page = max(1, int(page))

    if meta["ready"] != "true":
        pag = _build_pagination(0, requested_page, per_page)
        pag["requested_page"] = requested_page
        pag["page_clamped"] = False
        return {
            "letter": selected_letter,
            "query": raw_query,
            "letters": [{"letter": ch, "count": 0} for ch in alphabet] + [{"letter": "0-9", "count": 0}],
            "commercialise_only": bool(commercialise_only),
            "status": "database_empty",
            "warning": "La base de données locale ne contient encore aucun médicament importé.",
            "notice": None,
            "count": 0,
            "total_count": 0,
            "letter_total_count": 0,
            "pagination": pag,
            "results": [],
            "metadata": meta,
        }

    with get_connection(db_path) as conn:
        comm_where = "WHERE etat_commercialisation = ?" if commercialise_only else ""
        comm_params: list[str] = ["Commercialisée"] if commercialise_only else []

        # Compteurs généraux par lettre initiale (dépendent uniquement de commercialise_only,
        # jamais faussés par la recherche interne `query`).
        letter_rows = conn.execute(
            f"""
            SELECT
                CASE
                    WHEN UPPER(SUBSTR(denomination_normalisee, 1, 1)) BETWEEN 'A' AND 'Z'
                    THEN UPPER(SUBSTR(denomination_normalisee, 1, 1))
                    ELSE '0-9'
                END AS initial_bucket,
                COUNT(*) AS cnt
            FROM specialites
            {comm_where}
            GROUP BY initial_bucket
            """,
            comm_params,
        ).fetchall()
        counts_by_letter = {r["initial_bucket"]: r["cnt"] for r in letter_rows}
        letters_index = [
            {"letter": ch, "count": counts_by_letter.get(ch, 0)}
            for ch in [*alphabet, "0-9"]
        ]
        letter_total_count = counts_by_letter.get(selected_letter, 0)

        where_clauses: list[str] = []
        params: list[str | int] = []
        if commercialise_only:
            where_clauses.append("s.etat_commercialisation = ?")
            params.append("Commercialisée")

        # Contrainte stricte de lettre initiale (toujours appliquée, même avec une recherche interne)
        if selected_letter == "0-9":
            where_clauses.append("UPPER(SUBSTR(s.denomination_normalisee, 1, 1)) NOT BETWEEN 'A' AND 'Z'")
        else:
            where_clauses.append("UPPER(SUBSTR(s.denomination_normalisee, 1, 1)) = ?")
            params.append(selected_letter)

        # Recherche interne sécurisée (accents, casse, espaces superflus et échappement de %, _, \)
        if raw_query:
            has_alnum = any(ch.isalnum() for ch in raw_query)
            if not has_alnum:
                # Caractères spéciaux seuls (ex. '%', '_', '___') : comparaison littérale échappée
                escaped_raw = f"%{_escape_like_pattern(raw_query.lower())}%"
                where_clauses.append(
                    "(LOWER(s.denomination) LIKE ? ESCAPE '\\' OR LOWER(COALESCE(s.forme_pharmaceutique, '')) LIKE ? ESCAPE '\\')"
                )
                params.extend([escaped_raw, escaped_raw])
            else:
                tokens = tokenize_query(raw_query)
                # Si la saisie contient un joker SQL brut ('%' ou '_') mélangé à des lettres (ex. 'abc_def'),
                # on exige aussi une correspondance échappée littérale sur les champs bruts si aucun espace n'était là.
                if "_" in raw_query:
                    escaped_literal = f"%{_escape_like_pattern(raw_query.lower())}%"
                    where_clauses.append(
                        """
                        (
                            LOWER(s.denomination) LIKE ? ESCAPE '\\'
                            OR LOWER(COALESCE(s.forme_pharmaceutique, '')) LIKE ? ESCAPE '\\'
                            OR EXISTS (
                                SELECT 1 FROM compositions c
                                WHERE c.code_cis = s.code_cis
                                  AND LOWER(c.denomination_substance) LIKE ? ESCAPE '\\'
                            )
                        )
                        """
                    )
                    params.extend([escaped_literal, escaped_literal, escaped_literal])
                else:
                    for tok in tokens:
                        escaped_tok = _escape_like_pattern(tok)
                        like_pat = f"%{escaped_tok}%"
                        where_clauses.append(
                            """
                            (
                                s.denomination_normalisee LIKE ? ESCAPE '\\'
                                OR s.forme_normalisee LIKE ? ESCAPE '\\'
                                OR s.code_cis = ?
                                OR EXISTS (
                                    SELECT 1 FROM compositions c
                                    WHERE c.code_cis = s.code_cis
                                      AND (
                                          c.substance_normalisee LIKE ? ESCAPE '\\'
                                          OR c.dosage_normalise LIKE ? ESCAPE '\\'
                                      )
                                )
                            )
                            """
                        )
                        params.extend([like_pat, like_pat, tok, like_pat, like_pat])

        where_sql = " AND ".join(where_clauses)
        total_count = conn.execute(
            f"SELECT COUNT(*) AS cnt FROM specialites s WHERE {where_sql}",
            params,
        ).fetchone()["cnt"]

        safe_per_page = max(1, min(int(per_page), 100))
        total_pages = math.ceil(total_count / safe_per_page) if total_count > 0 else 0
        page_clamped = total_count > 0 and requested_page > total_pages
        effective_page = total_pages if page_clamped else (1 if total_count == 0 else requested_page)

        pag = _build_pagination(total_count, effective_page, safe_per_page)
        pag["requested_page"] = requested_page
        pag["page_clamped"] = page_clamped

        enriched: list[dict] = []
        status = "ok"
        warning = None
        notice = None

        if total_count == 0:
            status = "no_results"
            if raw_query:
                warning = (
                    f"Aucune spécialité commençant par « {selected_letter} » ne correspond à « {raw_query} »"
                    + (" parmi les spécialités commercialisées." if commercialise_only else ".")
                )
            else:
                warning = f"Aucune spécialité référencée pour l'initiale « {selected_letter} » avec ce filtre."
        else:
            if page_clamped:
                notice = (
                    f"La page {requested_page} n'existe pas pour cette sélection : "
                    f"affichage automatique de la dernière page disponible (page {effective_page} sur {total_pages})."
                )
            spec_rows = conn.execute(
                f"""
                SELECT s.*
                FROM specialites s
                WHERE {where_sql}
                ORDER BY
                    CASE WHEN s.etat_commercialisation = 'Commercialisée' THEN 0 ELSE 1 END ASC,
                    s.denomination ASC
                LIMIT ? OFFSET ?
                """,
                [*params, pag["per_page"], pag["offset"]],
            ).fetchall()
            enriched = _enrich_search_results(conn, spec_rows, match_type="exacte_ou_normalisee")

    return {
        "letter": selected_letter,
        "query": raw_query,
        "letters": letters_index,
        "commercialise_only": bool(commercialise_only),
        "status": status,
        "warning": warning,
        "notice": notice,
        "count": len(enriched),
        "total_count": total_count,
        "letter_total_count": letter_total_count,
        "pagination": pag,
        "results": enriched,
        "metadata": meta,
    }
