"""Script de gestion de la base BDPM (téléchargement, importation et statistiques).

Exemples d'utilisation depuis le dossier du projet :
    python scripts/manage_bdpm.py download-and-import
    python scripts/manage_bdpm.py import-only
    python scripts/manage_bdpm.py status
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

# Ajoute la racine du projet au chemin Python pour permettre l'exécution directe du script
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import DEFAULT_DB_PATH, RAW_BDPM_DIR
from app.importer.bdpm_importer import import_bdpm_directory
from app.importer.downloader import download_bdpm_files, download_complementary_bdpm_files
from app.search.service import get_db_metadata


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Outil de téléchargement et d'importation des données officielles de la BDPM."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "download",
        help="Télécharge les fichiers officiels de la BDPM dans data/raw/",
    )

    import_parser = subparsers.add_parser(
        "import-only",
        help="Importe les fichiers déjà présents dans data/raw/ vers SQLite",
    )
    import_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limite optionnelle du nombre de spécialités à importer",
    )

    both_parser = subparsers.add_parser(
        "download-and-import",
        help="Télécharge les fichiers officiels BDPM puis les importe dans SQLite",
    )
    both_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limite optionnelle du nombre de spécialités à importer",
    )

    subparsers.add_parser(
        "status",
        help="Affiche l'état actuel de la base SQLite locale",
    )

    subparsers.add_parser(
        "download-lot2c",
        help="Télécharge uniquement les 5 sources complémentaires du Lot 2C dans data/raw/",
    )

    args = parser.parse_args()

    if args.command in ("download", "download-and-import"):
        print(f"[1/2] Téléchargement des fichiers officiels BDPM vers {RAW_BDPM_DIR} ...")
        dl_info = download_bdpm_files(target_dir=RAW_BDPM_DIR)
        for fname, info in dl_info.items():
            print(
                f"  - {fname}: {info['size_bytes']:,} octets "
                f"(date officielle BDPM : {info['official_bdpm_date']})"
            )

    if args.command in ("import-only", "download-and-import"):
        print(f"[2/2] Importation dans la base SQLite {DEFAULT_DB_PATH} ...")
        report = import_bdpm_directory(
            raw_dir=RAW_BDPM_DIR,
            db_path=DEFAULT_DB_PATH,
            limit_specialites=getattr(args, "limit", None),
        )
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))

    if args.command == "status":
        meta = get_db_metadata(DEFAULT_DB_PATH)
        print(json.dumps(meta, indent=2, ensure_ascii=False))

    if args.command == "download-lot2c":
        info = download_complementary_bdpm_files(target_dir=RAW_BDPM_DIR)
        print(json.dumps(info, indent=2, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
