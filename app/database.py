"""Gestion de la base de données SQLite et création du schéma relationnel BDPM.

Toutes les tables sont reliées par l'identifiant officiel `code_cis` (Code Identifiant
de Spécialité à 8 chiffres défini par les autorités de santé françaises).
Aucune relation n'est créée par simple ressemblance de noms.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
import sqlite3

from app.config import DEFAULT_DB_PATH

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS import_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS specialites (
    code_cis TEXT PRIMARY KEY,
    denomination TEXT NOT NULL,
    denomination_normalisee TEXT NOT NULL,
    marque_principale_normalisee TEXT NOT NULL,
    forme_pharmaceutique TEXT,
    forme_normalisee TEXT,
    voies_administration TEXT,
    statut_amm TEXT,
    type_procedure_amm TEXT,
    etat_commercialisation TEXT,
    date_amm TEXT,
    statut_bdm TEXT,
    numero_autorisation_europeenne TEXT,
    titulaires TEXT,
    surveillance_renforcee TEXT,
    official_fiche_url TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS compositions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code_cis TEXT NOT NULL,
    designation_element TEXT,
    code_substance TEXT,
    denomination_substance TEXT NOT NULL,
    substance_normalisee TEXT NOT NULL,
    dosage TEXT,
    dosage_normalise TEXT,
    reference_dosage TEXT,
    nature_composant TEXT,
    numero_liaison TEXT,
    FOREIGN KEY (code_cis) REFERENCES specialites(code_cis) ON DELETE CASCADE,
    UNIQUE(
        code_cis,
        designation_element,
        code_substance,
        denomination_substance,
        dosage,
        reference_dosage,
        nature_composant,
        numero_liaison
    )
);

CREATE TABLE IF NOT EXISTS presentations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code_cis TEXT NOT NULL,
    code_cip7 TEXT,
    libelle_presentation TEXT,
    statut_administratif TEXT,
    etat_commercialisation TEXT,
    date_declaration_commercialisation TEXT,
    code_cip13 TEXT,
    agrement_collectivites TEXT,
    taux_remboursement TEXT,
    prix_hors_honoraire TEXT,
    prix_ttc TEXT,
    honoraire_dispensation TEXT,
    indications_remboursement TEXT,
    FOREIGN KEY (code_cis) REFERENCES specialites(code_cis) ON DELETE CASCADE,
    UNIQUE(code_cis, code_cip7, code_cip13)
);

CREATE TABLE IF NOT EXISTS conditions_prescription (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code_cis TEXT NOT NULL,
    condition_prescription TEXT NOT NULL,
    FOREIGN KEY (code_cis) REFERENCES specialites(code_cis) ON DELETE CASCADE,
    UNIQUE(code_cis, condition_prescription)
);

CREATE TABLE IF NOT EXISTS groupes_generiques (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    identifiant_groupe TEXT NOT NULL,
    libelle_groupe TEXT NOT NULL,
    libelle_groupe_normalise TEXT NOT NULL DEFAULT '',
    code_cis TEXT NOT NULL,
    type_generique TEXT,
    numero_tri TEXT,
    FOREIGN KEY (code_cis) REFERENCES specialites(code_cis) ON DELETE CASCADE,
    UNIQUE(identifiant_groupe, code_cis, type_generique)
);

CREATE TABLE IF NOT EXISTS infos_importantes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code_cis TEXT NOT NULL,
    date_debut TEXT,
    date_fin TEXT,
    texte_information TEXT NOT NULL,
    lien_ansm TEXT,
    FOREIGN KEY (code_cis) REFERENCES specialites(code_cis) ON DELETE CASCADE,
    UNIQUE(code_cis, date_debut, date_fin, texte_information)
);

CREATE INDEX IF NOT EXISTS idx_specialites_denom_norm ON specialites(denomination_normalisee);
CREATE INDEX IF NOT EXISTS idx_specialites_marque_norm ON specialites(marque_principale_normalisee);
CREATE INDEX IF NOT EXISTS idx_specialites_forme_norm ON specialites(forme_normalisee);
CREATE INDEX IF NOT EXISTS idx_compositions_cis ON compositions(code_cis);
CREATE INDEX IF NOT EXISTS idx_compositions_subst_norm ON compositions(substance_normalisee);
CREATE INDEX IF NOT EXISTS idx_presentations_cis ON presentations(code_cis);
CREATE INDEX IF NOT EXISTS idx_presentations_cip7 ON presentations(code_cip7);
CREATE INDEX IF NOT EXISTS idx_presentations_cip13 ON presentations(code_cip13);
CREATE INDEX IF NOT EXISTS idx_cpd_cis ON conditions_prescription(code_cis);
CREATE INDEX IF NOT EXISTS idx_gener_cis ON groupes_generiques(code_cis);
CREATE INDEX IF NOT EXISTS idx_gener_groupe ON groupes_generiques(identifiant_groupe);
CREATE INDEX IF NOT EXISTS idx_infos_cis ON infos_importantes(code_cis);
"""


