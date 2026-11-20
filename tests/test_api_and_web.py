"""Tests d'intégration des routes API JSON et de l'interface web HTML."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from app.importer.bdpm_importer import import_bdpm_directory
from app.main import create_app
from tests.synthetic_fixtures import create_synthetic_bdpm_files


class TestApiAndWebRoutes(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.raw_dir = self.base_path / "raw"
        self.db_path = self.base_path / "test_bdpm.sqlite3"
        create_synthetic_bdpm_files(self.raw_dir)
        import_bdpm_directory(raw_dir=self.raw_dir, db_path=self.db_path)

        app = create_app(db_path=self.db_path)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_api_health(self) -> None:
        resp = self.client.get("/api/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["database_ready"])
        self.assertEqual(data["specialites_count"], 3)
        self.assertEqual(data["provenance"]["official_bdpm_date"], "29/09/2026")

    def test_api_search_and_detail(self) -> None:
        search_resp = self.client.get("/api/search", params={"q": "amoxicilline synthetique"})
        self.assertEqual(search_resp.status_code, 200)
        payload = search_resp.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["results"][0]["code_cis"], "90000003")

        detail_resp = self.client.get("/api/medications/90000001")
        self.assertEqual(detail_resp.status_code, 200)
        detail = detail_resp.json()
        self.assertEqual(detail["denomination"], "SYNTHETICDOL 1000 mg, comprimé pelliculé")
        self.assertEqual(
            detail["external_official_links"]["fiche_bdpm_url"],
            "https://base-donnees-publique.medicaments.gouv.fr/medicament/90000001/extrait",
        )

    def test_api_detail_404_when_missing(self) -> None:
        resp = self.client.get("/api/medications/00000000")
        self.assertEqual(resp.status_code, 404)

    def test_web_pages_include_provenance_and_disclaimers(self) -> None:
        home_resp = self.client.get("/", params={"q": "SYNTHETICDOL"})
        self.assertEqual(home_resp.status_code, 200)
        html_text = home_resp.text
        self.assertIn("SYNTHETICDOL 1000 mg", html_text)
        self.assertIn("Base de données publique des médicaments", html_text)
        self.assertIn("29/09/2026", html_text)

        detail_resp = self.client.get("/medicaments/90000003")
        self.assertEqual(detail_resp.status_code, 200)
        detail_html = detail_resp.text
        self.assertIn("SYNTHETIMOX 1 g", detail_html)
        self.assertIn("Alerte fictive de sécurité pour test unitaire", detail_html)
        self.assertIn("Limites des données affichées", detail_html)


if __name__ == "__main__":
    unittest.main()
