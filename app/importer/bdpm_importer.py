"""Importateur reproductible et idempotent des fichiers publics de la BDPM vers SQLite.

Principes respectés :
- Vérification de la structure des lignes (nombre minimal de colonnes et format du code CIS).
- Gestion stricte des valeurs manquantes : une chaîne vide est enregistrée comme `None` (NULL),
  sans jamais inventer de valeur par défaut médicale.
- Idempotence : relancer l'importation met à jour proprement les données sans créer de doublons.
- Sécurité : le fichier `CIS_InfoImportantes.txt` contient des balises HTML `<a href='...'>...</a>`.
  On extrait proprement le texte brut et l'URL ANSM vérifiée au lieu d'injecter du HTML brut.
- Traçabilité : chaque import enregistre la date d'import, la date officielle BDPM (si connue),
  la source officielle et les compteurs détaillés (lignes lues, importées, ignorées, erreurs).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import re
import sqlite3

from app.config import (
    BDPM_BASE_URL,
    BDPM_FILES,
    BDPM_SOURCE_NAME,
    DEFAULT_DB_PATH,
    OFFICIAL_SOURCES,
    RAW_BDPM_DIR,
    get_official_fiche_url,
)
from app.database import (
    LOT2C_TABLES,
    get_connection,
    init_db,
    inspect_schema_status,
    migrate_lot2c_schema,
)
from app.search.normalizer import normalize_text

CIS_PATTERN = re.compile(r"^\d{8}$")
CIP13_PATTERN = re.compile(r"^\d{13}$")
DATE_FR_PATTERN = re.compile(r"^\d{2}/\d{2}/\d{4}$")
DATE_ISO8_PATTERN = re.compile(r"^\d{8}$")
DATE_ISO_DASH_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HREF_PATTERN = re.compile(r"""href\s*=\s*['"]([^'"]+)['"]""", re.IGNORECASE)
TAG_PATTERN = re.compile(r"<[^>]+>")
# Ne cible STRICTEMENT que les balises <br>, <br/>, <br /> (jamais '< 18 ans', 'p < 0,0001', '< -3 DS')
BR_TAG_PATTERN = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)


@dataclass
class FileImportStats:
    """Statistiques d'import pour un fichier source BDPM/ANSM/HAS/MITM."""

    filename: str
    present: bool = False
    lines_read: int = 0
    rows_imported: int = 0
    rows_skipped_duplicate: int = 0
    rows_skipped_orphan: int = 0
    rows_historical_unattached: int = 0
    rows_malformed: int = 0
    import_status: str = "ok"
    download_status: str = "not_tracked"
    preserved_previous_data: bool = False
    active_rows_count: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class ImportReport:
    """Rapport complet retourné à la fin d'une importation BDPM / Lot 2C."""

    imported_at_utc: str
    official_bdpm_date: str | None
    source_name: str
    source_url: str
    files: dict[str, FileImportStats] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "imported_at_utc": self.imported_at_utc,
            "official_bdpm_date": self.official_bdpm_date,
            "source_name": self.source_name,
            "source_url": self.source_url,
            "files": {
                k: {
                    "filename": v.filename,
                    "present": v.present,
                    "lines_read": v.lines_read,
                    "rows_imported": v.rows_imported,
                    "rows_skipped_duplicate": v.rows_skipped_duplicate,
                    "rows_skipped_orphan": v.rows_skipped_orphan,
                    "rows_historical_unattached": v.rows_historical_unattached,
                    "rows_malformed": v.rows_malformed,
                    "import_status": v.import_status,
                    "download_status": v.download_status,
                    "preserved_previous_data": v.preserved_previous_data,
                    "active_rows_count": v.active_rows_count,
                    "errors": v.errors[:20],
                }
                for k, v in self.files.items()
            },
        }


def decode_bdpm_line(raw_line: bytes) -> str:
    """Décode une ligne binaire issue d'un fichier BDPM.

    Certains fichiers de la BDPM sont encodés en Windows-1252 (cp1252) et d'autres
    en UTF-8. On tente d'abord UTF-8 strict, puis on bascule sur cp1252.
    """
    try:
        return raw_line.decode("utf-8").lstrip("\ufeff")
    except UnicodeDecodeError:
        return raw_line.decode("cp1252", errors="replace")


def clean_field(value: str | None) -> str | None:
    """Nettoie un champ texte et renvoie `None` s'il est vide."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped != "" else None


def clean_has_medical_text(raw_text: str | None) -> str | None:
    """Nettoie un libellé médical HAS (`libelle_smr` ou `libelle_asmr`).

    Règle stricte :
    - Remplace UNIQUEMENT les véritables balises `<br>`, `<br/>`, `<br />` (insensibles à la casse)
      par des retours à la ligne `\\n`.
    - N'applique JAMAIS de suppression générique `<[^>]+>` afin de préserver intégralement
      les expressions cliniques contenant `<` ou `>` (ex. `< 18 ans`, `p < 0,0001`, `< -3 DS`).
    - L'échappement HTML au rendu web reste intégralement assuré par Jinja2.
    """
    if raw_text is None:
        return None
    text = raw_text.strip()
    if not text:
        return None
    text = BR_TAG_PATTERN.sub("\n", text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    cleaned = "\n".join(line for line in lines if line)
    return cleaned if cleaned else None


def normalize_has_date(raw_date: str | None) -> tuple[str | None, bool]:
    """Valide et normalise une date HAS (`AAAAMMJJ` -> `AAAA-MM-JJ`).

    Retourne `(date_normalisee, est_valide)`. Une date vide retourne `(None, True)`.
    """
    cleaned = clean_field(raw_date)
    if cleaned is None:
        return None, True
    if DATE_ISO8_PATTERN.match(cleaned):
        try:
            dt = datetime.strptime(cleaned, "%Y%m%d")
            return dt.strftime("%Y-%m-%d"), True
        except ValueError:
            return None, False
    if DATE_ISO_DASH_PATTERN.match(cleaned):
        try:
            datetime.strptime(cleaned, "%Y-%m-%d")
            return cleaned, True
        except ValueError:
            return None, False
    return None, False


def validate_fr_date(raw_date: str | None) -> tuple[str | None, bool]:
    """Valide une date française `JJ/MM/AAAA`. Une valeur vide retourne `(None, True)`."""
    cleaned = clean_field(raw_date)
    if cleaned is None:
        return None, True
    if not DATE_FR_PATTERN.match(cleaned):
        return None, False
    try:
        datetime.strptime(cleaned, "%d/%m/%Y")
    except ValueError:
        return None, False
    return cleaned, True


def validate_http_url(raw_url: str | None) -> tuple[str | None, bool]:
    """Valide qu'une URL utilise strictement le schéma `http://` ou `https://`."""
    cleaned = clean_field(raw_url)
    if cleaned is None:
        return None, True
    low = cleaned.lower()
    if low.startswith("https://") or low.startswith("http://"):
        return cleaned, True
    return None, False