@contextmanager
def get_connection(db_path: Path | str = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    """Ouvre une connexion SQLite avec accès aux colonnes par nom (`sqlite3.Row`),
    active les clés étrangères (`PRAGMA foreign_keys = ON`) et garantit la fermeture
    explicite du fichier (`conn.close()`) sous Windows à la sortie du bloc `with`.
    """
    path_obj = Path(db_path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path_obj))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
    finally:
        conn.close()


_INITIALIZED_DBS: set[tuple[str, int]] = set()


def init_db(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Crée les tables et les index SQLite s'ils n'existent pas encore,
    et met à jour automatiquement la colonne `libelle_groupe_normalise` si la base
    existait déjà d'une version antérieure.
    Ne modifie jamais le fichier sur le disque si le schéma est déjà à jour (évite
    tout rechargement intempestif de VS Code Live Server).
    """
    from app.search.normalizer import normalize_text

    path_obj = Path(db_path).resolve()
    if path_obj.exists():
        cache_key = (str(path_obj), path_obj.stat().st_mtime_ns)
        if cache_key in _INITIALIZED_DBS:
            return

        # Vérification rapide en lecture seule : si le schéma est déjà complet, aucun écriture disque
        try:
            with get_connection(path_obj) as conn:
                tables = {
                    r["name"]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table';"
                    ).fetchall()
                }
                required = {
                    "import_metadata",
                    "specialites",
                    "compositions",
                    "presentations",
                    "conditions_prescription",
                    "groupes_generiques",
                    "infos_importantes",
                }
                if required.issubset(tables):
                    cols = {
                        r["name"]
                        for r in conn.execute("PRAGMA table_info(groupes_generiques);").fetchall()
                    }
                    if "libelle_groupe_normalise" in cols:
                        missing_cnt = conn.execute(
                            "SELECT COUNT(*) AS cnt FROM groupes_generiques WHERE libelle_groupe_normalise = '' AND libelle_groupe != '';"
                        ).fetchone()["cnt"]
                        if missing_cnt == 0:
                            _INITIALIZED_DBS.add((str(path_obj), path_obj.stat().st_mtime_ns))
                            return
        except sqlite3.Error:
            pass

    with get_connection(path_obj) as conn:
        conn.executescript(SCHEMA_SQL)
        cols = [
            r["name"]
            for r in conn.execute("PRAGMA table_info(groupes_generiques);").fetchall()
        ]
        if "libelle_groupe_normalise" not in cols:
            conn.execute(
                "ALTER TABLE groupes_generiques ADD COLUMN libelle_groupe_normalise TEXT NOT NULL DEFAULT '';"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_gener_libelle_norm ON groupes_generiques(libelle_groupe_normalise);"
        )
        missing_norm = conn.execute(
            "SELECT DISTINCT libelle_groupe FROM groupes_generiques WHERE libelle_groupe_normalise = '';"
        ).fetchall()
        if missing_norm:
            conn.executemany(
                "UPDATE groupes_generiques SET libelle_groupe_normalise = ? WHERE libelle_groupe = ?;",
                [(normalize_text(r["libelle_groupe"]), r["libelle_groupe"]) for r in missing_norm],
            )
        conn.commit()

    if path_obj.exists():
        _INITIALIZED_DBS.add((str(path_obj), path_obj.stat().st_mtime_ns))
