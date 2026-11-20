# Référentiel Médicaments — Données Publiques BDPM (Palier 1)

Application web et API de recherche documentaire de médicaments commercialisés ou référencés en France, alimentée par les fichiers publics officiels de la **Base de données publique des médicaments (BDPM)**.

## 1. Architecture du projet

```text
bdpm_app/
├── app/
│   ├── config.py                # Chemins et URLs officielles de la BDPM
│   ├── database.py              # Schéma relationnel SQLite et connexions
│   ├── main.py                  # Point d'entrée FastAPI
│   ├── importer/
│   │   ├── downloader.py        # Téléchargement des fichiers officiels BDPM
│   │   └── bdpm_importer.py     # Importation reproductible et contrôlée vers SQLite
│   ├── search/
│   │   ├── normalizer.py        # Normalisation (accents, casse, ponctuation)
│   │   └── service.py           # Moteur de recherche et fiches détaillées
│   ├── api/
│   │   └── routes.py            # Routes JSON (/api/health, /api/search, /api/medications/{cis})
│   └── web/
│       ├── routes.py            # Pages HTML rendues côté serveur
│       ├── templates/           # Templates Jinja2 (base.html, index.html, detail.html)
│       └── static/style.css     # Feuille de style responsive
├── scripts/
│   ├── manage_bdpm.py           # Outil en ligne de commande (téléchargement, import, statut)
│   └── run_tests.py             # Exécution de toute la suite de tests automatisés
└── tests/
    ├── synthetic_fixtures.py    # Données synthétiques isolées des données réelles
    ├── test_importer.py         # Tests unitaires de l'importateur
    ├── test_search.py           # Tests unitaires du moteur de recherche
    └── test_api_and_web.py      # Tests d'intégration API et pages HTML
```

## 2. Commandes d'utilisation (depuis PowerShell)

Ouvrez un terminal PowerShell et placez-vous dans le dossier du projet :

```powershell
cd C:\Users\snxwe\.gemini\antigravity\scratch\bdpm_app
```

> **Note sur Python sur votre machine :** Votre installation Python 3.12.10 se trouve dans :
> `C:\Users\snxwe\AppData\Local\Python\pythoncore-3.12-64\python.exe`

### A. Lancer les tests automatisés (sur données synthétiques isolées)

```powershell
C:\Users\snxwe\AppData\Local\Python\pythoncore-3.12-64\python.exe scripts\run_tests.py
```

### B. Télécharger et importer les vraies données officielles de la BDPM

```powershell
C:\Users\snxwe\AppData\Local\Python\pythoncore-3.12-64\python.exe scripts\manage_bdpm.py download-and-import
```

### C. Vérifier l'état de la base SQLite

```powershell
C:\Users\snxwe\AppData\Local\Python\pythoncore-3.12-64\python.exe scripts\manage_bdpm.py status
```

### D. Lancer le serveur web FastAPI

```powershell
C:\Users\snxwe\AppData\Local\Python\pythoncore-3.12-64\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Puis ouvrez votre navigateur à l'adresse :
- Interface web : `http://127.0.0.1:8000`
- Documentation interactive de l'API : `http://127.0.0.1:8000/docs`
