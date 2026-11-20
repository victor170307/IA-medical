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

# Cache mémoire des termes candidats (mot -> poids de fréquence) pour accélérer la tolérance orthographique
_FUZZY_CANDIDATES_CACHE: dict[tuple[int, int], dict[str, int]] = {}


def get_db_metadata(db_path: Path | str = DEFAULT_DB_PATH) -> dict[str, str]:
    """Récupère les métadonnées d'importation et de provenance stockées dans SQLite."""
    path_obj = Path(db_path)
    if not path_obj.exists():
        return {
            "ready": "false",
            "specialites_count": "0",
            "official_bdpm_date": "Base non initialisée",
            "imported_at_utc": "Aucun import effectué",
            "source_name": BDPM_SOURCE_NAME,
            "source_url": BDPM_BASE_URL,
        }

    init_db(path_obj)
    with get_connection(path_obj) as conn:
        rows = conn.execute("SELECT key, value FROM import_metadata;").fetchall()
        meta = {row["key"]: row["value"] for row in rows}
        count_row = conn.execute("SELECT COUNT(*) AS cnt FROM specialites;").fetchone()
        actual_count = count_row["cnt"] if count_row else 0

    return {
        "ready": "true" if actual_count > 0 else "false",
        "specialites_count": str(actual_count),
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
) -> dict:
    """Recherche des médicaments dans la base SQLite BDPM.

    Étapes :
    1. Si la requête est vide -> retourne immédiatement 0 résultat avec `status='empty_query'`.
    2. Recherche exacte/normalisée sur le code CIS, le code CIP13/CIP7, la dénomination,
       les substances actives, les dosages, la forme pharmaceutique, les voies d'administration,
       les titulaires, les présentations et les groupes génériques (`CIS_GENER_bdpm.txt`).
    3. Si aucun résultat exact/normalisé n'est trouvé et que `allow_fuzzy=True`,
       effectue une recherche tolérante aux fautes de frappe simples sur les noms
       de marques, de groupes génériques et de substances actives, en signalant
       explicitement qu'il s'agit d'une suggestion orthographique à vérifier.
    """
    raw_query = (query or "").strip()
    tokens = tokenize_query(raw_query)
    meta = get_db_metadata(db_path)

    if not tokens:
        return {
            "query": raw_query,
            "normalized_query": "",
            "status": "empty_query",
            "match_mode": "none",
            "warning": None,
            "suggested_terms": [],
            "count": 0,
            "results": [],
            "metadata": meta,
        }

    if meta["ready"] != "true":
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
            "results": [],
            "metadata": meta,
        }

    with get_connection(db_path) as conn:
        exact_rows = _execute_normalized_search(conn, tokens, limit=limit)
        if exact_rows:
            enriched = _enrich_search_results(conn, exact_rows, match_type="exacte_ou_normalisee")
            return {
                "query": raw_query,
                "normalized_query": " ".join(tokens),
                "status": "ok",
                "match_mode": "exact_or_normalized",
                "warning": None,
                "suggested_terms": [],
                "count": len(enriched),
                "results": enriched,
                "metadata": meta,
            }

        # Aucune correspondance exacte/normalisée : tentative de tolérance orthographique
        if allow_fuzzy:
            fuzzy_tokens, suggested_terms = _find_fuzzy_token_replacements(conn, tokens)
            if suggested_terms and fuzzy_tokens != tokens:
                fuzzy_rows = _execute_normalized_search(conn, fuzzy_tokens, limit=limit)
                if fuzzy_rows:
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
                        "results": enriched_fuzzy,
                        "metadata": meta,
                    }

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
        "results": [],
        "metadata": meta,
    }


