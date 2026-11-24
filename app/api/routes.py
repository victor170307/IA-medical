"""Points d'entrée JSON de l'API pour la recherche, les fiches, les substances,
les groupes génériques et le catalogue alphabétique BDPM.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from app.search.service import (
    get_db_metadata,
    get_generic_group_detail,
    get_medication_detail,
    get_substance_detail,
    list_active_substances,
    list_catalogue_medications,
    search_medications,
)

router = APIRouter(prefix="/api", tags=["API Médicaments BDPM"])


def _resolve_db_path(request: Request):
    return getattr(request.app.state, "db_path", None)


@router.get("/health")
def api_health(request: Request) -> dict:
    """Vérifie le fonctionnement du serveur et l'état de la base SQLite BDPM."""
    db_path = _resolve_db_path(request)
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
    page: int = Query(default=1, ge=1, description="Numéro de page (commence à 1)"),
    per_page: int | None = Query(default=None, ge=1, le=100, description="Résultats par page"),
    commercialise_only: bool = Query(default=False, description="Uniquement les spécialités commercialisées"),
    forme: str = Query(default="", description="Filtre sur la forme pharmaceutique"),
    generic_role: str = Query(
        default="",
        description="Filtre groupe générique : '', 'in_group', 'generic' ou 'princeps'",
    ),
    prescription_only: bool = Query(
        default=False,
        description="Uniquement les spécialités soumises à prescription (d'après CIS_CPD)",
    ),
    with_ansm_info: bool = Query(
        default=False,
        description="Uniquement les spécialités avec informations ANSM disponibles",
    ),
) -> dict:
    """Recherche paginée et filtrable de médicaments dans la base BDPM locale."""
    db_path = _resolve_db_path(request)
    kwargs = dict(
        query=q,
        limit=limit,
        allow_fuzzy=fuzzy,
        page=page,
        per_page=per_page,
        commercialise_only=commercialise_only,
        forme=forme,
        generic_role=generic_role,
        prescription_only=prescription_only,
        with_ansm_info=with_ansm_info,
    )
    if db_path:
        return search_medications(db_path=db_path, **kwargs)
    return search_medications(**kwargs)


@router.get("/medications/{code_cis}")
def api_medication_detail(request: Request, code_cis: str) -> dict:
    """Retourne la fiche complète d'une spécialité pharmaceutique par son code CIS."""
    db_path = _resolve_db_path(request)
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


@router.get("/substances")
def api_substances(
    request: Request,
    q: str = Query(default="", description="Filtre par nom ou code officiel de substance active (SA)"),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=30, ge=1, le=100),
) -> dict:
    """Liste paginée des substances actives officielles (nature SA uniquement)."""
    db_path = _resolve_db_path(request)
    if db_path:
        return list_active_substances(db_path=db_path, query=q, page=page, per_page=per_page)
    return list_active_substances(query=q, page=page, per_page=per_page)


@router.get("/substances/{code_substance}")
def api_substance_detail(
    request: Request,
    code_substance: str,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=100),
    commercialise_only: bool = Query(default=False),
) -> dict:
    """Retourne le détail d'une substance active officielle (SA) et ses spécialités paginées."""
    db_path = _resolve_db_path(request)
    detail = (
        get_substance_detail(
            code_substance,
            db_path=db_path,
            page=page,
            per_page=per_page,
            commercialise_only=commercialise_only,
        )
        if db_path
        else get_substance_detail(
            code_substance,
            page=page,
            per_page=per_page,
            commercialise_only=commercialise_only,
        )
    )
    if not detail:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Aucune substance active (nature SA) trouvée pour le code officiel « {code_substance} ». "
                "Aucune donnée n'est inventée."
            ),
        )
    return detail


@router.get("/generics/{identifiant_groupe}")
def api_generic_group_detail(request: Request, identifiant_groupe: str) -> dict:
    """Retourne les princeps et les génériques d'un groupe générique officiel BDPM."""
    db_path = _resolve_db_path(request)
    detail = (
        get_generic_group_detail(identifiant_groupe, db_path=db_path)
        if db_path
        else get_generic_group_detail(identifiant_groupe)
    )
    if not detail:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Aucun groupe générique trouvé pour l'identifiant « {identifiant_groupe} ». "
                "Aucune donnée n'est inventée."
            ),
        )
    return detail


@router.get("/catalogue")
def api_catalogue(
    request: Request,
    lettre: str = Query(default="A", description="Initiale alphabétique A–Z ou '0-9'"),
    q: str = Query(default="", description="Recherche interne confinée à la lettre sélectionnée"),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=100),
    commercialise_only: bool = Query(default=False),
) -> dict:
    """Retourne une page du catalogue alphabétique A–Z des spécialités."""
    db_path = _resolve_db_path(request)
    if db_path:
        return list_catalogue_medications(
            db_path=db_path,
            letter=lettre,
            query=q,
            page=page,
            per_page=per_page,
            commercialise_only=commercialise_only,
        )
    return list_catalogue_medications(
        letter=lettre,
        query=q,
        page=page,
        per_page=per_page,
        commercialise_only=commercialise_only,
    )
