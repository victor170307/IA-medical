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
import html
from pathlib import Path
import re
import sqlite3

from app.config import (
    BDPM_BASE_URL,
    BDPM_SOURCE_NAME,
    DEFAULT_DB_PATH,
    RAW_BDPM_DIR,
    get_official_fiche_url,
)
from app.database import get_connection, init_db
from app.search.normalizer import normalize_text

CIS_PATTERN = re.compile(r"^\d{8}$")
HREF_PATTERN = re.compile(r"""href\s*=\s*['"]([^'"]+)['"]""", re.IGNORECASE)
TAG_PATTERN = re.compile(r"<[^>]+>")


@dataclass
class FileImportStats:
    """Statistiques d'import pour un fichier BDPM donné."""

    filename: str
    present: bool = False
    lines_read: int = 0
    rows_imported: int = 0
    rows_skipped_duplicate: int = 0
    rows_skipped_orphan: int = 0
    rows_malformed: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class ImportReport:
    """Rapport complet retourné à la fin d'une importation BDPM."""

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
                    "rows_malformed": v.rows_malformed,
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
        return raw_line.decode("utf-8")
    except UnicodeDecodeError:
        return raw_line.decode("cp1252", errors="replace")


def clean_field(value: str | None) -> str | None:
    """Nettoie un champ texte et renvoie `None` s'il est vide."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped != "" else None


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
) -> ImportReport:
    """Importe les fichiers BDPM présents dans `raw_dir` vers la base SQLite `db_path`.

    Le fichier `CIS_bdpm.txt` est obligatoire car il contient la table maîtresse des
    spécialités (`code_cis`). Les autres fichiers (`CIS_COMPO_bdpm.txt`, `CIS_CIP_bdpm.txt`,
    `CIS_CPD_bdpm.txt`, `CIS_GENER_bdpm.txt`, `CIS_InfoImportantes.txt`) sont importés
    s'ils sont présents dans `raw_dir`.
    """
    raw_path = Path(raw_dir)
    cis_bdpm_path = raw_path / "CIS_bdpm.txt"
    if not cis_bdpm_path.exists():
        raise FileNotFoundError(
            f"Le fichier principal CIS_bdpm.txt est introuvable dans {raw_path}."
        )

    init_db(db_path)

    detected_date = official_bdpm_date or _read_metadata_date(raw_path)
    imported_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    report = ImportReport(
        imported_at_utc=imported_at,
        official_bdpm_date=detected_date,
        source_name=BDPM_SOURCE_NAME,
        source_url=BDPM_BASE_URL,
    )

    with get_connection(db_path) as conn:
        # Pour garantir un ré-import propre et sans doublons ni anciens enregistrements obsolètes,
        # on effectue l'opération dans une transaction atomique.
        conn.execute("DELETE FROM infos_importantes;")
        conn.execute("DELETE FROM groupes_generiques;")
        conn.execute("DELETE FROM conditions_prescription;")
        conn.execute("DELETE FROM presentations;")
        conn.execute("DELETE FROM compositions;")
        conn.execute("DELETE FROM specialites;")

        known_cis: set[str] = set()

        # 1. Import de CIS_bdpm.txt (Spécialités)
        spec_stats = FileImportStats(filename="CIS_bdpm.txt", present=True)
        report.files["CIS_bdpm.txt"] = spec_stats
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
            """,
            spec_batch,
        )

        # 2. Import de CIS_COMPO_bdpm.txt (Substances actives et dosages)
        _import_compositions(conn, raw_path / "CIS_COMPO_bdpm.txt", known_cis, report)

        # 3. Import de CIS_CIP_bdpm.txt (Présentations / Boîtes)
        _import_presentations(conn, raw_path / "CIS_CIP_bdpm.txt", known_cis, report)

        # 4. Import de CIS_CPD_bdpm.txt (Conditions de prescription et délivrance)
        _import_conditions_prescription(conn, raw_path / "CIS_CPD_bdpm.txt", known_cis, report)

        # 5. Import de CIS_GENER_bdpm.txt (Groupes génériques)
        _import_groupes_generiques(conn, raw_path / "CIS_GENER_bdpm.txt", known_cis, report)

        # 6. Import de CIS_InfoImportantes.txt (Alertes et informations de sécurité ANSM)
        _import_infos_importantes(conn, raw_path / "CIS_InfoImportantes.txt", known_cis, report)

        # 7. Enregistrement des métadonnées de traçabilité
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
