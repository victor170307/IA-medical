"""Gestion de la base de données SQLite et création du schéma relationnel BDPM.

Toutes les tables sont reliées par l'identifiant officiel `code_cis` (Code Identifiant
de Spécialité à 8 chiffres défini par les autorités de santé françaises).
Aucune relation n'est créée par simple ressemblance de noms.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3

from app.config import BACKUP_DIR, DEFAULT_DB_PATH

PALIER1_TABLES: tuple[str, ...] = (
    "import_metadata",
    "specialites",
    "compositions",
    "presentations",
    "conditions_prescription",
    "groupes_generiques",
    "infos_importantes",
)

LOT2C_TABLES: tuple[str, ...] = (
    "disponibilites_spec",
    "has_avis_smr",
    "has_avis_asmr",
    "has_liens_ct",
    "specialites_mitm",
    "sources_fraicheur",
)

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

# Schéma additionnel du Lot 2C (6 tables spécialisées + index).
# IMPORTANT : ce schéma n'est JAMAIS appliqué automatiquement au démarrage de l'application ;
# il est appliqué uniquement par la migration explicite `migrate_lot2c_schema()`.
LOT2C_DDL_STATEMENTS: tuple[str, ...] = (
    # 1. Disponibilités, tensions et ruptures (CIS_CIP_Dispo_Spec.txt — 8 colonnes officielles)
    """
    CREATE TABLE IF NOT EXISTS disponibilites_spec (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code_cis TEXT NOT NULL,
        code_cip13 TEXT,
        code_statut TEXT NOT NULL,
        libelle_statut TEXT NOT NULL,
        date_debut TEXT,
        date_mise_a_jour TEXT,
        date_remise_disposition TEXT,
        lien_ansm TEXT,
        FOREIGN KEY (code_cis) REFERENCES specialites(code_cis) ON DELETE CASCADE
    );
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_dispo_exact_row ON disponibilites_spec (
        code_cis,
        IFNULL(code_cip13, ''),
        code_statut,
        libelle_statut,
        IFNULL(date_debut, ''),
        IFNULL(date_mise_a_jour, ''),
        IFNULL(date_remise_disposition, ''),
        IFNULL(lien_ansm, '')
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_dispo_cis ON disponibilites_spec(code_cis);",
    "CREATE INDEX IF NOT EXISTS idx_dispo_cip13 ON disponibilites_spec(code_cip13);",
    "CREATE INDEX IF NOT EXISTS idx_dispo_statut ON disponibilites_spec(code_statut);",
    # 2. Avis HAS — SMR par indication (CIS_HAS_SMR_bdpm.txt — 6 colonnes officielles)
    # Volontairement sans clé étrangère bloquante vers `specialites` afin de ne jamais perdre
    # les avis historiques ou récents (non encore dans CIS_bdpm.txt) ; la colonne
    # `specialite_presente` (1/0) distingue clairement les avis rattachés des avis historiques.
    """
    CREATE TABLE IF NOT EXISTS has_avis_smr (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code_cis TEXT NOT NULL,
        code_dossier_has TEXT NOT NULL,
        motif_evaluation TEXT,
        date_avis TEXT,
        valeur_smr TEXT,
        libelle_smr TEXT NOT NULL,
        specialite_presente INTEGER NOT NULL DEFAULT 1
    );
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_has_smr_exact_row ON has_avis_smr (
        code_cis,
        code_dossier_has,
        IFNULL(motif_evaluation, ''),
        IFNULL(date_avis, ''),
        IFNULL(valeur_smr, ''),
        libelle_smr
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_has_smr_cis ON has_avis_smr(code_cis);",
    "CREATE INDEX IF NOT EXISTS idx_has_smr_dossier ON has_avis_smr(code_dossier_has);",
    "CREATE INDEX IF NOT EXISTS idx_has_smr_presente ON has_avis_smr(specialite_presente);",
    # 3. Avis HAS — ASMR par indication (CIS_HAS_ASMR_bdpm.txt — 6 colonnes officielles)
    """
    CREATE TABLE IF NOT EXISTS has_avis_asmr (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code_cis TEXT NOT NULL,
        code_dossier_has TEXT NOT NULL,
        motif_evaluation TEXT,
        date_avis TEXT,
        valeur_asmr TEXT,
        libelle_asmr TEXT NOT NULL,
        specialite_presente INTEGER NOT NULL DEFAULT 1
    );
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_has_asmr_exact_row ON has_avis_asmr (
        code_cis,
        code_dossier_has,
        IFNULL(motif_evaluation, ''),
        IFNULL(date_avis, ''),
        IFNULL(valeur_asmr, ''),
        libelle_asmr
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_has_asmr_cis ON has_avis_asmr(code_cis);",
    "CREATE INDEX IF NOT EXISTS idx_has_asmr_dossier ON has_avis_asmr(code_dossier_has);",
    "CREATE INDEX IF NOT EXISTS idx_has_asmr_presente ON has_avis_asmr(specialite_presente);",
    # 4. Liens officiels vers les pages d'avis CT de la HAS (HAS_LiensPageCT_bdpm.txt)
    # Plusieurs URLs sont autorisées pour un même `code_dossier_has` (16 cas réels constatés).
    """
    CREATE TABLE IF NOT EXISTS has_liens_ct (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code_dossier_has TEXT NOT NULL,
        lien_page_ct TEXT NOT NULL,
        UNIQUE(code_dossier_has, lien_page_ct)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_has_liens_dossier ON has_liens_ct(code_dossier_has);",
    # 5. Médicaments d'intérêt thérapeutique majeur (CIS_MITM.txt — 7 colonnes réelles)
    """
    CREATE TABLE IF NOT EXISTS specialites_mitm (
        code_cis TEXT PRIMARY KEY,
        code_atc TEXT,
        denomination TEXT,
        titulaire TEXT,
        exploitant TEXT,
        date_publication TEXT,
        lien_bdpm TEXT,
        FOREIGN KEY (code_cis) REFERENCES specialites(code_cis) ON DELETE CASCADE
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_mitm_atc ON specialites_mitm(code_atc);",
    # 6. Suivi indépendant de fraîcheur, présence, téléchargement et import par source
    """
    CREATE TABLE IF NOT EXISTS sources_fraicheur (
        source_key TEXT PRIMARY KEY,
        domaine TEXT NOT NULL,
        source_label TEXT NOT NULL,
        source_url TEXT,
        is_optional INTEGER NOT NULL DEFAULT 1,
        file_present INTEGER NOT NULL DEFAULT 0,
        download_status TEXT NOT NULL DEFAULT 'not_tracked',
        import_status TEXT NOT NULL DEFAULT 'never_imported',
        last_attempt_at_utc TEXT,
        last_attempt_error TEXT,
        last_attempt_lines_read INTEGER NOT NULL DEFAULT 0,
        last_attempt_rows_imported INTEGER NOT NULL DEFAULT 0,
        last_attempt_skipped_duplicate INTEGER NOT NULL DEFAULT 0,
        last_attempt_skipped_orphan INTEGER NOT NULL DEFAULT 0,
        last_attempt_malformed INTEGER NOT NULL DEFAULT 0,
        last_success_imported_at_utc TEXT,
        last_success_downloaded_at_utc TEXT,
        last_success_official_date TEXT,
        last_success_official_date_origin TEXT,
        http_last_modified TEXT,
        last_success_sha256 TEXT,
        last_success_size_bytes INTEGER,
        active_rows_count INTEGER NOT NULL DEFAULT 0,
        historical_unattached_count INTEGER NOT NULL DEFAULT 0
    );
    """,
)

LOT2C_SCHEMA_SQL = "\n".join(stmt.strip() for stmt in LOT2C_DDL_STATEMENTS) + "\n"


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


def _file_sha256(path_obj: Path) -> str:
    digest = hashlib.sha256()
    with path_obj.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def get_readonly_connection(db_path: Path | str = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    """Ouvre une connexion SQLite strictement en lecture seule (`mode=ro`)."""
    path_obj = Path(db_path).resolve()
    if not path_obj.exists():
        raise FileNotFoundError(f"Base SQLite introuvable : {path_obj}")
    uri = f"file:{path_obj.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _invalidate_init_cache(path_obj: Path) -> None:
    resolved = str(path_obj.resolve())
    to_remove = {entry for entry in _INITIALIZED_DBS if entry[0] == resolved}
    _INITIALIZED_DBS.difference_update(to_remove)


def inspect_schema_status(db_path: Path | str = DEFAULT_DB_PATH) -> dict:
    """Inspecte en lecture seule l'état du schéma SQLite (tables Palier 1, Lot 2C, compteurs).

    Ne crée ni ne modifie jamais le fichier sur le disque.
    """
    path_obj = Path(db_path).resolve()
    if not path_obj.exists():
        return {
            "exists": False,
            "db_path": str(path_obj),
            "size_bytes": 0,
            "quick_check": None,
            "palier1_present": [],
            "palier1_missing": list(PALIER1_TABLES),
            "lot2c_present": [],
            "lot2c_missing": list(LOT2C_TABLES),
            "lot2c_migrated": False,
            "schema_version": None,
            "table_counts": {},
        }

    with get_readonly_connection(path_obj) as conn:
        quick_check = conn.execute("PRAGMA quick_check;").fetchone()[0]
        existing_tables = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
            ).fetchall()
        }
        table_counts = {
            name: conn.execute(f"SELECT COUNT(*) FROM \"{name}\";").fetchone()[0]
            for name in sorted(existing_tables)
        }
        schema_version = None
        if "import_metadata" in existing_tables:
            row = conn.execute(
                "SELECT value FROM import_metadata WHERE key = 'schema_version';"
            ).fetchone()
            schema_version = row["value"] if row else "1"

    palier1_present = [t for t in PALIER1_TABLES if t in existing_tables]
    palier1_missing = [t for t in PALIER1_TABLES if t not in existing_tables]
    lot2c_present = [t for t in LOT2C_TABLES if t in existing_tables]
    lot2c_missing = [t for t in LOT2C_TABLES if t not in existing_tables]

    return {
        "exists": True,
        "db_path": str(path_obj),
        "size_bytes": path_obj.stat().st_size,
        "quick_check": quick_check,
        "palier1_present": palier1_present,
        "palier1_missing": palier1_missing,
        "lot2c_present": lot2c_present,
        "lot2c_missing": lot2c_missing,
        "lot2c_migrated": len(lot2c_missing) == 0,
        "schema_version": schema_version,
        "table_counts": table_counts,
    }


def backup_database(
    db_path: Path | str = DEFAULT_DB_PATH,
    backup_dir: Path | str = BACKUP_DIR,
    label: str = "manual",
) -> dict:
    """Crée une sauvegarde vérifiée d'une base SQLite via l'API officielle `sqlite3.backup()`.

    Vérifie immédiatement après copie :
    - l'intégrité SQLite (`PRAGMA quick_check == 'ok'`),
    - l'identité exacte des compteurs de lignes sur toutes les tables,
    - l'empreinte SHA-256 du fichier de sauvegarde.
    """
    src_path = Path(db_path).resolve()
    if not src_path.exists():
        raise FileNotFoundError(f"Impossible de sauvegarder une base inexistante : {src_path}")

    source_status = inspect_schema_status(src_path)
    if source_status["quick_check"] != "ok":
        raise RuntimeError(
            f"La base source échoue à PRAGMA quick_check ({source_status['quick_check']})."
        )

    backup_root = Path(backup_dir).resolve()
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    safe_label = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in label).strip("_") or "backup"
    dest_path = backup_root / f"{src_path.stem}_{safe_label}_{stamp}.sqlite3"
    tmp_dest = backup_root / f".{dest_path.name}.tmp"

    try:
        with get_readonly_connection(src_path) as src_conn:
            dst_conn = sqlite3.connect(str(tmp_dest))
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()

        backup_status = inspect_schema_status(tmp_dest)
        if backup_status["quick_check"] != "ok":
            raise RuntimeError("La sauvegarde générée échoue à PRAGMA quick_check.")
        if backup_status["table_counts"] != source_status["table_counts"]:
            raise RuntimeError("Les compteurs de lignes de la sauvegarde diffèrent de la source.")

        tmp_dest.replace(dest_path)
    except Exception:
        if tmp_dest.exists():
            tmp_dest.unlink()
        raise

    return {
        "source_path": str(src_path),
        "backup_path": str(dest_path),
        "created_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "size_bytes": dest_path.stat().st_size,
        "sha256": _file_sha256(dest_path),
        "integrity_ok": True,
        "table_counts": backup_status["table_counts"],
    }


