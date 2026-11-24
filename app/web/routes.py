"""Routes web HTML pour l'accueil, la recherche filtrée et paginée, les fiches
médicaments, les substances actives (SA), les groupes génériques et le catalogue A–Z.
"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.config import TEMPLATES_DIR
from app.search.service import (
    get_available_forms,
    get_db_metadata,
    get_generic_group_detail,
    get_medication_detail,
    get_substance_detail,
    list_active_substances,
    list_catalogue_medications,
    list_initial_medications,
    search_medications,
)

router = APIRouter(tags=["Interface Web"])
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _resolve_db_path(request: Request):
    return getattr(request.app.state, "db_path", None)


def _parse_page_param(raw_page: str | None, default: int = 1) -> int:
    """Analyse prudemment le numéro de page sur une page HTML.

    Une valeur non numérique ou <= 0 est ramenée à 1 pour éviter une erreur brute 422 ;
    une valeur positive au-delà du nombre total de pages (ex. 99) est transmise telle quelle
    au service afin d'afficher un avertissement explicite de page inexistante.
    """
    if not raw_page:
        return default
    try:
        parsed = int(raw_page.strip())
    except ValueError:
        return default
    return parsed if parsed >= 1 else default


def _parse_per_page_param(raw_per_page: str | None, default: int = 20) -> int:
    if not raw_per_page:
        return default
    try:
        parsed = int(raw_per_page.strip())
    except ValueError:
        return default
    return max(1, min(parsed, 100))


def _build_query_string(params: dict[str, str | int | bool | None]) -> str:
    """Construit une chaîne de requête propre en omettant les valeurs vides ou fausses par défaut."""
    clean: dict[str, str] = {}
    for key, val in params.items():
        if val is None or val == "" or val is False:
            continue
        if isinstance(val, bool):
            clean[key] = "true"
        else:
            clean[key] = str(val)
    return urlencode(clean)


@router.get("/", response_class=HTMLResponse)
def web_index(
    request: Request,
    q: str = Query(default="", description="Terme recherché"),
    fuzzy: bool = Query(default=True, description="Tolérance aux fautes de frappe"),
    page: str = Query(default="1", description="Numéro de page"),
    per_page: str = Query(default="20", description="Nombre de résultats par page"),
    commercialise_only: bool = Query(default=False, description="Uniquement les spécialités commercialisées"),
    forme: str = Query(default="", description="Filtre forme pharmaceutique"),
    generic_role: str = Query(default="", description="Filtre groupe générique"),
    prescription_only: bool = Query(default=False, description="Soumis à prescription"),
    with_ansm_info: bool = Query(default=False, description="Informations ANSM disponibles"),
) -> HTMLResponse:
    """Affiche la page d'accueil, les filtres combinables et les résultats paginés."""
    db_path = _resolve_db_path(request)
    meta = get_db_metadata(db_path) if db_path else get_db_metadata()
    available_forms = (
        get_available_forms(db_path=db_path, limit=35)
        if db_path
        else get_available_forms(limit=35)
    )

    page_num = _parse_page_param(page, default=1)
    per_page_num = _parse_per_page_param(per_page, default=20)

    clean_forme = forme.strip()
    clean_generic_role = generic_role.strip().lower()
    if clean_generic_role not in ("in_group", "generic", "princeps"):
        clean_generic_role = ""

    has_active_filters = any(
        [
            commercialise_only,
            bool(clean_forme),
            bool(clean_generic_role),
            prescription_only,
            with_ansm_info,
        ]
    )

    search_payload = None
    initial_medications = []
    if q.strip() or has_active_filters:
        kwargs = dict(
            query=q,
            allow_fuzzy=fuzzy,
            page=page_num,
            per_page=per_page_num,
            commercialise_only=commercialise_only,
            forme=clean_forme,
            generic_role=clean_generic_role,
            prescription_only=prescription_only,
            with_ansm_info=with_ansm_info,
        )
        search_payload = (
            search_medications(db_path=db_path, **kwargs)
            if db_path
            else search_medications(**kwargs)
        )
    elif meta.get("ready") == "true":
        initial_medications = (
            list_initial_medications(db_path=db_path, limit=24)
            if db_path
            else list_initial_medications(limit=24)
        )

    filters = {
        "commercialise_only": commercialise_only,
        "forme": clean_forme,
        "generic_role": clean_generic_role,
        "prescription_only": prescription_only,
        "with_ansm_info": with_ansm_info,
    }
    active_filter_count = sum(1 for v in filters.values() if v)

    base_params: dict[str, str | int | bool | None] = {
        "q": q.strip() or None,
        "fuzzy": "false" if not fuzzy else ("true" if q.strip() else None),
        "per_page": per_page_num if per_page_num != 20 else None,
        "commercialise_only": commercialise_only,
        "forme": clean_forme or None,
        "generic_role": clean_generic_role or None,
        "prescription_only": prescription_only,
        "with_ansm_info": with_ansm_info,
    }
    current_params = dict(base_params)
    if page_num > 1:
        current_params["page"] = page_num

    current_qs = _build_query_string(current_params)
    current_return_path = f"/?{current_qs}" if current_qs else "/"
    pagination_base_qs = _build_query_string(base_params)

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "active_nav": "search",
            "query": q,
            "fuzzy": fuzzy,
            "page": page_num,
            "per_page": per_page_num,
            "filters": filters,
            "active_filter_count": active_filter_count,
            "available_forms": available_forms,
            "search": search_payload,
            "initial_medications": initial_medications,
            "current_return_path": current_return_path,
            "pagination_base_qs": pagination_base_qs,
            "metadata": meta,
        },
    )


