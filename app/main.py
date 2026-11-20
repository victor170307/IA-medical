"""Point d'entrée de l'application FastAPI (Palier 1 — Référentiel BDPM)."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.routes import router as api_router
from app.config import DEFAULT_DB_PATH, STATIC_DIR
from app.web.routes import router as web_router


def create_app(db_path: Path | str = DEFAULT_DB_PATH) -> FastAPI:
    """Crée et configure l'application FastAPI.

    Le paramètre `db_path` permet d'injecter une base SQLite synthétique isolée
    lors de l'exécution des tests automatisés, sans jamais toucher à la base réelle.
    """
    application = FastAPI(
        title="Référentiel Médicaments — Données Publiques BDPM",
        description=(
            "Application de recherche documentaire de médicaments fondée sur les fichiers "
            "ouverts de la Base de données publique des médicaments (BDPM)."
        ),
        version="0.1.0",
    )
    application.state.db_path = Path(db_path)

    # Autorisation CORS pour permettre au portfolio et autres frontends de consommer l'API locale
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    application.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    application.include_router(api_router)
    application.include_router(web_router)
    return application


app = create_app()