def restore_database(
    backup_path: Path | str,
    target_db_path: Path | str = DEFAULT_DB_PATH,
) -> dict:
    """Restaure une base SQLite à partir d'une sauvegarde préalablement vérifiée.

    Vérifie l'intégrité de la sauvegarde avant toute opération, restaure dans un fichier
    temporaire via `sqlite3.backup()`, vérifie la copie puis remplace atomiquement la cible.
    """
    src_backup = Path(backup_path).resolve()
    dst_target = Path(target_db_path).resolve()
    if not src_backup.exists():
        raise FileNotFoundError(f"Sauvegarde introuvable : {src_backup}")

    backup_status = inspect_schema_status(src_backup)
    if backup_status["quick_check"] != "ok":
        raise RuntimeError(
            f"Sauvegarde corrompue (PRAGMA quick_check={backup_status['quick_check']}), restauration annulée."
        )

    dst_target.parent.mkdir(parents=True, exist_ok=True)
    tmp_target = dst_target.parent / f".{dst_target.name}.restore.tmp"

    try:
        with get_readonly_connection(src_backup) as bkp_conn:
            out_conn = sqlite3.connect(str(tmp_target))
            try:
                bkp_conn.backup(out_conn)
            finally:
                out_conn.close()

        restored_status = inspect_schema_status(tmp_target)
        if restored_status["quick_check"] != "ok":
            raise RuntimeError("La base restaurée échoue à PRAGMA quick_check.")
        if restored_status["table_counts"] != backup_status["table_counts"]:
            raise RuntimeError("Incohérence de compteurs détectée lors de la restauration.")

        tmp_target.replace(dst_target)
        _invalidate_init_cache(dst_target)
    except Exception:
        if tmp_target.exists():
            tmp_target.unlink()
        raise

    return {
        "restored_from": str(src_backup),
        "target_db_path": str(dst_target),
        "restored_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "size_bytes": dst_target.stat().st_size,
        "sha256": _file_sha256(dst_target),
        "integrity_ok": True,
        "table_counts": restored_status["table_counts"],
    }