@router.get("/medicaments/{code_cis}", response_class=HTMLResponse)
def web_medication_detail(request: Request, code_cis: str) -> HTMLResponse:
    """Affiche la fiche détaillée d'un médicament à partir de son code CIS."""
    db_path = _resolve_db_path(request)
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
            "active_nav": "detail",
            "med": detail,
            "metadata": meta,
        },
    )


@router.get("/catalogue", response_class=HTMLResponse, response_model=None)
def web_catalogue(
    request: Request,
    lettre: str = Query(default="A", description="Initiale alphabétique A–Z ou 0-9"),
    q: str = Query(default="", description="Recherche interne dans la lettre sélectionnée"),
    page: str = Query(default="1"),
    per_page: str = Query(default="20"),
    commercialise_only: bool = Query(default=False),
) -> Response:
    """Affiche le catalogue alphabétique A–Z interactif et paginé (20 par page par défaut)."""
    db_path = _resolve_db_path(request)
    page_num = _parse_page_param(page, default=1)
    per_page_num = _parse_per_page_param(per_page, default=20)
    clean_query = q.strip()

    catalogue = (
        list_catalogue_medications(
            db_path=db_path,
            letter=lettre,
            query=clean_query,
            page=page_num,
            per_page=per_page_num,
            commercialise_only=commercialise_only,
        )
        if db_path
        else list_catalogue_medications(
            letter=lettre,
            query=clean_query,
            page=page_num,
            per_page=per_page_num,
            commercialise_only=commercialise_only,
        )
    )

    base_params: dict[str, str | int | bool | None] = {
        "lettre": catalogue["letter"],
        "q": clean_query or None,
        "commercialise_only": commercialise_only,
        "per_page": per_page_num if per_page_num != 20 else None,
    }

    # Une page demandée au-delà de la dernière page disponible est ramenée vers cette dernière page
    # tout en conservant la lettre, la recherche interne et les filtres actifs.
    effective_page = catalogue["pagination"]["page"]
    if catalogue["pagination"].get("page_clamped"):
        clamped_params = dict(base_params)
        if effective_page > 1:
            clamped_params["page"] = effective_page
        clamped_url = f"/catalogue?{_build_query_string(clamped_params)}"
        return RedirectResponse(url=clamped_url, status_code=302)

    current_params = dict(base_params)
    if effective_page > 1:
        current_params["page"] = effective_page
    current_return_path = f"/catalogue?{_build_query_string(current_params)}"
    pagination_base_qs = _build_query_string(base_params)

    return templates.TemplateResponse(
        request=request,
        name="catalogue.html",
        context={
            "active_nav": "catalogue",
            "catalogue": catalogue,
            "query": clean_query,
            "current_return_path": current_return_path,
            "pagination_base_qs": pagination_base_qs,
            "metadata": catalogue["metadata"],
        },
    )


