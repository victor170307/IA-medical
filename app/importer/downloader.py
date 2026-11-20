"""Téléchargement des fichiers publics de la BDPM et relevé de la date officielle."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
import urllib.request

from app.config import BDPM_BASE_URL, BDPM_FILES, RAW_BDPM_DIR


def fetch_official_bdpm_update_date(timeout: int = 20) -> str | None:
    """Récupère la date de dernière mise à jour affichée sur le portail officiel BDPM.

    Renvoie la date sous forme 'JJ/MM/AAAA' si elle est trouvée, ou None sinon.
    """
    url = f"{BDPM_BASE_URL}/telechargement"
    req = urllib.request.Request(url, headers={"User-Agent": "BDPM-Educ-Importer/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            html = response.read().decode("utf-8", errors="replace")
        match = re.search(
            r"Derni(?:è|&egrave;)re mise (?:à|&agrave;) jour le\s*(\d{2}/\d{2}/\d{4})",
            html,
            flags=re.IGNORECASE,
        )
        if match:
            return match.group(1)
    except Exception:
        return None
    return None


def download_bdpm_files(target_dir: Path = RAW_BDPM_DIR, timeout: int = 60) -> dict[str, dict]:
    """Télécharge les fichiers officiels de la BDPM dans `target_dir`.

    Renvoie un dictionnaire décrivant chaque fichier téléchargé (chemin, taille en octets,
    date de téléchargement UTC).
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict] = {}
    downloaded_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    official_date = fetch_official_bdpm_update_date(timeout=timeout)

    for filename, url in BDPM_FILES.items():
        req = urllib.request.Request(url, headers={"User-Agent": "BDPM-Educ-Importer/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            content = response.read()
        dest_path = target_dir / filename
        dest_path.write_bytes(content)
        results[filename] = {
            "path": str(dest_path),
            "size_bytes": len(content),
            "url": url,
            "downloaded_at_utc": downloaded_at,
            "official_bdpm_date": official_date,
        }

    # Enregistre un petit fichier de métadonnées de téléchargement
    meta_path = target_dir / "metadata_telechargement.txt"
    lines = [
        f"downloaded_at_utc={downloaded_at}",
        f"official_bdpm_date={official_date or 'non_detectee'}",
        f"source_url={BDPM_BASE_URL}/telechargement",
    ]
    for fname, info in results.items():
        lines.append(f"file.{fname}.size_bytes={info['size_bytes']}")
    meta_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return results


if __name__ == "__main__":
    info = download_bdpm_files()
    for name, details in info.items():
        print(f"{name}: {details['size_bytes']} octets (date officielle BDPM: {details['official_bdpm_date']})")

