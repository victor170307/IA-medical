"""Points d'entrée JSON de l'API pour la recherche et les fiches de médicaments."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from app.search.service import get_db_metadata, get_medication_detail, search_medications

router = APIRouter(prefix="/api", tags=["API Médicaments BDPM"])


@router.get("/health")
def api_health(request: Request) -> dict:
    """Vérifie le fonctionnement du serveur et l'état de la base SQLite BDPM."""
    db_path = getattr(request.app.state, "db_path", None)
    meta = get_db_metadata(db_path) if db_path else get_db_metadata()
    return {
        "status": "ok",
        "database_ready": meta["ready"] == "true",
        "specialites_count": int(meta["specialites_count"]),
        "provenance": meta,
    }


@router.get("/search")
def api_search(
    request: Request,
    q: str = Query(default="", description="Terme recherché (nom, substance, dosage, forme, CIS, CIP13)"),
    limit: int = Query(default=30, ge=1, le=100),
    fuzzy: bool = Query(default=True, description="Autoriser la recherche tolérante aux fautes de frappe"),
) -> dict:
    """Recherche des médicaments dans la base BDPM locale."""
    db_path = getattr(request.app.state, "db_path", None)
    if db_path:
        return search_medications(query=q, db_path=db_path, limit=limit, allow_fuzzy=fuzzy)
    return search_medications(query=q, limit=limit, allow_fuzzy=fuzzy)


@router.get("/medications/{code_cis}")
def api_medication_detail(request: Request, code_cis: str) -> dict:
    """Retourne la fiche complète d'une spécialité pharmaceutique par son code CIS."""
    db_path = getattr(request.app.state, "db_path", None)
    detail = (
        get_medication_detail(code_cis, db_path=db_path)
        if db_path
        else get_medication_detail(code_cis)
    )
    if not detail:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Aucune spécialité trouvée dans la base BDPM locale pour le code CIS « {code_cis} ». "
                "Aucune donnée n'est inventée."
            ),
        )
    return detail