def migrate_lot2c_schema(
    db_path: Path | str = DEFAULT_DB_PATH,
    *,
    create_backup: bool = True,
    backup_dir: Path | str = BACKUP_DIR,
) -> dict:
    """Migration explicite, transactionnelle et idempotente vers le schéma Lot 2C.

    - N'est JAMAIS appelée automatiquement au démarrage normal de l'application.
    - Crée une sauvegarde vérifiée avant toute écriture si la base existe déjà et `create_backup=True`.
    - Ne modifie ni ne supprime aucune donnée des 7 tables existantes du Palier 1 / Lot 2B.
    """
    path_obj = Path(db_path).resolve()
    existed_before = path_obj.exists()
    before_status = inspect_schema_status(path_obj) if existed_before else None

    if existed_before and before_status and before_status["palier1_missing"]:
        raise RuntimeError(
            "La base existante ne contient pas toutes les tables de base Palier 1 : "
            f"{before_status['palier1_missing']}."
        )

    backup_info = None
    if existed_before and create_backup and before_status and not before_status["lot2c_migrated"]:
        backup_info = backup_database(
            db_path=path_obj,
            backup_dir=backup_dir,
            label="pre_lot2c_migration",
        )

    if not existed_before:
        init_db(path_obj)
        before_status = inspect_schema_status(path_obj)

    missing_before = list(before_status["lot2c_missing"]) if before_status else list(LOT2C_TABLES)
    already_migrated = len(missing_before) == 0

    migrated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_connection(path_obj) as conn:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE;")
        try:
            for stmt in LOT2C_DDL_STATEMENTS:
                conn.execute(stmt)
            conn.execute(
                "INSERT OR REPLACE INTO import_metadata (key, value) VALUES ('schema_version', '2c');"
            )
            if not already_migrated:
                conn.execute(
                    "INSERT OR REPLACE INTO import_metadata (key, value) VALUES ('lot2c_migrated_at_utc', ?);",
                    (migrated_at,),
                )
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise

    _invalidate_init_cache(path_obj)
    after_status = inspect_schema_status(path_obj)
    if after_status["quick_check"] != "ok":
        raise RuntimeError(f"Échec de PRAGMA quick_check après migration : {after_status['quick_check']}")

    return {
        "db_path": str(path_obj),
        "already_migrated": already_migrated,
        "tables_created": missing_before,
        "backup": backup_info,
        "integrity_ok": after_status["quick_check"] == "ok",
        "schema_status": after_status,
    }