def _execute_normalized_search(
    conn: sqlite3.Connection,
    tokens: list[str],
    limit: int = 30,
) -> list[sqlite3.Row]:
    """Exécute une recherche SQL où chaque mot-clé de la requête doit être présent
    soit dans le code CIS, le code CIP13/CIP7, la dénomination, la forme pharmaceutique,
    la voie d'administration, le titulaire, la substance active, le dosage, le libellé de
    présentation ou le groupe générique (`CIS_GENER_bdpm.txt`) de la spécialité.
    """
    normalized_full = " ".join(tokens)

    # Cas 1 : Recherche directe par Code CIS (8 chiffres), CIP7 (7 chiffres) ou CIP13 (13 chiffres)
    if len(tokens) == 1 and re.match(r"^\d{8}$", tokens[0]):
        return conn.execute(
            """
            SELECT * FROM specialites
            WHERE code_cis = ?
            LIMIT ?
            """,
            (tokens[0], limit),
        ).fetchall()

    if len(tokens) == 1 and re.match(r"^\d{7}$", tokens[0]):
        return conn.execute(
            """
            SELECT DISTINCT s.*
            FROM specialites s
            JOIN presentations p ON p.code_cis = s.code_cis
            WHERE p.code_cip7 = ?
            LIMIT ?
            """,
            (tokens[0], limit),
        ).fetchall()

    if len(tokens) == 1 and re.match(r"^\d{13}$", tokens[0]):
        return conn.execute(
            """
            SELECT DISTINCT s.*
            FROM specialites s
            JOIN presentations p ON p.code_cis = s.code_cis
            WHERE p.code_cip13 = ?
            LIMIT ?
            """,
            (tokens[0], limit),
        ).fetchall()

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
                    OR s.code_cis IN (
                        SELECT c.code_cis FROM compositions c
                        WHERE (' ' || c.dosage_normalise || ' ') LIKE ?
                    )
                    OR s.code_cis IN (
                        SELECT g.code_cis FROM groupes_generiques g
                        WHERE (' ' || g.libelle_groupe_normalise || ' ') LIKE ?
                           OR g.identifiant_groupe = ?
                    )
                    OR s.code_cis IN (
                        SELECT p.code_cis FROM presentations p
                        WHERE p.code_cip13 = ?
                           OR p.code_cip7 = ?
                           OR (' ' || COALESCE(p.libelle_presentation, '') || ' ') LIKE ?
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
                    OR s.code_cis IN (
                        SELECT c.code_cis FROM compositions c
                        WHERE c.substance_normalisee LIKE ?
                           OR c.dosage_normalise LIKE ?
                    )
                    OR s.code_cis IN (
                        SELECT g.code_cis FROM groupes_generiques g
                        WHERE g.libelle_groupe_normalise LIKE ?
                    )
                    OR s.code_cis IN (
                        SELECT p.code_cis FROM presentations p
                        WHERE REPLACE(REPLACE(REPLACE(REPLACE(LOWER(COALESCE(p.libelle_presentation, '')), 'é', 'e'), 'è', 'e'), 'ê', 'e'), 'î', 'i') LIKE ?
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
        WHERE {" AND ".join(where_clauses)}
        ORDER BY
            rank_priority ASC,
            CASE WHEN s.etat_commercialisation = 'Commercialisée' THEN 0 ELSE 1 END ASC,
            s.denomination ASC
        LIMIT ?
    """
    first_token = tokens[0]
    prefix_pat = f"{normalized_full}%"
    first_contains_pat = f"%{first_token}%"
    final_params: list[str | int] = [
        normalized_full,
        first_token,
        prefix_pat,
        first_contains_pat,
        *params,
        limit,
    ]
    return conn.execute(sql, final_params).fetchall()


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
    `CIS_COMPO_bdpm.txt`, ses groupes génériques issus de `CIS_GENER_bdpm.txt` ainsi
    que le nombre d'alertes ANSM éventuelles.
    """
    if not spec_rows:
        return []

    cis_list = [r["code_cis"] for r in spec_rows]
    placeholders = ",".join("?" for _ in cis_list)

    compo_rows = conn.execute(
        f"""
        SELECT code_cis, denomination_substance, dosage, reference_dosage, nature_composant
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

    info_rows = conn.execute(
        f"""
        SELECT code_cis, COUNT(*) AS alert_count
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

    alerts_by_cis = {r["code_cis"]: r["alert_count"] for r in info_rows}

    results: list[dict] = []
    for row in spec_rows:
        cis = row["code_cis"]
        gen_list = gener_by_cis.get(cis, [])
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
                "compositions": compo_by_cis.get(cis, []),
                "groupes_generiques": gen_list,
                "important_info_count": alerts_by_cis.get(cis, 0),
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
    }
