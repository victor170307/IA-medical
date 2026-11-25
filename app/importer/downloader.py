"""Téléchargement des fichiers publics de la BDPM et suivi de fraîcheur par fichier.

Principes respectés :
- Enregistre pour chaque fichier : date de téléchargement local (UTC), taille en octets,
  empreinte SHA-256, en-tête HTTP `Last-Modified` (lorsqu'il est fourni) et date officielle
  affichée sur le portail BDPM lorsqu'elle est vérifiable.
- Ne présente jamais l'en-tête HTTP `Last-Modified` comme une date officielle de publication.
- Conserve intégralement le fonctionnement existant (`download_bdpm_files`) et le fichier
  `metadata_telechargement.txt`.
- Protège les fichiers déjà présents : écriture atomique par fichier temporaire et aucune
  suppression en cas d'échec d'une source facultative.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import html as html_lib
import json
from pathlib import Path
import re
import urllib.request

from app.config import BASE_DIR, BDPM_BASE_URL, BDPM_FILES, BDPM_LOT2C_FILES, RAW_BDPM_DIR

# Sources complémentaires officielles du Lot 2C (vérifiées sur /telechargement)
BDPM_COMPLEMENTARY_FILES: dict[str, str] = dict(BDPM_LOT2C_FILES)

USER_AGENT = "BDPM-Educ-Importer/1.0"


def fetch_official_bdpm_portal_dates(timeout: int = 20) -> dict:
    """Inspecte la page officielle `/telechargement` de la BDPM pour relever :
    - `global_date` : date globale affichée dans l'en-tête (`JJ/MM/AAAA` ou None) ;
    - `file_dates` : dictionnaire par nom de fichier avec la date officielle vérifiable
      affichée à côté du lien de téléchargement (ou `None` pour les fichiers générés en direct).
    """
    url = f"{BDPM_BASE_URL}/telechargement"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    result: dict = {
        "portal_url": url,
        "global_date": None,
        "file_dates": {},
    }
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            page = response.read().decode("utf-8", errors="replace")
    except Exception:
        return result

    global_match = re.search(
        r"Derni(?:è|&egrave;)re mise (?:à|&agrave;) jour le\s*(\d{2}/\d{2}/\d{4})",
        page,
        flags=re.IGNORECASE,
    )
    if global_match:
        result["global_date"] = global_match.group(1)

    for li_match in re.finditer(r"<li[^>]*>(.*?)</li>", page, flags=re.DOTALL | re.IGNORECASE):
        block = li_match.group(1)
        href_match = re.search(r'href="(/download/[^"]+\.txt)"', block, flags=re.IGNORECASE)
        if not href_match:
            continue
        href = href_match.group(1)
        filename = href.rsplit("/", 1)[-1]
        clean_text = re.sub(r"<[^>]+>", " ", html_lib.unescape(block))
        clean_text = re.sub(r"\s+", " ", clean_text).strip()

        date_match = re.search(
            r"Date de mise à jour\s*:\s*(\d{2}/\d{2}/\d{4})",
            clean_text,
            flags=re.IGNORECASE,
        )
        if date_match:
            result["file_dates"][filename] = {
                "official_source_date": date_match.group(1),
                "official_date_verified_from": f"Page officielle BDPM ({url})",
                "portal_href": f"{BDPM_BASE_URL}{href}",
            }
        elif "génération en direct" in clean_text.lower():
            result["file_dates"][filename] = {
                "official_source_date": None,
                "official_date_verified_from": "Génération en direct sur le portail BDPM (pas de date fixe affichée)",
                "portal_href": f"{BDPM_BASE_URL}{href}",
            }
        else:
            result["file_dates"][filename] = {
                "official_source_date": None,
                "official_date_verified_from": "Date officielle non mentionnée sur le portail BDPM",
                "portal_href": f"{BDPM_BASE_URL}{href}",
            }

    return result


def fetch_official_bdpm_update_date(timeout: int = 20) -> str | None:
    """Récupère la date de dernière mise à jour affichée sur le portail officiel BDPM.

    Renvoie la date sous forme 'JJ/MM/AAAA' si elle est trouvée, ou None sinon.
    """
    portal_info = fetch_official_bdpm_portal_dates(timeout=timeout)
    return portal_info.get("global_date")


def _compute_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _format_metadata_path(dest_path: Path) -> str:
    """Formate le chemin enregistré dans `metadata_sources.json` en chemin relatif
    portable (ex. `data/raw/CIS_MITM.txt`) afin de ne jamais exposer un chemin local absolu.
    """
    try:
        return dest_path.resolve().relative_to(BASE_DIR.resolve()).as_posix()
    except ValueError:
        return dest_path.name


def _load_sources_metadata(meta_json_path: Path) -> dict:
    if not meta_json_path.exists():
        return {"portal_url": f"{BDPM_BASE_URL}/telechargement", "files": {}}
    try:
        data = json.loads(meta_json_path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("files"), dict):
            return data
    except Exception:
        pass
    return {"portal_url": f"{BDPM_BASE_URL}/telechargement", "files": {}}


def _save_sources_metadata(
    target_dir: Path,
    file_entries: dict[str, dict],
    portal_global_date: str | None,
) -> Path:
    """Enregistre/fusionne le suivi détaillé par fichier dans `metadata_sources.json`
    sans effacer les métadonnées des autres fichiers déjà suivis.
    """
    meta_json_path = target_dir / "metadata_sources.json"
    existing = _load_sources_metadata(meta_json_path)
    existing["portal_url"] = f"{BDPM_BASE_URL}/telechargement"
    if portal_global_date:
        existing["portal_global_date"] = portal_global_date
    existing["updated_at_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    existing.setdefault("files", {}).update(file_entries)

    tmp_path = meta_json_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp_path.replace(meta_json_path)
    return meta_json_path


def _download_single_file(
    filename: str,
    url: str,
    target_dir: Path,
    portal_dates: dict,
    timeout: int = 60,
    is_optional: bool = False,
) -> dict:
    """Télécharge un fichier officiel de manière atomique et collecte ses métadonnées.

    L'en-tête HTTP `Last-Modified` est stocké séparément de `official_source_date` afin
    de ne jamais confondre une date serveur HTTP avec une date officielle de publication.
    En cas d'erreur sur une source facultative, aucun fichier existant n'est supprimé.
    """
    dest_path = target_dir / filename
    portal_entry = portal_dates.get("file_dates", {}).get(filename, {})
    official_source_date = portal_entry.get("official_source_date")
    official_date_verified_from = portal_entry.get(
        "official_date_verified_from",
        "Date officielle non vérifiable automatiquement",
    )

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            http_last_modified = response.headers.get("Last-Modified")
            content = response.read()
    except Exception as exc:
        if not is_optional:
            raise
        return {
            "filename": filename,
            "path": _format_metadata_path(dest_path),
            "url": url,
            "optional": True,
            "status": "error",
            "error": str(exc),
            "existing_file_preserved": dest_path.exists(),
            "downloaded_at_utc": None,
            "size_bytes": dest_path.stat().st_size if dest_path.exists() else None,
            "sha256": None,
            "http_last_modified": None,
            "official_source_date": official_source_date,
            "official_date_verified_from": official_date_verified_from,
        }

    downloaded_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sha256_hex = _compute_sha256(content)

    # Écriture atomique : un échec d'écriture ne détruit pas une version antérieure valide
    tmp_path = target_dir / f".{filename}.tmp"
    tmp_path.write_bytes(content)
    tmp_path.replace(dest_path)

    return {
        "filename": filename,
        "path": _format_metadata_path(dest_path),
        "url": url,
        "optional": is_optional,
        "status": "ok",
        "downloaded_at_utc": downloaded_at,
        "size_bytes": len(content),
        "sha256": sha256_hex,
        "http_last_modified": http_last_modified,
        "official_source_date": official_source_date,
        "official_date_verified_from": official_date_verified_from,
        "official_bdpm_date": official_source_date or portal_dates.get("global_date"),
    }


def download_bdpm_files(
    target_dir: Path = RAW_BDPM_DIR,
    timeout: int = 60,
    include_complementary: bool = False,
) -> dict[str, dict]:
    """Télécharge les fichiers officiels de la BDPM dans `target_dir`.

    Conserve le fonctionnement existant (6 fichiers du Palier 1 + `metadata_telechargement.txt`)
    et enregistre également le suivi détaillé par fichier dans `metadata_sources.json`.
    Si `include_complementary=True`, télécharge en plus les 5 fichiers du Lot 2C en mode
    facultatif (un échec sur une source complémentaire ne supprime aucune donnée existante).
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict] = {}
    batch_downloaded_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    portal_dates = fetch_official_bdpm_portal_dates(timeout=min(timeout, 30))
    official_date = portal_dates.get("global_date")

    for filename, url in BDPM_FILES.items():
        results[filename] = _download_single_file(
            filename=filename,
            url=url,
            target_dir=target_dir,
            portal_dates=portal_dates,
            timeout=timeout,
            is_optional=False,
        )

    if include_complementary:
        for filename, url in BDPM_COMPLEMENTARY_FILES.items():
            results[filename] = _download_single_file(
                filename=filename,
                url=url,
                target_dir=target_dir,
                portal_dates=portal_dates,
                timeout=timeout,
                is_optional=True,
            )

    # Conserve intact le format historique de `metadata_telechargement.txt`
    meta_path = target_dir / "metadata_telechargement.txt"
    lines = [
        f"downloaded_at_utc={batch_downloaded_at}",
        f"official_bdpm_date={official_date or 'non_detectee'}",
        f"source_url={BDPM_BASE_URL}/telechargement",
    ]
    for fname, info in results.items():
        if info.get("status") == "ok":
            lines.append(f"file.{fname}.size_bytes={info['size_bytes']}")
            lines.append(f"file.{fname}.sha256={info['sha256']}")
            if info.get("http_last_modified"):
                lines.append(f"file.{fname}.http_last_modified={info['http_last_modified']}")
            if info.get("official_source_date"):
                lines.append(f"file.{fname}.official_source_date={info['official_source_date']}")
    meta_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Enregistre également le suivi structuré par fichier (fusionné sans écraser les autres sources)
    _save_sources_metadata(target_dir, results, official_date)

    return results


def download_complementary_bdpm_files(
    target_dir: Path = RAW_BDPM_DIR,
    timeout: int = 60,
    files_subset: dict[str, str] | None = None,
) -> dict[str, dict]:
    """Télécharge uniquement les sources complémentaires officielles (Lot 2C) dans `target_dir`
    sans modifier ni écraser les 6 fichiers du Palier 1 ni `metadata_telechargement.txt`.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    selected_files = files_subset if files_subset is not None else BDPM_COMPLEMENTARY_FILES
    portal_dates = fetch_official_bdpm_portal_dates(timeout=min(timeout, 30))

    results: dict[str, dict] = {}
    for filename, url in selected_files.items():
        results[filename] = _download_single_file(
            filename=filename,
            url=url,
            target_dir=target_dir,
            portal_dates=portal_dates,
            timeout=timeout,
            is_optional=True,
        )

    _save_sources_metadata(target_dir, results, portal_dates.get("global_date"))
    return results


if __name__ == "__main__":
    info = download_bdpm_files()
    for name, details in info.items():
        print(
            f"{name}: {details['size_bytes']} octets "
            f"(sha256: {details['sha256'][:12]}..., "
            f"date officielle: {details['official_source_date'] or 'non précisée'})"
        )