@router.get("/substances", response_class=HTMLResponse)
def web_substances(
    request: Request,
    q: str = Query(default="", description="Recherche par nom ou code officiel de substance active (SA)"),
    page: str = Query(default="1"),
    per_page: str = Query(default="30"),
) -> HTMLResponse:
    """Affiche l'annuaire paginé des substances actives officielles (nature SA)."""
    db_path = _resolve_db_path(request)
    page_num = _parse_page_param(page, default=1)
    per_page_num = _parse_per_page_param(per_page, default=30)

    payload = (
        list_active_substances(
            db_path=db_path,
            query=q,
            page=page_num,
            per_page=per_page_num,
        )
        if db_path
        else list_active_substances(
            query=q,
            page=page_num,
            per_page=per_page_num,
        )
    )

    base_params: dict[str, str | int | bool | None] = {
        "q": q.strip() or None,
        "per_page": per_page_num if per_page_num != 30 else None,
    }
    current_params = dict(base_params)
    if page_num > 1:
        current_params["page"] = page_num
    qs = _build_query_string(current_params)
    current_return_path = f"/substances?{qs}" if qs else "/substances"
    pagination_base_qs = _build_query_string(base_params)

    return templates.TemplateResponse(
        request=request,
        name="substances.html",
        context={
            "active_nav": "substances",
            "substances": payload,
            "query": q,
            "current_return_path": current_return_path,
            "pagination_base_qs": pagination_base_qs,
            "metadata": payload["metadata"],
        },
    )


@router.get("/substances/{code_substance}", response_class=HTMLResponse)
def web_substance_detail(
    request: Request,
    code_substance: str,
    page: str = Query(default="1"),
    per_page: str = Query(default="20"),
    commercialise_only: bool = Query(default=False),
) -> HTMLResponse:
    """Affiche toutes les spécialités contenant une substance active (SA) par son code officiel."""
    db_path = _resolve_db_path(request)
    page_num = _parse_page_param(page, default=1)
    per_page_num = _parse_per_page_param(per_page, default=20)

    detail = (
        get_substance_detail(
            code_substance,
            db_path=db_path,
            page=page_num,
            per_page=per_page_num,
            commercialise_only=commercialise_only,
        )
        if db_path
        else get_substance_detail(
            code_substance,
            page=page_num,
            per_page=per_page_num,
            commercialise_only=commercialise_only,
        )
    )
    if not detail:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Aucune substance active officielle (nature SA) trouvée pour le code « {code_substance} ». "
                "L'application n'invente aucune donnée absente de la BDPM."
            ),
        )

    base_params: dict[str, str | int | bool | None] = {
        "commercialise_only": commercialise_only,
        "per_page": per_page_num if per_page_num != 20 else None,
    }
    current_params = dict(base_params)
    if page_num > 1:
        current_params["page"] = page_num
    qs = _build_query_string(current_params)
    current_return_path = (
        f"/substances/{detail['code_substance']}?{qs}"
        if qs
        else f"/substances/{detail['code_substance']}"
    )
    pagination_base_qs = _build_query_string(base_params)

    return templates.TemplateResponse(
        request=request,
        name="substance_detail.html",
        context={
            "active_nav": "substances",
            "substance": detail,
            "current_return_path": current_return_path,
            "pagination_base_qs": pagination_base_qs,
            "metadata": detail["metadata"],
        },
    )


@router.get("/generiques/{identifiant_groupe}", response_class=HTMLResponse)
def web_generic_detail(request: Request, identifiant_groupe: str) -> HTMLResponse:
    """Affiche le détail d'un groupe générique officiel (princeps et génériques distingués)."""
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
                "L'application n'invente aucune donnée absente de la BDPM."
            ),
        )

    current_return_path = f"/generiques/{detail['identifiant_groupe']}"
    return templates.TemplateResponse(
        request=request,
        name="generic_detail.html",
        context={
            "active_nav": "generics",
            "group": detail,
            "current_return_path": current_return_path,
            "metadata": detail["metadata"],
        },
    )
