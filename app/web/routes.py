"""Routes web HTML pour l'accueil, la recherche et la consultation d'une fiche médicament."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import TEMPLATES_DIR
from app.search.service import (
    get_db_metadata,
    get_medication_detail,
    list_initial_medications,
    search_medications,
)

router = APIRouter(tags=["Interface Web"])
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("/", response_class=HTMLResponse)
def web_index(
    request: Request,
    q: str = Query(default="", description="Terme recherché"),
    fuzzy: bool = Query(default=True, description="Tolérance aux fautes de frappe"),
) -> HTMLResponse:
    """Affiche la page d'accueil, l'aperçu initial et les résultats de recherche."""
    db_path = getattr(request.app.state, "db_path", None)
    meta = get_db_metadata(db_path) if db_path else get_db_metadata()

    search_payload = None
    initial_medications = []
    if q.strip():
        search_payload = (
            search_medications(query=q, db_path=db_path, allow_fuzzy=fuzzy)
            if db_path
            else search_medications(query=q, allow_fuzzy=fuzzy)
        )
    elif meta.get("ready") == "true":
        initial_medications = (
            list_initial_medications(db_path=db_path, limit=24)
            if db_path
            else list_initial_medications(limit=24)
        )

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "query": q,
            "fuzzy": fuzzy,
            "search": search_payload,
            "initial_medications": initial_medications,
            "metadata": meta,
        },
    )


@router.get("/medicaments/{code_cis}", response_class=HTMLResponse)
def web_medication_detail(request: Request, code_cis: str) -> HTMLResponse:
    """Affiche la fiche détaillée d'un médicament à partir de son code CIS."""
    db_path = getattr(request.app.state, "db_path", None)
    meta = get_db_metadata(db_path) if db_path else get_db_metadata()
    detail = (
        get_medication_detail(code_cis, db_path=db_path)
        if db_path
        else get_medication_detail(code_cis)
    )
    if not detail:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Aucune spécialité trouvée pour le code CIS « {code_cis} ». "
                "L'application n'invente aucune fiche absente de la BDPM."
            ),
        )

    return templates.TemplateResponse(
        request=request,
        name="detail.html",
        context={
            "med": detail,
            "metadata": meta,
        },
    )
