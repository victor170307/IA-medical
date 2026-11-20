"""Configuration centralisée de l'application.

Ce fichier regroupe les chemins des dossiers (données brutes, base SQLite,
templates HTML) et les adresses officielles de la Base de Données Publique
des Médicaments (BDPM).
"""

from pathlib import Path

# Dossier racine du projet : C:\Users\snxwe\.gemini\antigravity\scratch\bdpm_app
BASE_DIR = Path(__file__).resolve().parent.parent

# Dossiers de données
DATA_DIR = BASE_DIR / "data"
RAW_BDPM_DIR = DATA_DIR / "raw"
DEFAULT_DB_PATH = DATA_DIR / "bdpm.sqlite3"

# Dossiers de l'interface web
WEB_DIR = BASE_DIR / "app" / "web"
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

# Informations officielles de la source BDPM (Licence Ouverte / Article L. 161-40-1 CSS)
BDPM_SOURCE_NAME = "Base de données publique des médicaments (BDPM)"
BDPM_BASE_URL = "https://base-donnees-publique.medicaments.gouv.fr"
BDPM_DOWNLOAD_BASE_URL = f"{BDPM_BASE_URL}/download"

# Fichiers officiels téléchargeables de la BDPM utilisés au Palier 1
BDPM_FILES = {
    "CIS_bdpm.txt": f"{BDPM_DOWNLOAD_BASE_URL}/file/CIS_bdpm.txt",
    "CIS_COMPO_bdpm.txt": f"{BDPM_DOWNLOAD_BASE_URL}/file/CIS_COMPO_bdpm.txt",
    "CIS_CIP_bdpm.txt": f"{BDPM_DOWNLOAD_BASE_URL}/file/CIS_CIP_bdpm.txt",
    "CIS_CPD_bdpm.txt": f"{BDPM_DOWNLOAD_BASE_URL}/file/CIS_CPD_bdpm.txt",
    "CIS_GENER_bdpm.txt": f"{BDPM_DOWNLOAD_BASE_URL}/file/CIS_GENER_bdpm.txt",
    "CIS_InfoImportantes.txt": f"{BDPM_DOWNLOAD_BASE_URL}/CIS_InfoImportantes.txt",
}


def get_official_fiche_url(code_cis: str) -> str:
    """Construit le lien officiel vérifié vers la fiche BDPM d'une spécialité (par code CIS)."""
    return f"{BDPM_BASE_URL}/medicament/{code_cis}/extrait"
