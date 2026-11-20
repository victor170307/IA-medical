"""Tests unitaires de l'importateur BDPM (validation, doublons, orphelins, idempotence)."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from app.importer.bdpm_importer import import_bdpm_directory
from app.search.service import get_db_metadata, get_medication_detail
from tests.synthetic_fixtures import create_synthetic_bdpm_files


class TestBdpmImporter(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.raw_dir = self.base_path / "raw"
        self.db_path = self.base_path / "test_bdpm.sqlite3"
        create_synthetic_bdpm_files(self.raw_dir)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_import_counts_and_malformed_or_orphan_handling(self) -> None:
        report = import_bdpm_directory(raw_dir=self.raw_dir, db_path=self.db_path)

        spec_stats = report.files["CIS_bdpm.txt"]
        self.assertEqual(spec_stats.rows_imported, 3)
        self.assertEqual(spec_stats.rows_skipped_duplicate, 1)
        self.assertEqual(spec_stats.rows_malformed, 2)

        compo_stats = report.files["CIS_COMPO_bdpm.txt"]
        self.assertEqual(compo_stats.rows_imported, 3)
        self.assertEqual(compo_stats.rows_skipped_orphan, 1)

        meta = get_db_metadata(self.db_path)
        self.assertEqual(meta["ready"], "true")
        self.assertEqual(meta["specialites_count"], "3")
        self.assertEqual(meta["official_bdpm_date"], "29/09/2026")

    def test_idempotence_on_reimport(self) -> None:
        import_bdpm_directory(raw_dir=self.raw_dir, db_path=self.db_path)
        second_report = import_bdpm_directory(raw_dir=self.raw_dir, db_path=self.db_path)

        self.assertEqual(second_report.files["CIS_bdpm.txt"].rows_imported, 3)
        meta = get_db_metadata(self.db_path)
        self.assertEqual(meta["specialites_count"], "3")

    def test_missing_dosage_not_invented_and_html_sanitized(self) -> None:
        import_bdpm_directory(raw_dir=self.raw_dir, db_path=self.db_path)
        detail = get_medication_detail("90000003", db_path=self.db_path)
        self.assertIsNotNone(detail)
        assert detail is not None

        # Le dosage vide dans CIS_COMPO_bdpm.txt doit rester None (jamais inventé)
        self.assertEqual(len(detail["compositions"]), 1)
        self.assertIsNone(detail["compositions"][0]["dosage"])

        # L'alerte ANSM issue de CIS_InfoImportantes.txt doit avoir son HTML nettoyé et son lien extrait
        self.assertEqual(len(detail["infos_importantes"]), 1)
        info = detail["infos_importantes"][0]
        self.assertEqual(
            info["texte_information"],
            "Alerte fictive de sécurité pour test unitaire",
        )
        self.assertEqual(
            info["lien_ansm"],
            "https://ansm.sante.fr/actualites/alerte-fictive-test",
        )


if __name__ == "__main__":
    unittest.main()