def _compute_file_sha256(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_download_sources_metadata(raw_dir: Path) -> dict[str, dict]:
    """Lit les métadonnées de téléchargement par fichier depuis `metadata_sources.json`
    et complète au besoin avec `metadata_telechargement.txt` (compatibilité Palier 1).
    """
    sources_meta: dict[str, dict] = {}

    txt_path = raw_dir / "metadata_telechargement.txt"
    txt_global_dl: str | None = None
    txt_global_official: str | None = None
    if txt_path.exists():
        for line in txt_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k == "downloaded_at_utc" and v:
                txt_global_dl = v
            elif k == "official_bdpm_date" and v and v != "non_detectee":
                txt_global_official = v
            elif k.startswith("file."):
                parts = k.split(".")
                if len(parts) >= 3:
                    fname = ".".join(parts[1:-1])
                    attr = parts[-1]
                    entry = sources_meta.setdefault(fname, {"status": "ok"})
                    if attr == "size_bytes" and v.isdigit():
                        entry["size_bytes"] = int(v)
                    elif attr in ("sha256", "http_last_modified", "official_source_date"):
                        entry[attr] = v

    if txt_global_dl or txt_global_official:
        for palier1_fname in BDPM_FILES:
            if (raw_dir / palier1_fname).exists():
                sources_meta.setdefault(palier1_fname, {"status": "ok"})

    for fname, entry in sources_meta.items():
        if txt_global_dl and not entry.get("downloaded_at_utc"):
            entry["downloaded_at_utc"] = txt_global_dl
        if fname != "CIS_InfoImportantes.txt" and not entry.get("official_source_date") and txt_global_official:
            entry["official_source_date"] = txt_global_official
            entry.setdefault("official_date_verified_from", "metadata_telechargement.txt (portail BDPM)")

    json_path = raw_dir / "metadata_sources.json"
    if json_path.exists():
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            files_dict = payload.get("files", {}) if isinstance(payload, dict) else {}
            if isinstance(files_dict, dict):
                for fname, info in files_dict.items():
                    if isinstance(info, dict):
                        merged = dict(sources_meta.get(fname, {}))
                        merged.update(info)
                        sources_meta[fname] = merged
        except Exception:
            pass

    return sources_meta


def extract_main_brand_token(denomination: str) -> str:
    """Extrait le premier mot significatif normalisé du nom de spécialité
    (ex. 'DOLIPRANE 1000 mg, comprimé' -> 'doliprane') pour faciliter la
    correction de fautes de frappe sur les noms de médicaments.
    """
    norm = normalize_text(denomination)
    if not norm:
        return ""
    return norm.split(" ")[0]


def parse_info_importante_html(raw_html_field: str) -> tuple[str, str | None]:
    """Extrait sans danger le texte lisible et le lien officiel ANSM éventuel
    à partir de la 4e colonne de `CIS_InfoImportantes.txt`.
    """
    link: str | None = None
    match = HREF_PATTERN.search(raw_html_field)
    if match:
        candidate = html.unescape(match.group(1).strip())
        if candidate.startswith("https://") or candidate.startswith("http://"):
            link = candidate

    text_without_tags = TAG_PATTERN.sub(" ", raw_html_field)
    unescaped = html.unescape(text_without_tags)
    clean_text = re.sub(r"\s+", " ", unescaped).strip()
    return clean_text, link


def _read_metadata_date(raw_dir: Path) -> str | None:
    """Lit la date officielle BDPM enregistrée lors du téléchargement si disponible."""
    meta_file = raw_dir / "metadata_telechargement.txt"
    if not meta_file.exists():
        return None
    for line in meta_file.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("official_bdpm_date="):
            val = line.split("=", 1)[1].strip()
            if val and val != "non_detectee":
                return val
    return None


def import_bdpm_directory(
    raw_dir: Path | str = RAW_BDPM_DIR,
    db_path: Path | str = DEFAULT_DB_PATH,
    official_bdpm_date: str | None = None,
    limit_specialites: int | None = None,
    *,
    include_lot2c: bool = True,
    allow_schema_migration: bool | None = None,
) -> ImportReport:
    """Importe les fichiers BDPM présents dans `raw_dir` vers la base SQLite `db_path`.

    Sécurité et compatibilité :
    - `CIS_bdpm.txt` est validé en mémoire AVANT toute écriture SQLite : un fichier vide ou
      entièrement malformé lève `ValueError` sans toucher aux données existantes.
    - La mise à jour de `specialites` utilise un UPSERT + nettoyage ciblé des CIS retirés
      afin de ne jamais effacer en cascade les données Lot 2C préservées lors d'un réimport.
    - Sur une base existante qui n'a pas encore été migrée vers le Lot 2C, aucune migration
      automatique n'est effectuée tant que `allow_schema_migration=True` n'est pas explicite
      (ou que `migrate_lot2c_schema()` n'a pas été exécutée). Sur une base neuve inexistante,
      le schéma complet est créé proprement.
    """
    raw_path = Path(raw_dir)
    cis_bdpm_path = raw_path / "CIS_bdpm.txt"
    if not cis_bdpm_path.exists():
        raise FileNotFoundError(
            f"Le fichier principal CIS_bdpm.txt est introuvable dans {raw_path}."
        )

    db_path_obj = Path(db_path)
    existed_before = db_path_obj.exists()

    # Pré-validation de CIS_bdpm.txt en mémoire AVANT toute écriture dans SQLite
    known_cis: set[str] = set()
    spec_stats = FileImportStats(filename="CIS_bdpm.txt", present=True)
    spec_batch: list[tuple] = []

    with cis_bdpm_path.open("rb") as f:
        for line_no, raw_line in enumerate(f, start=1):
            if not raw_line.strip():
                continue
            spec_stats.lines_read += 1
            line = decode_bdpm_line(raw_line).rstrip("\r\n")
            cols = line.split("\t")
            if len(cols) < 8:
                spec_stats.rows_malformed += 1
                spec_stats.errors.append(
                    f"Ligne {line_no}: nombre de colonnes insuffisant ({len(cols)} < 8)"
                )
                continue

            code_cis = clean_field(cols[0])
            denomination = clean_field(cols[1])
            if not code_cis or not CIS_PATTERN.match(code_cis) or not denomination:
                spec_stats.rows_malformed += 1
                spec_stats.errors.append(
                    f"Ligne {line_no}: code CIS ({code_cis}) ou dénomination invalide"
                )
                continue

            if code_cis in known_cis:
                spec_stats.rows_skipped_duplicate += 1
                continue

            forme = clean_field(cols[2]) if len(cols) > 2 else None
            voies = clean_field(cols[3]) if len(cols) > 3 else None
            statut_amm = clean_field(cols[4]) if len(cols) > 4 else None
            type_proc = clean_field(cols[5]) if len(cols) > 5 else None
            etat_comm = clean_field(cols[6]) if len(cols) > 6 else None
            date_amm = clean_field(cols[7]) if len(cols) > 7 else None
            statut_bdm = clean_field(cols[8]) if len(cols) > 8 else None
            num_euro = clean_field(cols[9]) if len(cols) > 9 else None
            titulaires = clean_field(cols[10]) if len(cols) > 10 else None
            surv_renf = clean_field(cols[11]) if len(cols) > 11 else None

            known_cis.add(code_cis)
            spec_batch.append(
                (
                    code_cis,
                    denomination,
                    normalize_text(denomination),
                    extract_main_brand_token(denomination),
                    forme,
                    normalize_text(forme),
                    voies,
                    statut_amm,
                    type_proc,
                    etat_comm,
                    date_amm,
                    statut_bdm,
                    num_euro,
                    titulaires,
                    surv_renf,
                    get_official_fiche_url(code_cis),
                )
            )
            spec_stats.rows_imported += 1

            if limit_specialites and spec_stats.rows_imported >= limit_specialites:
                break

    if not spec_batch:
        raise ValueError(
            "Le fichier CIS_bdpm.txt est vide ou ne contient aucune spécialité valide ; "
            "l'importation est annulée et les données existantes sont préservées."
        )

    init_db(db_path_obj)
    schema_status = inspect_schema_status(db_path_obj)
    if include_lot2c and not schema_status["lot2c_migrated"]:
        should_migrate = (not existed_before) if allow_schema_migration is None else allow_schema_migration
        if should_migrate:
            migrate_lot2c_schema(db_path_obj, create_backup=existed_before)
            schema_status = inspect_schema_status(db_path_obj)

    has_lot2c_schema = schema_status["lot2c_migrated"]
    download_meta = _read_download_sources_metadata(raw_path)

    detected_date = official_bdpm_date or _read_metadata_date(raw_path)
    imported_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    report = ImportReport(
        imported_at_utc=imported_at,
        official_bdpm_date=detected_date,
        source_name=BDPM_SOURCE_NAME,
        source_url=BDPM_BASE_URL,
    )
    spec_stats.import_status = "ok"
    spec_stats.active_rows_count = spec_stats.rows_imported
    spec_stats.download_status = download_meta.get("CIS_bdpm.txt", {}).get("status", "not_tracked")
    report.files["CIS_bdpm.txt"] = spec_stats

    with get_connection(db_path_obj) as conn:
        # 1. Mise à jour sûre de `specialites` (UPSERT + suppression des seuls CIS disparus)
        # afin de ne jamais déclencher un ON DELETE CASCADE destructeur sur les tables Lot 2C
        # lorsque des fichiers facultatifs du Lot 2C sont absents ou en échec.
        conn.executemany(
            """
            INSERT INTO specialites (
                code_cis, denomination, denomination_normalisee,
                marque_principale_normalisee, forme_pharmaceutique, forme_normalisee,
                voies_administration, statut_amm, type_procedure_amm,
                etat_commercialisation, date_amm, statut_bdm,
                numero_autorisation_europeenne, titulaires, surveillance_renforcee,
                official_fiche_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code_cis) DO UPDATE SET
                denomination = excluded.denomination,
                denomination_normalisee = excluded.denomination_normalisee,
                marque_principale_normalisee = excluded.marque_principale_normalisee,
                forme_pharmaceutique = excluded.forme_pharmaceutique,
                forme_normalisee = excluded.forme_normalisee,
                voies_administration = excluded.voies_administration,
                statut_amm = excluded.statut_amm,
                type_procedure_amm = excluded.type_procedure_amm,
                etat_commercialisation = excluded.etat_commercialisation,
                date_amm = excluded.date_amm,
                statut_bdm = excluded.statut_bdm,
                numero_autorisation_europeenne = excluded.numero_autorisation_europeenne,
                titulaires = excluded.titulaires,
                surveillance_renforcee = excluded.surveillance_renforcee,
                official_fiche_url = excluded.official_fiche_url
            """,
            spec_batch,
        )
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _imported_cis (code_cis TEXT PRIMARY KEY);")
        conn.execute("DELETE FROM _imported_cis;")
        conn.executemany(
            "INSERT INTO _imported_cis (code_cis) VALUES (?);",
            [(cis,) for cis in known_cis],
        )
        conn.execute(
            "DELETE FROM specialites WHERE code_cis NOT IN (SELECT code_cis FROM _imported_cis);"
        )

        # Réinitialisation atomique des 5 tables filles du Palier 1
        conn.execute("DELETE FROM infos_importantes;")
        conn.execute("DELETE FROM groupes_generiques;")
        conn.execute("DELETE FROM conditions_prescription;")
        conn.execute("DELETE FROM presentations;")
        conn.execute("DELETE FROM compositions;")

        # 2. Import de CIS_COMPO_bdpm.txt (Substances actives et dosages)
        _import_compositions(conn, raw_path / "CIS_COMPO_bdpm.txt", known_cis, report)

        # 3. Import de CIS_CIP_bdpm.txt (Présentations / Boîtes)
        _import_presentations(conn, raw_path / "CIS_CIP_bdpm.txt", known_cis, report)

        # 4. Import de CIS_CPD_bdpm.txt (Conditions de prescription et délivrance)
        _import_conditions_prescription(conn, raw_path / "CIS_CPD_bdpm.txt", known_cis, report)

        # 5. Import de CIS_GENER_bdpm.txt (Groupes génériques)
        _import_groupes_generiques(conn, raw_path / "CIS_GENER_bdpm.txt", known_cis, report)

        # 6. Import de CIS_InfoImportantes.txt (Informations importantes ANSM)
        _import_infos_importantes(conn, raw_path / "CIS_InfoImportantes.txt", known_cis, report)

        if has_lot2c_schema:
            _record_palier1_freshness(
                conn=conn,
                raw_path=raw_path,
                report=report,
                download_meta=download_meta,
                imported_at=imported_at,
                fallback_official_date=detected_date,
            )

        # 7. Import sûr et transactionnel des 5 sources complémentaires du Lot 2C
        if include_lot2c and has_lot2c_schema:
            _run_lot2c_importers_on_conn(
                conn=conn,
                raw_path=raw_path,
                known_cis=known_cis,
                report=report,
                download_meta=download_meta,
                attempt_at_utc=imported_at,
            )

        # Recalcule systématiquement le rattachement (`specialite_presente`) des avis HAS
        # conservés lorsque `specialites` évolue (permet le rattachement automatique si un CIS réapparaît)
        if has_lot2c_schema:
            _refresh_has_specialite_links(conn)

        # 8. Enregistrement des métadonnées globales de traçabilité (compatibilité Palier 1)
        metadata_entries = [
            ("imported_at_utc", imported_at),
            ("official_bdpm_date", detected_date or "Non précisée dans les fichiers locaux"),
            ("source_name", BDPM_SOURCE_NAME),
            ("source_url", BDPM_BASE_URL),
            ("specialites_count", str(spec_stats.rows_imported)),
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO import_metadata (key, value) VALUES (?, ?)",
            metadata_entries,
        )
        conn.commit()

    return report


def _import_compositions(
    conn: sqlite3.Connection,
    file_path: Path,
    known_cis: set[str],
    report: ImportReport,
) -> None:
    stats = FileImportStats(filename="CIS_COMPO_bdpm.txt", present=file_path.exists())
    report.files["CIS_COMPO_bdpm.txt"] = stats
    if not file_path.exists():
        return

    seen_keys: set[tuple] = set()
    batch: list[tuple] = []

    with file_path.open("rb") as f:
        for line_no, raw_line in enumerate(f, start=1):
            if not raw_line.strip():
                continue
            stats.lines_read += 1
            line = decode_bdpm_line(raw_line).rstrip("\r\n")
            cols = line.split("\t")
            if len(cols) < 6:
                stats.rows_malformed += 1
                stats.errors.append(
                    f"Ligne {line_no}: nombre de colonnes insuffisant ({len(cols)} < 6)"
                )
                continue

            code_cis = clean_field(cols[0])
            substance = clean_field(cols[3])
            if not code_cis or not CIS_PATTERN.match(code_cis) or not substance:
                stats.rows_malformed += 1
                stats.errors.append(f"Ligne {line_no}: code CIS ou substance manquant")
                continue

            if code_cis not in known_cis:
                stats.rows_skipped_orphan += 1
                continue

            designation_elem = clean_field(cols[1])
            code_subst = clean_field(cols[2])
            dosage = clean_field(cols[4]) if len(cols) > 4 else None
            ref_dosage = clean_field(cols[5]) if len(cols) > 5 else None
            nature = clean_field(cols[6]) if len(cols) > 6 else None
            num_liaison = clean_field(cols[7]) if len(cols) > 7 else None

            dedup_key = (
                code_cis,
                designation_elem,
                code_subst,
                substance,
                dosage,
                ref_dosage,
                nature,
                num_liaison,
            )
            if dedup_key in seen_keys:
                stats.rows_skipped_duplicate += 1
                continue
            seen_keys.add(dedup_key)

            batch.append(
                (
                    code_cis,
                    designation_elem,
                    code_subst,
                    substance,
                    normalize_text(substance),
                    dosage,
                    normalize_text(dosage),
                    ref_dosage,
                    nature,
                    num_liaison,
                )
            )
            stats.rows_imported += 1

    conn.executemany(
        """
        INSERT INTO compositions (
            code_cis, designation_element, code_substance,
            denomination_substance, substance_normalisee,
            dosage, dosage_normalise, reference_dosage,
            nature_composant, numero_liaison
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        batch,
    )


def _import_presentations(
    conn: sqlite3.Connection,
    file_path: Path,
    known_cis: set[str],
    report: ImportReport,
) -> None:
    stats = FileImportStats(filename="CIS_CIP_bdpm.txt", present=file_path.exists())
    report.files["CIS_CIP_bdpm.txt"] = stats
    if not file_path.exists():
        return

    seen_keys: set[tuple] = set()
    batch: list[tuple] = []

    with file_path.open("rb") as f:
        for line_no, raw_line in enumerate(f, start=1):
            if not raw_line.strip():
                continue
            stats.lines_read += 1
            line = decode_bdpm_line(raw_line).rstrip("\r\n")
            cols = line.split("\t")
            if len(cols) < 7:
                stats.rows_malformed += 1
                stats.errors.append(
                    f"Ligne {line_no}: nombre de colonnes insuffisant ({len(cols)} < 7)"
                )
                continue

            code_cis = clean_field(cols[0])
            code_cip7 = clean_field(cols[1])
            libelle = clean_field(cols[2])
            if not code_cis or not CIS_PATTERN.match(code_cis) or not libelle:
                stats.rows_malformed += 1
                stats.errors.append(f"Ligne {line_no}: code CIS ou libellé manquant")
                continue

            if code_cis not in known_cis:
                stats.rows_skipped_orphan += 1
                continue

            statut_admin = clean_field(cols[3]) if len(cols) > 3 else None
            etat_comm = clean_field(cols[4]) if len(cols) > 4 else None
            date_decl = clean_field(cols[5]) if len(cols) > 5 else None
            code_cip13 = clean_field(cols[6]) if len(cols) > 6 else None
            agrement = clean_field(cols[7]) if len(cols) > 7 else None
            taux_remb = clean_field(cols[8]) if len(cols) > 8 else None
            prix_ht = clean_field(cols[9]) if len(cols) > 9 else None
            prix_ttc = clean_field(cols[10]) if len(cols) > 10 else None
            honoraire = clean_field(cols[11]) if len(cols) > 11 else None
            indications_remb = clean_field(cols[12]) if len(cols) > 12 else None

            dedup_key = (code_cis, code_cip7, code_cip13)
            if dedup_key in seen_keys:
                stats.rows_skipped_duplicate += 1
                continue
            seen_keys.add(dedup_key)

            batch.append(
                (
                    code_cis,
                    code_cip7,
                    libelle,
                    statut_admin,
                    etat_comm,
                    date_decl,
                    code_cip13,
                    agrement,
                    taux_remb,
                    prix_ht,
                    prix_ttc,
                    honoraire,
                    indications_remb,
                )
            )
            stats.rows_imported += 1

    conn.executemany(
        """
        INSERT INTO presentations (
            code_cis, code_cip7, libelle_presentation,
            statut_administratif, etat_commercialisation,
            date_declaration_commercialisation, code_cip13,
            agrement_collectivites, taux_remboursement,
            prix_hors_honoraire, prix_ttc, honoraire_dispensation,
            indications_remboursement
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        batch,
    )


def _import_conditions_prescription(
    conn: sqlite3.Connection,
    file_path: Path,
    known_cis: set[str],
    report: ImportReport,
) -> None:
    stats = FileImportStats(filename="CIS_CPD_bdpm.txt", present=file_path.exists())
    report.files["CIS_CPD_bdpm.txt"] = stats
    if not file_path.exists():
        return

    seen_keys: set[tuple] = set()
    batch: list[tuple] = []

    with file_path.open("rb") as f:
        for line_no, raw_line in enumerate(f, start=1):
            if not raw_line.strip():
                continue
            stats.lines_read += 1
            line = decode_bdpm_line(raw_line).rstrip("\r\n")
            cols = line.split("\t")
            if len(cols) < 2:
                stats.rows_malformed += 1
                stats.errors.append(f"Ligne {line_no}: moins de 2 colonnes")
                continue

            code_cis = clean_field(cols[0])
            condition = clean_field(cols[1])
            if not code_cis or not CIS_PATTERN.match(code_cis) or not condition:
                stats.rows_malformed += 1
                stats.errors.append(f"Ligne {line_no}: code CIS ou condition invalide")
                continue

            if code_cis not in known_cis:
                stats.rows_skipped_orphan += 1
                continue

            dedup_key = (code_cis, condition)
            if dedup_key in seen_keys:
                stats.rows_skipped_duplicate += 1
                continue
            seen_keys.add(dedup_key)

            batch.append((code_cis, condition))
            stats.rows_imported += 1

    conn.executemany(
        """
        INSERT INTO conditions_prescription (code_cis, condition_prescription)
        VALUES (?, ?)
        """,
        batch,
    )


def _import_groupes_generiques(
    conn: sqlite3.Connection,
    file_path: Path,
    known_cis: set[str],
    report: ImportReport,
) -> None:
    stats = FileImportStats(filename="CIS_GENER_bdpm.txt", present=file_path.exists())
    report.files["CIS_GENER_bdpm.txt"] = stats
    if not file_path.exists():
        return

    seen_keys: set[tuple] = set()
    batch: list[tuple] = []

    with file_path.open("rb") as f:
        for line_no, raw_line in enumerate(f, start=1):
            if not raw_line.strip():
                continue
            stats.lines_read += 1
            line = decode_bdpm_line(raw_line).rstrip("\r\n")
            cols = line.split("\t")
            if len(cols) < 4:
                stats.rows_malformed += 1
                stats.errors.append(f"Ligne {line_no}: moins de 4 colonnes")
                continue

            id_groupe = clean_field(cols[0])
            libelle_groupe = clean_field(cols[1])
            code_cis = clean_field(cols[2])
            type_gen = clean_field(cols[3]) if len(cols) > 3 else None
            num_tri = clean_field(cols[4]) if len(cols) > 4 else None

            if not id_groupe or not libelle_groupe or not code_cis or not CIS_PATTERN.match(code_cis):
                stats.rows_malformed += 1
                stats.errors.append(f"Ligne {line_no}: champs de groupe générique invalides")
                continue

            if code_cis not in known_cis:
                stats.rows_skipped_orphan += 1
                continue

            dedup_key = (id_groupe, code_cis, type_gen)
            if dedup_key in seen_keys:
                stats.rows_skipped_duplicate += 1
                continue
            seen_keys.add(dedup_key)

            batch.append(
                (
                    id_groupe,
                    libelle_groupe,
                    normalize_text(libelle_groupe),
                    code_cis,
                    type_gen,
                    num_tri,
                )
            )
            stats.rows_imported += 1

    conn.executemany(
        """
        INSERT INTO groupes_generiques (
            identifiant_groupe, libelle_groupe, libelle_groupe_normalise,
            code_cis, type_generique, numero_tri
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        batch,
    )


def _import_infos_importantes(
    conn: sqlite3.Connection,
    file_path: Path,
    known_cis: set[str],
    report: ImportReport,
) -> None:
    stats = FileImportStats(filename="CIS_InfoImportantes.txt", present=file_path.exists())
    report.files["CIS_InfoImportantes.txt"] = stats
    if not file_path.exists():
        return

    seen_keys: set[tuple] = set()
    batch: list[tuple] = []

    with file_path.open("rb") as f:
        for line_no, raw_line in enumerate(f, start=1):
            if not raw_line.strip():
                continue
            stats.lines_read += 1
            line = decode_bdpm_line(raw_line).rstrip("\r\n")
            cols = line.split("\t")
            if len(cols) < 4:
                stats.rows_malformed += 1
                stats.errors.append(f"Ligne {line_no}: moins de 4 colonnes")
                continue

            code_cis = clean_field(cols[0])
            date_debut = clean_field(cols[1])
            date_fin = clean_field(cols[2])
            raw_html = clean_field(cols[3])

            if not code_cis or not CIS_PATTERN.match(code_cis) or not raw_html:
                stats.rows_malformed += 1
                stats.errors.append(f"Ligne {line_no}: code CIS ou texte d'alerte manquant")
                continue

            if code_cis not in known_cis:
                stats.rows_skipped_orphan += 1
                continue

            texte_info, lien_ansm = parse_info_importante_html(raw_html)
            if not texte_info:
                stats.rows_malformed += 1
                continue

            dedup_key = (code_cis, date_debut, date_fin, texte_info)
            if dedup_key in seen_keys:
                stats.rows_skipped_duplicate += 1
                continue
            seen_keys.add(dedup_key)

            batch.append((code_cis, date_debut, date_fin, texte_info, lien_ansm))
            stats.rows_imported += 1

    conn.executemany(
        """
        INSERT INTO infos_importantes (
            code_cis, date_debut, date_fin, texte_information, lien_ansm
        ) VALUES (?, ?, ?, ?, ?)
        """,
        batch,
    )


# ==============================================================================
# Suivi de fraîcheur (`sources_fraicheur`) et importateurs sûrs du Lot 2C
# ==============================================================================

def _source_descriptor(source_key: str) -> dict:
    info = OFFICIAL_SOURCES.get(source_key)
    if info:
        return info
    return {
        "domaine": "BDPM",
        "label": source_key,
        "url": None,
        "table": None,
        "is_optional": True,
    }


def _record_source_attempt_only(
    conn: sqlite3.Connection,
    *,
    source_key: str,
    file_present: bool,
    download_status: str,
    import_status: str,
    attempt_at_utc: str,
    attempt_error: str | None,
    stats: FileImportStats,
    active_rows_count: int,
) -> None:
    """Met à jour UNIQUEMENT les champs de dernière tentative dans `sources_fraicheur`.

    Les champs `last_success_*` restent strictement inchangés afin qu'une source absente,
    vide, malformée ou en échec ne masque jamais l'ancienneté des données préservées.
    """
    desc = _source_descriptor(source_key)
    conn.execute(
        """
        INSERT INTO sources_fraicheur (
            source_key, domaine, source_label, source_url, is_optional,
            file_present, download_status, import_status,
            last_attempt_at_utc, last_attempt_error,
            last_attempt_lines_read, last_attempt_rows_imported,
            last_attempt_skipped_duplicate, last_attempt_skipped_orphan, last_attempt_malformed,
            active_rows_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_key) DO UPDATE SET
            domaine = excluded.domaine,
            source_label = excluded.source_label,
            source_url = excluded.source_url,
            is_optional = excluded.is_optional,
            file_present = excluded.file_present,
            download_status = excluded.download_status,
            import_status = excluded.import_status,
            last_attempt_at_utc = excluded.last_attempt_at_utc,
            last_attempt_error = excluded.last_attempt_error,
            last_attempt_lines_read = excluded.last_attempt_lines_read,
            last_attempt_rows_imported = excluded.last_attempt_rows_imported,
            last_attempt_skipped_duplicate = excluded.last_attempt_skipped_duplicate,
            last_attempt_skipped_orphan = excluded.last_attempt_skipped_orphan,
            last_attempt_malformed = excluded.last_attempt_malformed,
            active_rows_count = excluded.active_rows_count
        """,
        (
            source_key,
            desc["domaine"],
            desc["label"],
            desc["url"],
            1 if desc.get("is_optional", True) else 0,
            1 if file_present else 0,
            download_status,
            import_status,
            attempt_at_utc,
            attempt_error,
            stats.lines_read,
            stats.rows_imported,
            stats.rows_skipped_duplicate,
            stats.rows_skipped_orphan,
            stats.rows_malformed,
            active_rows_count,
        ),
    )


def _record_source_success(
    conn: sqlite3.Connection,
    *,
    source_key: str,
    download_status: str,
    attempt_at_utc: str,
    stats: FileImportStats,
    active_rows_count: int,
    downloaded_at_utc: str | None,
    official_date: str | None,
    official_date_origin: str | None,
    http_last_modified: str | None,
    file_sha256: str | None,
    file_size_bytes: int | None,
) -> None:
    """Enregistre une importation réussie (tentative + champs `last_success_*`).

    `http_last_modified` est stocké séparément de `last_success_official_date` et
    n'est jamais substitué à une date officielle de publication.
    """
    desc = _source_descriptor(source_key)
    conn.execute(
        """
        INSERT INTO sources_fraicheur (
            source_key, domaine, source_label, source_url, is_optional,
            file_present, download_status, import_status,
            last_attempt_at_utc, last_attempt_error,
            last_attempt_lines_read, last_attempt_rows_imported,
            last_attempt_skipped_duplicate, last_attempt_skipped_orphan, last_attempt_malformed,
            last_success_imported_at_utc, last_success_downloaded_at_utc,
            last_success_official_date, last_success_official_date_origin,
            http_last_modified, last_success_sha256, last_success_size_bytes,
            active_rows_count, historical_unattached_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_key) DO UPDATE SET
            domaine = excluded.domaine,
            source_label = excluded.source_label,
            source_url = excluded.source_url,
            is_optional = excluded.is_optional,
            file_present = excluded.file_present,
            download_status = excluded.download_status,
            import_status = excluded.import_status,
            last_attempt_at_utc = excluded.last_attempt_at_utc,
            last_attempt_error = NULL,
            last_attempt_lines_read = excluded.last_attempt_lines_read,
            last_attempt_rows_imported = excluded.last_attempt_rows_imported,
            last_attempt_skipped_duplicate = excluded.last_attempt_skipped_duplicate,
            last_attempt_skipped_orphan = excluded.last_attempt_skipped_orphan,
            last_attempt_malformed = excluded.last_attempt_malformed,
            last_success_imported_at_utc = excluded.last_success_imported_at_utc,
            last_success_downloaded_at_utc = excluded.last_success_downloaded_at_utc,
            last_success_official_date = excluded.last_success_official_date,
            last_success_official_date_origin = excluded.last_success_official_date_origin,
            http_last_modified = excluded.http_last_modified,
            last_success_sha256 = excluded.last_success_sha256,
            last_success_size_bytes = excluded.last_success_size_bytes,
            active_rows_count = excluded.active_rows_count,
            historical_unattached_count = excluded.historical_unattached_count
        """,
        (
            source_key,
            desc["domaine"],
            desc["label"],
            desc["url"],
            1 if desc.get("is_optional", True) else 0,
            1,
            download_status,
            "ok",
            attempt_at_utc,
            None,
            stats.lines_read,
            stats.rows_imported,
            stats.rows_skipped_duplicate,
            stats.rows_skipped_orphan,
            stats.rows_malformed,
            attempt_at_utc,
            downloaded_at_utc,
            official_date,
            official_date_origin,
            http_last_modified,
            file_sha256,
            file_size_bytes,
            active_rows_count,
            stats.rows_historical_unattached,
        ),
    )


def _record_palier1_freshness(
    conn: sqlite3.Connection,
    raw_path: Path,
    report: ImportReport,
    download_meta: dict[str, dict],
    imported_at: str,
    fallback_official_date: str | None,
) -> None:
    """Enregistre l'état de fraîcheur des 6 sources du Palier 1 dans `sources_fraicheur`."""
    palier1_map = (
        ("CIS_bdpm.txt", "specialites"),
        ("CIS_COMPO_bdpm.txt", "compositions"),
        ("CIS_CIP_bdpm.txt", "presentations"),
        ("CIS_CPD_bdpm.txt", "conditions_prescription"),
        ("CIS_GENER_bdpm.txt", "groupes_generiques"),
        ("CIS_InfoImportantes.txt", "infos_importantes"),
    )
    for fname, table_name in palier1_map:
        stats = report.files.get(fname, FileImportStats(filename=fname, present=False))
        file_path = raw_path / fname
        dl = download_meta.get(fname, {})
        active_cnt = conn.execute(f"SELECT COUNT(*) FROM \"{table_name}\";").fetchone()[0]
        stats.active_rows_count = active_cnt

        if not file_path.exists():
            stats.import_status = "skipped_missing"
            stats.download_status = dl.get("status", "missing")
            _record_source_attempt_only(
                conn,
                source_key=fname,
                file_present=False,
                download_status=stats.download_status,
                import_status="skipped_missing",
                attempt_at_utc=imported_at,
                attempt_error="Fichier absent lors de l'importation",
                stats=stats,
                active_rows_count=active_cnt,
            )
            continue

        stats.import_status = "ok"
        stats.download_status = dl.get("status", "not_tracked")
        official_date = dl.get("official_source_date")
        official_origin = dl.get("official_date_verified_from")
        if official_date is None and fname != "CIS_InfoImportantes.txt" and fallback_official_date:
            official_date = fallback_official_date
            official_origin = "Date globale portail BDPM (metadata_telechargement.txt)"
        elif official_origin is None:
            official_origin = (
                "Génération en direct sur le portail BDPM (pas de date fixe)"
                if fname == "CIS_InfoImportantes.txt"
                else "Non fournie par les métadonnées locales"
            )

        _record_source_success(
            conn,
            source_key=fname,
            download_status=stats.download_status,
            attempt_at_utc=imported_at,
            stats=stats,
            active_rows_count=active_cnt,
            downloaded_at_utc=dl.get("downloaded_at_utc"),
            official_date=official_date,
            official_date_origin=official_origin,
            http_last_modified=dl.get("http_last_modified"),
            file_sha256=dl.get("sha256") or _compute_file_sha256(file_path),
            file_size_bytes=file_path.stat().st_size,
        )


def _parse_disponibilites_spec(
    file_path: Path,
    known_cis: set[str],
) -> tuple[list[tuple], FileImportStats]:
    """Parse et valide `CIS_CIP_Dispo_Spec.txt` (8 colonnes officielles).

    - Conserve les multiples événements et dates de mise à jour par CIS/CIP13.
    - Déduplique uniquement les lignes strictement identiques.
    - Ne confond jamais tension (code 2) et rupture (code 1).
    """
    stats = FileImportStats(filename="CIS_CIP_Dispo_Spec.txt", present=True)
    seen_exact: set[tuple] = set()
    batch: list[tuple] = []

    with file_path.open("rb") as f:
        for line_no, raw_line in enumerate(f, start=1):
            if not raw_line.strip():
                continue
            stats.lines_read += 1
            line = decode_bdpm_line(raw_line).rstrip("\r\n")
            cols = line.split("\t")
            if len(cols) < 6:
                stats.rows_malformed += 1
                stats.errors.append(
                    f"Ligne {line_no}: nombre de colonnes insuffisant ({len(cols)} < 6)"
                )
                continue

            code_cis = clean_field(cols[0])
            code_cip13 = clean_field(cols[1])
            code_statut = clean_field(cols[2])
            libelle_statut = clean_field(cols[3])
            date_debut, ok_debut = validate_fr_date(cols[4])
            date_maj, ok_maj = validate_fr_date(cols[5])
            date_remise, ok_remise = validate_fr_date(cols[6] if len(cols) > 6 else None)
            lien_ansm, ok_url = validate_http_url(cols[7] if len(cols) > 7 else None)

            if (
                not code_cis
                or not CIS_PATTERN.match(code_cis)
                or (code_cip13 is not None and not CIP13_PATTERN.match(code_cip13))
                or not code_statut
                or not code_statut.isdigit()
                or not libelle_statut
                or not ok_debut
                or not ok_maj
                or not ok_remise
                or not ok_url
            ):
                stats.rows_malformed += 1
                stats.errors.append(f"Ligne {line_no}: champs de disponibilité invalides")
                continue

            if code_cis not in known_cis:
                stats.rows_skipped_orphan += 1
                continue

            exact_key = (
                code_cis,
                code_cip13,
                code_statut,
                libelle_statut,
                date_debut,
                date_maj,
                date_remise,
                lien_ansm,
            )
            if exact_key in seen_exact:
                stats.rows_skipped_duplicate += 1
                continue
            seen_exact.add(exact_key)
            batch.append(exact_key)
            stats.rows_imported += 1

    return batch, stats


def _parse_has_liens_ct(
    file_path: Path,
    known_cis: set[str],
) -> tuple[list[tuple], FileImportStats]:
    """Parse et valide `HAS_LiensPageCT_bdpm.txt` (2 colonnes officielles).

    Autorise plusieurs URLs officielles pour un même `code_dossier_has` et déduplique
    strictement sur le couple `(code_dossier_has, lien_page_ct)`.
    """
    _ = known_cis  # Ce fichier est indexé par `code_dossier_has` (sans colonne `code_cis`)
    stats = FileImportStats(filename="HAS_LiensPageCT_bdpm.txt", present=True)
    seen_pairs: set[tuple[str, str]] = set()
    batch: list[tuple[str, str]] = []

    with file_path.open("rb") as f:
        for line_no, raw_line in enumerate(f, start=1):
            if not raw_line.strip():
                continue
            stats.lines_read += 1
            line = decode_bdpm_line(raw_line).rstrip("\r\n")
            cols = line.split("\t")
            if len(cols) < 2:
                stats.rows_malformed += 1
                stats.errors.append(f"Ligne {line_no}: moins de 2 colonnes")
                continue

            code_dossier = clean_field(cols[0])
            lien_ct, ok_url = validate_http_url(cols[1])
            if not code_dossier or not lien_ct or not ok_url:
                stats.rows_malformed += 1
                stats.errors.append(f"Ligne {line_no}: dossier HAS ou URL CT invalide")
                continue

            pair = (code_dossier, lien_ct)
            if pair in seen_pairs:
                stats.rows_skipped_duplicate += 1
                continue
            seen_pairs.add(pair)
            batch.append(pair)
            stats.rows_imported += 1

    return batch, stats


def _parse_has_avis_file(
    file_path: Path,
    known_cis: set[str],
    filename: str,
) -> tuple[list[tuple], FileImportStats]:
    """Parse et valide `CIS_HAS_SMR_bdpm.txt` ou `CIS_HAS_ASMR_bdpm.txt` (6 colonnes).

    - Préserve intégralement chaque indication distincte (jamais de fusion ni note globale).
    - Nettoie uniquement les balises `<br>` via `clean_has_medical_text()`, sans jamais altérer
      les expressions médicales telles que `< 18 ans`, `p < 0,0001` ou `< -3 DS`.
    - Compte et signale les codes CIS historiques absents de `specialites`.
    """
    stats = FileImportStats(filename=filename, present=True)
    seen_exact: set[tuple] = set()
    batch: list[tuple] = []

    with file_path.open("rb") as f:
        for line_no, raw_line in enumerate(f, start=1):
            if not raw_line.strip():
                continue
            stats.lines_read += 1
            line = decode_bdpm_line(raw_line).rstrip("\r\n")
            cols = line.split("\t")
            if len(cols) < 6:
                stats.rows_malformed += 1
                stats.errors.append(
                    f"Ligne {line_no}: nombre de colonnes insuffisant ({len(cols)} < 6)"
                )
                continue

            code_cis = clean_field(cols[0])
            code_dossier = clean_field(cols[1])
            motif = clean_field(cols[2])
            date_avis, ok_date = normalize_has_date(cols[3])
            valeur = clean_field(cols[4])
            libelle = clean_has_medical_text(cols[5])

            if (
                not code_cis
                or not CIS_PATTERN.match(code_cis)
                or not code_dossier
                or not ok_date
                or not libelle
            ):
                stats.rows_malformed += 1
                stats.errors.append(f"Ligne {line_no}: champs d'avis HAS invalides")
                continue

            is_present = 1 if code_cis in known_cis else 0
            exact_key = (code_cis, code_dossier, motif, date_avis, valeur, libelle)
            if exact_key in seen_exact:
                stats.rows_skipped_duplicate += 1
                continue
            seen_exact.add(exact_key)
            if is_present == 0:
                stats.rows_historical_unattached += 1
            batch.append((*exact_key, is_present))
            stats.rows_imported += 1

    return batch, stats


def _parse_has_avis_smr(
    file_path: Path,
    known_cis: set[str],
) -> tuple[list[tuple], FileImportStats]:
    return _parse_has_avis_file(file_path, known_cis, "CIS_HAS_SMR_bdpm.txt")


def _parse_has_avis_asmr(
    file_path: Path,
    known_cis: set[str],
) -> tuple[list[tuple], FileImportStats]:
    return _parse_has_avis_file(file_path, known_cis, "CIS_HAS_ASMR_bdpm.txt")


def _parse_specialites_mitm(
    file_path: Path,
    known_cis: set[str],
) -> tuple[list[tuple], FileImportStats]:
    """Parse et valide `CIS_MITM.txt` selon ses 7 colonnes réelles vérifiées :
    `code_cis`, `code_atc`, `denomination`, `titulaire`, `exploitant` (facultatif),
    `date_publication` (`JJ/MM/AAAA`), `lien_bdpm`.
    """
    stats = FileImportStats(filename="CIS_MITM.txt", present=True)
    seen_cis: set[str] = set()
    batch: list[tuple] = []

    with file_path.open("rb") as f:
        for line_no, raw_line in enumerate(f, start=1):
            if not raw_line.strip():
                continue
            stats.lines_read += 1
            line = decode_bdpm_line(raw_line).rstrip("\r\n")
            cols = line.split("\t")
            if len(cols) < 7:
                stats.rows_malformed += 1
                stats.errors.append(
                    f"Ligne {line_no}: nombre de colonnes insuffisant ({len(cols)} < 7)"
                )
                continue

            code_cis = clean_field(cols[0])
            code_atc = clean_field(cols[1])
            denomination = clean_field(cols[2])
            titulaire = clean_field(cols[3])
            exploitant = clean_field(cols[4])  # facultatif (20 lignes vides constatées)
            date_pub, ok_date = validate_fr_date(cols[5])
            lien_bdpm, ok_url = validate_http_url(cols[6])

            if (
                not code_cis
                or not CIS_PATTERN.match(code_cis)
                or not denomination
                or not ok_date
                or not ok_url
            ):
                stats.rows_malformed += 1
                stats.errors.append(f"Ligne {line_no}: champs MITM invalides")
                continue

            if code_cis not in known_cis:
                stats.rows_skipped_orphan += 1
                continue

            if code_cis in seen_cis:
                stats.rows_skipped_duplicate += 1
                continue
            seen_cis.add(code_cis)
            batch.append((code_cis, code_atc, denomination, titulaire, exploitant, date_pub, lien_bdpm))
            stats.rows_imported += 1

    return batch, stats


def _replace_lot2c_table_safely(
    conn: sqlite3.Connection,
    *,
    source_key: str,
    table_name: str,
    file_path: Path,
    known_cis: set[str],
    parse_fn,
    insert_sql: str,
    download_meta: dict[str, dict],
    attempt_at_utc: str,
    report: ImportReport,
) -> FileImportStats:
    """Valide une source facultative du Lot 2C avant tout remplacement, puis la charge
    dans un `SAVEPOINT` SQLite isolé.

    Garanties strictes :
    - Si le téléchargement est enregistré en échec (`status == 'error'`) -> préserve les anciennes données.
    - Si le fichier est absent -> préserve les anciennes données.
    - Si le fichier est vide ou malformé -> préserve les anciennes données.
    - Si une erreur SQLite survient pendant le remplacement -> `ROLLBACK TO SAVEPOINT` complet,
      les anciennes données restent 100 % intactes et l'échec est journalisé dans `sources_fraicheur`
      sans modifier `last_success_*`.
    """
    existing_rows = conn.execute(f"SELECT COUNT(*) FROM \"{table_name}\";").fetchone()[0]
    dl = download_meta.get(source_key, {})
    dl_status = dl.get("status")

    # 1. Protection en cas d'échec de téléchargement enregistré dans metadata_sources.json
    if dl_status == "error":
        err_msg = dl.get("error") or "Échec du dernier téléchargement de la source"
        stats = FileImportStats(
            filename=source_key,
            present=file_path.exists(),
            import_status="preserved_download_error",
            download_status="error",
            preserved_previous_data=existing_rows > 0,
            active_rows_count=existing_rows,
            errors=[f"Téléchargement en échec ({err_msg}) : anciennes données préservées."],
        )
        report.files[source_key] = stats
        _record_source_attempt_only(
            conn,
            source_key=source_key,
            file_present=file_path.exists(),
            download_status="error",
            import_status="preserved_download_error",
            attempt_at_utc=attempt_at_utc,
            attempt_error=err_msg,
            stats=stats,
            active_rows_count=existing_rows,
        )
        return stats

    # 2. Protection en cas de fichier absent
    if not file_path.exists():
        stats = FileImportStats(
            filename=source_key,
            present=False,
            import_status="skipped_missing",
            download_status=dl_status or "missing",
            preserved_previous_data=existing_rows > 0,
            active_rows_count=existing_rows,
            errors=["Fichier source absent : anciennes données préservées."],
        )
        report.files[source_key] = stats
        _record_source_attempt_only(
            conn,
            source_key=source_key,
            file_present=False,
            download_status=stats.download_status,
            import_status="skipped_missing",
            attempt_at_utc=attempt_at_utc,
            attempt_error="Fichier source absent dans le dossier raw",
            stats=stats,
            active_rows_count=existing_rows,
        )
        return stats

    # 3. Parsing et validation complète en mémoire (aucune écriture SQLite à ce stade)
    effective_dl_status = dl_status or "not_tracked"
    batch, stats = parse_fn(file_path, known_cis)
    stats.download_status = effective_dl_status

    # Rejet d'un fichier vide
    if stats.lines_read == 0:
        stats.import_status = "rejected_empty"
        stats.preserved_previous_data = existing_rows > 0
        stats.active_rows_count = existing_rows
        stats.errors.append("Fichier source vide : remplacement annulé, anciennes données préservées.")
        report.files[source_key] = stats
        _record_source_attempt_only(
            conn,
            source_key=source_key,
            file_present=True,
            download_status=effective_dl_status,
            import_status="rejected_empty",
            attempt_at_utc=attempt_at_utc,
            attempt_error="Fichier vide (0 ligne exploitable)",
            stats=stats,
            active_rows_count=existing_rows,
        )
        return stats

    # Rejet d'un fichier malformé / corrompu avant tout effacement
    structurally_valid = stats.rows_imported + stats.rows_skipped_duplicate + stats.rows_skipped_orphan
    malformed_ratio = stats.rows_malformed / stats.lines_read
    is_rejected_malformed = (
        structurally_valid == 0
        or (stats.rows_malformed > 0 and stats.rows_imported == 0 and stats.rows_skipped_orphan == 0)
        or (existing_rows > 0 and stats.rows_malformed > 0 and malformed_ratio > 0.5)
    )
    if is_rejected_malformed:
        stats.import_status = "rejected_malformed"
        stats.preserved_previous_data = existing_rows > 0
        stats.active_rows_count = existing_rows
        stats.errors.append(
            f"Fichier malformé ({stats.rows_malformed}/{stats.lines_read} lignes invalides) : "
            "remplacement annulé, anciennes données préservées."
        )
        report.files[source_key] = stats
        _record_source_attempt_only(
            conn,
            source_key=source_key,
            file_present=True,
            download_status=effective_dl_status,
            import_status="rejected_malformed",
            attempt_at_utc=attempt_at_utc,
            attempt_error=f"Fichier malformé ({stats.rows_malformed}/{stats.lines_read} lignes invalides)",
            stats=stats,
            active_rows_count=existing_rows,
        )
        return stats

    # 4. Remplacement atomique protégé par SAVEPOINT SQLite
    file_sha256 = _compute_file_sha256(file_path)
    file_size_bytes = file_path.stat().st_size
    official_date = dl.get("official_source_date")
    official_origin = dl.get("official_date_verified_from") or (
        "Non fournie par la source" if official_date is None else "Métadonnées locales"
    )

    savepoint = f"sp_{table_name}"
    conn.execute(f"SAVEPOINT {savepoint};")
    try:
        conn.execute(f"DELETE FROM \"{table_name}\";")
        if batch:
            conn.executemany(insert_sql, batch)
        active_cnt = conn.execute(f"SELECT COUNT(*) FROM \"{table_name}\";").fetchone()[0]
        stats.import_status = "ok"
        stats.preserved_previous_data = False
        stats.active_rows_count = active_cnt
        _record_source_success(
            conn,
            source_key=source_key,
            download_status=effective_dl_status,
            attempt_at_utc=attempt_at_utc,
            stats=stats,
            active_rows_count=active_cnt,
            downloaded_at_utc=dl.get("downloaded_at_utc"),
            official_date=official_date,
            official_date_origin=official_origin,
            http_last_modified=dl.get("http_last_modified"),
            file_sha256=file_sha256,
            file_size_bytes=file_size_bytes,
        )
        conn.execute(f"RELEASE SAVEPOINT {savepoint};")
    except Exception as exc:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint};")
        conn.execute(f"RELEASE SAVEPOINT {savepoint};")
        restored_cnt = conn.execute(f"SELECT COUNT(*) FROM \"{table_name}\";").fetchone()[0]
        stats.import_status = "error_rollback"
        stats.rows_imported = 0
        stats.preserved_previous_data = restored_cnt > 0
        stats.active_rows_count = restored_cnt
        stats.errors.append(f"Erreur SQLite annulée par rollback : {exc}")
        _record_source_attempt_only(
            conn,
            source_key=source_key,
            file_present=True,
            download_status=effective_dl_status,
            import_status="error_rollback",
            attempt_at_utc=attempt_at_utc,
            attempt_error=f"Erreur SQLite annulée par rollback : {exc}",
            stats=stats,
            active_rows_count=restored_cnt,
        )

    report.files[source_key] = stats
    return stats


def _run_lot2c_importers_on_conn(
    conn: sqlite3.Connection,
    raw_path: Path,
    known_cis: set[str],
    report: ImportReport,
    download_meta: dict[str, dict],
    attempt_at_utc: str,
) -> None:
    """Exécute les 5 importateurs du Lot 2C sur une connexion SQLite déjà ouverte."""
    specs = (
        (
            "CIS_CIP_Dispo_Spec.txt",
            "disponibilites_spec",
            _parse_disponibilites_spec,
            """
            INSERT INTO disponibilites_spec (
                code_cis, code_cip13, code_statut, libelle_statut,
                date_debut, date_mise_a_jour, date_remise_disposition, lien_ansm
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
        ),
        (
            "HAS_LiensPageCT_bdpm.txt",
            "has_liens_ct",
            _parse_has_liens_ct,
            """
            INSERT INTO has_liens_ct (code_dossier_has, lien_page_ct)
            VALUES (?, ?)
            """,
        ),
        (
            "CIS_HAS_SMR_bdpm.txt",
            "has_avis_smr",
            _parse_has_avis_smr,
            """
            INSERT INTO has_avis_smr (
                code_cis, code_dossier_has, motif_evaluation,
                date_avis, valeur_smr, libelle_smr, specialite_presente
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
        ),
        (
            "CIS_HAS_ASMR_bdpm.txt",
            "has_avis_asmr",
            _parse_has_avis_asmr,
            """
            INSERT INTO has_avis_asmr (
                code_cis, code_dossier_has, motif_evaluation,
                date_avis, valeur_asmr, libelle_asmr, specialite_presente
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
        ),
        (
            "CIS_MITM.txt",
            "specialites_mitm",
            _parse_specialites_mitm,
            """
            INSERT INTO specialites_mitm (
                code_cis, code_atc, denomination, titulaire,
                exploitant, date_publication, lien_bdpm
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
        ),
    )

    for source_key, table_name, parse_fn, insert_sql in specs:
        _replace_lot2c_table_safely(
            conn,
            source_key=source_key,
            table_name=table_name,
            file_path=raw_path / source_key,
            known_cis=known_cis,
            parse_fn=parse_fn,
            insert_sql=insert_sql,
            download_meta=download_meta,
            attempt_at_utc=attempt_at_utc,
            report=report,
        )


def _refresh_has_specialite_links(conn: sqlite3.Connection) -> None:
    """Met à jour l'indicateur `specialite_presente` dans `has_avis_smr` et `has_avis_asmr`
    ainsi que `historical_unattached_count` dans `sources_fraicheur`.

    Permet de rattacher automatiquement un avis historique dès que son `code_cis`
    réapparaît dans `specialites`, sans même réimporter les fichiers HAS.
    """
    for table_name, source_key in (
        ("has_avis_smr", "CIS_HAS_SMR_bdpm.txt"),
        ("has_avis_asmr", "CIS_HAS_ASMR_bdpm.txt"),
    ):
        conn.execute(
            f"""
            UPDATE "{table_name}"
            SET specialite_presente = CASE
                WHEN EXISTS (
                    SELECT 1 FROM specialites s WHERE s.code_cis = "{table_name}".code_cis
                ) THEN 1
                ELSE 0
            END;
            """
        )
        unattached = conn.execute(
            f'SELECT COUNT(*) FROM "{table_name}" WHERE specialite_presente = 0;'
        ).fetchone()[0]
        conn.execute(
            """
            UPDATE sources_fraicheur
            SET historical_unattached_count = ?
            WHERE source_key = ?;
            """,
            (unattached, source_key),
        )


def _backfill_palier1_freshness_if_missing(
    conn: sqlite3.Connection,
    raw_path: Path,
    download_meta: dict[str, dict],
) -> None:
    """Initialise les 6 lignes Palier 1 dans `sources_fraicheur` à partir des métadonnées
    réelles déjà enregistrées (`import_metadata`, `metadata_telechargement.txt`, compteurs
    SQLite actuels et fichiers `data/raw/`), SANS réimporter les 6 sources Palier 1 et SANS
    inventer de date officielle fictive.
    """
    existing_keys = {
        row["source_key"]
        for row in conn.execute("SELECT source_key FROM sources_fraicheur;").fetchall()
    }
    meta_rows = {
        row["key"]: row["value"]
        for row in conn.execute("SELECT key, value FROM import_metadata;").fetchall()
    }
    db_imported_at = meta_rows.get("imported_at_utc")
    db_official_date = meta_rows.get("official_bdpm_date")
    if db_official_date in (None, "", "Non précisée dans les fichiers locaux", "non_detectee"):
        db_official_date = None

    palier1_map = (
        ("CIS_bdpm.txt", "specialites"),
        ("CIS_COMPO_bdpm.txt", "compositions"),
        ("CIS_CIP_bdpm.txt", "presentations"),
        ("CIS_CPD_bdpm.txt", "conditions_prescription"),
        ("CIS_GENER_bdpm.txt", "groupes_generiques"),
        ("CIS_InfoImportantes.txt", "infos_importantes"),
    )

    for fname, table_name in palier1_map:
        if fname in existing_keys:
            continue

        file_path = raw_path / fname
        file_exists = file_path.exists()
        dl = download_meta.get(fname, {})
        active_cnt = conn.execute(f'SELECT COUNT(*) FROM "{table_name}";').fetchone()[0]
        dl_status = dl.get("status") or ("ok" if file_exists else "missing")
        downloaded_at = dl.get("downloaded_at_utc")
        http_last_mod = dl.get("http_last_modified")

        # Aucune date officielle fictive :
        # - CIS_InfoImportantes.txt est généré en direct -> official_date = None
        # - les 5 autres sources reprennent leur date enregistrée (ou la date globale relevée)
        if fname == "CIS_InfoImportantes.txt":
            official_date = None
            official_origin = "Génération en direct sur le portail BDPM (pas de date fixe)"
        else:
            official_date = dl.get("official_source_date") or db_official_date
            if dl.get("official_source_date"):
                official_origin = dl.get("official_date_verified_from") or "Métadonnées de téléchargement"
            elif db_official_date:
                official_origin = "Date globale portail BDPM (import_metadata)"
            else:
                official_origin = "Non fournie dans les métadonnées existantes"

        sha256_val = dl.get("sha256") or (_compute_file_sha256(file_path) if file_exists else None)
        size_val = file_path.stat().st_size if file_exists else dl.get("size_bytes")

        desc = _source_descriptor(fname)
        conn.execute(
            """
            INSERT INTO sources_fraicheur (
                source_key, domaine, source_label, source_url, is_optional,
                file_present, download_status, import_status,
                last_attempt_at_utc, last_attempt_error,
                last_attempt_lines_read, last_attempt_rows_imported,
                last_attempt_skipped_duplicate, last_attempt_skipped_orphan, last_attempt_malformed,
                last_success_imported_at_utc, last_success_downloaded_at_utc,
                last_success_official_date, last_success_official_date_origin,
                http_last_modified, last_success_sha256, last_success_size_bytes,
                active_rows_count, historical_unattached_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fname,
                desc["domaine"],
                desc["label"],
                desc["url"],
                1 if desc.get("is_optional", True) else 0,
                1 if file_exists else 0,
                dl_status,
                "ok" if active_cnt > 0 or db_imported_at else "never_imported",
                db_imported_at,
                None,
                active_cnt,
                active_cnt,
                0,
                0,
                0,
                db_imported_at,
                downloaded_at,
                official_date,
                official_origin,
                http_last_mod,
                sha256_val,
                size_val,
                active_cnt,
                0,
            ),
        )


def import_lot2c_sources(
    raw_dir: Path | str = RAW_BDPM_DIR,
    db_path: Path | str = DEFAULT_DB_PATH,
    *,
    allow_schema_migration: bool = False,
) -> ImportReport:
    """Importe uniquement les 5 sources complémentaires du Lot 2C dans une base SQLite
    déjà initialisée avec les spécialités Palier 1, SANS modifier les 7 tables existantes.

    Initialise également (si absent) le suivi des 6 sources Palier 1 dans `sources_fraicheur`
    à partir des métadonnées existantes, afin que les 11 sources soient toutes suivies
    de façon cohérente.

    Par défaut, refuse de migrer automatiquement le schéma : la migration explicite
    (`migrate_lot2c_schema`) doit avoir été exécutée au préalable (sauf si
    `allow_schema_migration=True` est explicitement passé).
    """
    raw_path = Path(raw_dir)
    db_path_obj = Path(db_path)

    status = inspect_schema_status(db_path_obj)
    if not status["exists"] or status["palier1_missing"]:
        raise RuntimeError(
            "La base SQLite cible doit déjà contenir les tables Palier 1 avant d'importer les sources Lot 2C."
        )

    if not status["lot2c_migrated"]:
        if not allow_schema_migration:
            raise RuntimeError(
                "Le schéma Lot 2C n'est pas encore migré sur cette base. "
                f"Tables manquantes : {status['lot2c_missing']}. "
                "Exécutez d'abord `migrate_lot2c_schema()` (avec sauvegarde vérifiée)."
            )
        migrate_lot2c_schema(db_path_obj, create_backup=True)

    attempt_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    download_meta = _read_download_sources_metadata(raw_path)
    detected_date = _read_metadata_date(raw_path)

    report = ImportReport(
        imported_at_utc=attempt_at,
        official_bdpm_date=detected_date,
        source_name=BDPM_SOURCE_NAME,
        source_url=BDPM_BASE_URL,
    )

    with get_connection(db_path_obj) as conn:
        _backfill_palier1_freshness_if_missing(
            conn=conn,
            raw_path=raw_path,
            download_meta=download_meta,
        )
        known_cis = {
            row["code_cis"]
            for row in conn.execute("SELECT code_cis FROM specialites;").fetchall()
        }
        _run_lot2c_importers_on_conn(
            conn=conn,
            raw_path=raw_path,
            known_cis=known_cis,
            report=report,
            download_meta=download_meta,
            attempt_at_utc=attempt_at,
        )
        _refresh_has_specialite_links(conn)
        conn.commit()

    return report
