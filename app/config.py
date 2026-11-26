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
BACKUP_DIR = DATA_DIR / "backups"
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

# Sources complémentaires officielles du Lot 2C (URLs vérifiées sur /telechargement)
BDPM_LOT2C_FILES = {
    "CIS_CIP_Dispo_Spec.txt": f"{BDPM_DOWNLOAD_BASE_URL}/file/CIS_CIP_Dispo_Spec.txt",
    "CIS_HAS_SMR_bdpm.txt": f"{BDPM_DOWNLOAD_BASE_URL}/file/CIS_HAS_SMR_bdpm.txt",
    "CIS_HAS_ASMR_bdpm.txt": f"{BDPM_DOWNLOAD_BASE_URL}/file/CIS_HAS_ASMR_bdpm.txt",
    "HAS_LiensPageCT_bdpm.txt": f"{BDPM_DOWNLOAD_BASE_URL}/file/HAS_LiensPageCT_bdpm.txt",
    "CIS_MITM.txt": f"{BDPM_DOWNLOAD_BASE_URL}/file/CIS_MITM.txt",
}

# Registre descriptif des 11 sources officielles suivies par l'application
OFFICIAL_SOURCES: dict[str, dict] = {
    "CIS_bdpm.txt": {
        "domaine": "BDPM",
        "label": "Spécialités pharmaceutiques",
        "url": BDPM_FILES["CIS_bdpm.txt"],
        "table": "specialites",
        "is_optional": False,
    },
    "CIS_COMPO_bdpm.txt": {
        "domaine": "BDPM",
        "label": "Compositions (substances actives et dosages)",
        "url": BDPM_FILES["CIS_COMPO_bdpm.txt"],
        "table": "compositions",
        "is_optional": True,
    },
    "CIS_CIP_bdpm.txt": {
        "domaine": "BDPM",
        "label": "Présentations et codes CIP13/CIP7",
        "url": BDPM_FILES["CIS_CIP_bdpm.txt"],
        "table": "presentations",
        "is_optional": True,
    },
    "CIS_CPD_bdpm.txt": {
        "domaine": "BDPM",
        "label": "Conditions de prescription et délivrance",
        "url": BDPM_FILES["CIS_CPD_bdpm.txt"],
        "table": "conditions_prescription",
        "is_optional": True,
    },
    "CIS_GENER_bdpm.txt": {
        "domaine": "BDPM",
        "label": "Groupes génériques",
        "url": BDPM_FILES["CIS_GENER_bdpm.txt"],
        "table": "groupes_generiques",
        "is_optional": True,
    },
    "CIS_InfoImportantes.txt": {
        "domaine": "ANSM",
        "label": "Informations importantes ANSM",
        "url": BDPM_FILES["CIS_InfoImportantes.txt"],
        "table": "infos_importantes",
        "is_optional": True,
    },
    "CIS_CIP_Dispo_Spec.txt": {
        "domaine": "ANSM",
        "label": "Disponibilités, tensions et ruptures",
        "url": BDPM_LOT2C_FILES["CIS_CIP_Dispo_Spec.txt"],
        "table": "disponibilites_spec",
        "is_optional": True,
    },
    "CIS_HAS_SMR_bdpm.txt": {
        "domaine": "HAS",
        "label": "Avis HAS — Service Médical Rendu (SMR)",
        "url": BDPM_LOT2C_FILES["CIS_HAS_SMR_bdpm.txt"],
        "table": "has_avis_smr",
        "is_optional": True,
    },
    "CIS_HAS_ASMR_bdpm.txt": {
        "domaine": "HAS",
        "label": "Avis HAS — Amélioration du SMR (ASMR)",
        "url": BDPM_LOT2C_FILES["CIS_HAS_ASMR_bdpm.txt"],
        "table": "has_avis_asmr",
        "is_optional": True,
    },
    "HAS_LiensPageCT_bdpm.txt": {
        "domaine": "HAS",
        "label": "Liens officiels vers les avis CT de la HAS",
        "url": BDPM_LOT2C_FILES["HAS_LiensPageCT_bdpm.txt"],
        "table": "has_liens_ct",
        "is_optional": True,
    },
    "CIS_MITM.txt": {
        "domaine": "MITM",
        "label": "Médicaments d'intérêt thérapeutique majeur (MITM)",
        "url": BDPM_LOT2C_FILES["CIS_MITM.txt"],
        "table": "specialites_mitm",
        "is_optional": True,
    },
}


def get_official_fiche_url(code_cis: str) -> str:
    """Construit le lien officiel vérifié vers la fiche BDPM d'une spécialité (par code CIS)."""
    return f"{BDPM_BASE_URL}/medicament/{code_cis}/extrait"
