"""Tests unitaires du moteur de recherche (exacte, normalisée, multi-critères, fautes de frappe)."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from app.importer.bdpm_importer import import_bdpm_directory
from app.search.service import search_medications
from tests.synthetic_fixtures import create_synthetic_bdpm_files


class TestMedicationSearch(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.raw_dir = self.base_path / "raw"
        self.db_path = self.base_path / "test_bdpm.sqlite3"
        create_synthetic_bdpm_files(self.raw_dir)
        import_bdpm_directory(raw_dir=self.raw_dir, db_path=self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_search_by_medication_name(self) -> None:
        res = search_medications("SYNTHETICDOL", db_path=self.db_path)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["count"], 2)

    def test_search_by_substance_ignoring_accents_and_case(self) -> None:
        res = search_medications("  paracetamol   synthetique ", db_path=self.db_path)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["count"], 2)

    def test_search_combining_name_dosage_and_form(self) -> None:
        res = search_medications("syntheticdol 500 gelule", db_path=self.db_path)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["results"][0]["code_cis"], "90000002")

    def test_search_by_code_cis_and_cip13(self) -> None:
        by_cis = search_medications("90000003", db_path=self.db_path)
        self.assertEqual(by_cis["count"], 1)
        self.assertEqual(by_cis["results"][0]["denomination"], "SYNTHETIMOX 1 g, poudre pour suspension buvable")

        by_cip13 = search_medications("3400930000011", db_path=self.db_path)
        self.assertEqual(by_cip13["count"], 1)
        self.assertEqual(by_cip13["results"][0]["code_cis"], "90000001")

    def test_empty_query_returns_no_results(self) -> None:
        res = search_medications("   ", db_path=self.db_path)
        self.assertEqual(res["status"], "empty_query")
        self.assertEqual(res["count"], 0)
        self.assertEqual(res["results"], [])

    def test_unknown_medication_not_invented(self) -> None:
        res = search_medications("MEDICAMENTTOTALEMENTINEXISTANTXYZ", db_path=self.db_path)
        self.assertEqual(res["status"], "no_results")
        self.assertEqual(res["count"], 0)
        self.assertIsNotNone(res["warning"])

    def test_typo_tolerant_search_with_explicit_warning(self) -> None:
        # 'synthetikdol' comporte une faute de frappe ('k' au lieu de 'c')
        res = search_medications("synthetikdol 1000", db_path=self.db_path, allow_fuzzy=True)
        self.assertEqual(res["status"], "fuzzy_match")
        self.assertEqual(res["match_mode"], "fuzzy_orthographic")
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["results"][0]["code_cis"], "90000001")
        self.assertIn("ressemblance de nom ne prouve jamais", res["warning"])

    def test_fuzzy_search_never_alters_dosage_numbers(self) -> None:
        # Un dosage inexistant (2500 mg) ne doit jamais être remplacé par 1000 mg ou 500 mg
        res = search_medications("syntheticdol 2500", db_path=self.db_path, allow_fuzzy=True)
        self.assertEqual(res["status"], "no_results")
        self.assertEqual(res["count"], 0)

    def test_search_by_generic_group_princeps_name(self) -> None:
        # 'PRINCEPSFICTIF' figure uniquement dans CIS_GENER_bdpm.txt (comme ROACCUTANE 10 mg)
        res = search_medications("princepsfictif 1g", db_path=self.db_path)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["results"][0]["code_cis"], "90000003")
        self.assertEqual(len(res["results"][0]["groupes_generiques"]), 1)

    def test_search_with_attached_dosage_unit_and_plural_form(self) -> None:
        # '1000mg' sans espace et 'comprimes' au pluriel
        res = search_medications("syntheticdol 1000mg comprimes", db_path=self.db_path)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["results"][0]["code_cis"], "90000001")


if __name__ == "__main__":
    unittest.main()
