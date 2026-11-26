"""Suite de tests isolés du Lot 2C.2 :
- Schéma SQLite des 6 nouvelles tables (sans migration automatique au démarrage).
- Sauvegarde vérifiée, migration explicite et restauration.
- Importateurs des 5 fichiers officiels (Disponibilités, HAS SMR, HAS ASMR, Liens CT, MITM).
- Traitement sûr des textes médicaux HAS (`<br>` vs `< 18 ans`, `p < 0,0001`, `< -3 DS`).
- Protection contre fichiers absents, vides, malformés, échecs de téléchargement et rollback SQLite.
- Idempotence et suivi de fraîcheur (`sources_fraicheur`).

Tous les tests s'exécutent sur des bases SQLite synthétiques temporaires et ne touchent
jamais à `data/bdpm.sqlite3`.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from fastapi.testclient import TestClient
from jinja2 import Environment

from app.database import (
    LOT2C_TABLES,
    PALIER1_TABLES,
    backup_database,
    get_connection,
    inspect_schema_status,
    migrate_lot2c_schema,
    restore_database,
)
from app.importer.bdpm_importer import (
    clean_has_medical_text,
    import_bdpm_directory,
    import_lot2c_sources,
)
from app.main import create_app


def _write_palier1_synthetic_files(raw_dir: Path) -> None:
    """Crée un jeu synthétique minimal Palier 1 (3 spécialités : 93000001, 93000002, 93000003)."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    cis_lines = [
        "93000001\tMEDTEST ALPHA 500 mg, comprimé\tcomprimé\torale\tAutorisation active\tProcédure nationale\tCommercialisée\t01/01/2020\t\t\tLABO ALPHA\tNon",
        "93000002\tMEDTEST BETA 20 mg, gélule\tgélule\torale\tAutorisation active\tProcédure nationale\tCommercialisée\t15/06/2021\tWarning disponibilité\t\tLABO BETA\tNon",
        "93000003\tMEDTEST GAMMA 100 mg, comprimé\tcomprimé\torale\tAutorisation active\tProcédure nationale\tCommercialisée\t10/03/2022\t\t\tLABO GAMMA\tOui",
    ]
    (raw_dir / "CIS_bdpm.txt").write_bytes(("\r\n".join(cis_lines) + "\r\n").encode("cp1252"))

    compo_lines = [
        "93000001\tcomprimé\t30001\tSUBSTANCE ALPHA\t500 mg\tun comprimé\tSA\t1",
        "93000002\tgélule\t30002\tSUBSTANCE BETA\t20 mg\tune gélule\tSA\t1",
        "93000003\tcomprimé\t30003\tSUBSTANCE GAMMA\t100 mg\tun comprimé\tSA\t1",
    ]
    (raw_dir / "CIS_COMPO_bdpm.txt").write_bytes(("\r\n".join(compo_lines) + "\r\n").encode("cp1252"))

    cip_lines = [
        "93000001\t3300001\tboîte de 16 comprimés\tPrésentation active\tDéclaration de commercialisation\t01/01/2020\t3400933000011\toui\t65%\t2,00\t3,02\t1,02\t",
        "93000002\t3300002\tboîte de 30 gélules\tPrésentation active\tDéclaration de commercialisation\t15/06/2021\t3400933000028\toui\t65%\t5,00\t6,02\t1,02\t",
    ]
    (raw_dir / "CIS_CIP_bdpm.txt").write_bytes(("\n".join(cip_lines) + "\n").encode("utf-8"))
    (raw_dir / "CIS_CPD_bdpm.txt").write_bytes(b"93000002\tliste I\r\n")
    (raw_dir / "CIS_GENER_bdpm.txt").write_bytes(b"")
    (raw_dir / "CIS_InfoImportantes.txt").write_bytes(b"")
    (raw_dir / "metadata_telechargement.txt").write_text(
        "downloaded_at_utc=2026-11-20T10:00:00Z\nofficial_bdpm_date=20/11/2026\n",
        encoding="utf-8",
    )


def _write_lot2c_synthetic_files(raw_dir: Path, include_metadata_json: bool = True) -> None:
    """Crée les 5 fichiers synthétiques du Lot 2C reproduisant les structures réelles auditées."""
    # 1. CIS_CIP_Dispo_Spec.txt (8 colonnes, cp1252)
    dispo_lines = [
        # Rupture de stock (code 1) sur toute la spécialité (CIP13 vide)
        "93000001\t\t1\tRupture de stock\t10/10/2026\t02/11/2026\t\thttps://ansm.sante.fr/disponibilites/rupture-alpha",
        # Tension d'approvisionnement (code 2) sur toute la spécialité — 2 dates de mise à jour distinctes (historique)
        "93000002\t\t2\tTension d'approvisionnement\t01/09/2026\t02/11/2026\t\thttps://ansm.sante.fr/disponibilites/tension-beta",
        "93000002\t\t2\tTension d'approvisionnement\t01/09/2026\t28/09/2026\t\thttps://ansm.sante.fr/disponibilites/tension-beta",
        # Doublon strictement identique de la ligne précédente (doit être dédupliqué)
        "93000002\t\t2\tTension d'approvisionnement\t01/09/2026\t28/09/2026\t\thttps://ansm.sante.fr/disponibilites/tension-beta",
        # Tension ciblée sur un CIP13 précis
        "93000002\t3400933000028\t2\tTension d'approvisionnement\t15/10/2026\t01/11/2026\t\thttps://ansm.sante.fr/disponibilites/tension-beta-boite",
        # Arrêt de commercialisation (code 3) avec date future
        "93000003\t\t3\tArrêt de commercialisation\t01/07/2027\t07/08/2026\t\thttps://ansm.sante.fr/disponibilites/arret-gamma",
        # Remise à disposition (code 4) avec date de remise
        "93000001\t3400933000011\t4\tRemise à disposition\t01/08/2026\t20/10/2026\t19/10/2026\thttps://ansm.sante.fr/disponibilites/remise-alpha",
        # Code CIS orphelin (absent de specialites)
        "99999999\t\t1\tRupture de stock\t01/10/2026\t02/11/2026\t\thttps://ansm.sante.fr/disponibilites/orphelin",
        # Ligne malformée (CIP13 invalide)
        "93000001\t12345\t2\tTension d'approvisionnement\t01/10/2026\t02/11/2026\t\thttps://ansm.sante.fr/disponibilites/bad",
    ]
    (raw_dir / "CIS_CIP_Dispo_Spec.txt").write_bytes(
        ("\r\n".join(dispo_lines) + "\r\n").encode("cp1252")
    )

    # 2. HAS_LiensPageCT_bdpm.txt (2 colonnes, UTF-8) — plusieurs URLs pour un même dossier
    ct_lines = [
        "CT-90001\thttps://www.has-sante.fr/jcms/p_90001_a",
        "CT-90001\thttps://www.has-sante.fr/jcms/p_90001_b",
        "CT-90001\thttps://www.has-sante.fr/jcms/p_90001_c",
        # Doublon exact (dossier + URL) -> ignoré
        "CT-90001\thttps://www.has-sante.fr/jcms/p_90001_a",
        "CT-90002\thttps://www.has-sante.fr/jcms/p_90002",
        # URL non http/https -> malformée
        "CT-90003\tjavascript:alert(1)",
    ]
    (raw_dir / "HAS_LiensPageCT_bdpm.txt").write_bytes(
        ("\n".join(ct_lines) + "\n").encode("utf-8")
    )

    # 3. CIS_HAS_SMR_bdpm.txt (6 colonnes, cp1252) — multi-indications + <br> + '<' médicaux
    smr_lines = [
        "93000001\tCT-90001\tInscription (CT)\t20261007\tImportant\tLe SMR est important chez les enfants (< 18 ans) avec T-score < -3 DS.<br>• Critère confirmé (p < 0,0001).<BR/>• Surveillance requise.",
        "93000001\tCT-90001\tInscription (CT)\t20261007\tInsuffisant\tDans les autres situations de l’AMM (> 65 ans, IMC < 19 kg/m²), le SMR est insuffisant.<br />Aucune prise en charge.",
        # Doublon strictement identique -> ignoré
        "93000001\tCT-90001\tInscription (CT)\t20261007\tInsuffisant\tDans les autres situations de l’AMM (> 65 ans, IMC < 19 kg/m²), le SMR est insuffisant.<br />Aucune prise en charge.",
        # Deuxième spécialité avec SMR conditionnel
        "93000002\tCT-90002\tRéévaluation SMR\t20250415\tImportant conditionnel\tSMR important sous réserve d'étude post-inscription.",
        # Code CIS historique absent de la base actuelle (orphelin)
        "98888888\tCT-10001\tInscription (CT)\t20050112\tModéré\tSpécialité historique radiée.",
    ]
    (raw_dir / "CIS_HAS_SMR_bdpm.txt").write_bytes(
        ("\r\n".join(smr_lines) + "\r\n").encode("cp1252")
    )

    # 4. CIS_HAS_ASMR_bdpm.txt (6 colonnes, cp1252)
    asmr_lines = [
        "93000001\tCT-90001\tInscription (CT)\t20261007\tIII\tCompte tenu :<br>• d'une réduction significative (p < 0,0001) chez les patients < 18 ans,<br>• d'un profil de tolérance acceptable.",
        "93000001\tCT-90001\tInscription (CT)\t20261007\tV\tAbsence d'amélioration du SMR dans les autres indications.",
        "93000002\tCT-90002\tExtension d'indication\t20250415\tCommentaires sans chiffrage de l'ASMR\tDonnées insuffisantes (< 12 mois de recul).",
        # Code CIS historique orphelin
        "98888888\tCT-10001\tInscription (CT)\t20050112\tV\tAncien médicament.",
    ]
    (raw_dir / "CIS_HAS_ASMR_bdpm.txt").write_bytes(
        ("\r\n".join(asmr_lines) + "\r\n").encode("cp1252")
    )

    # 5. CIS_MITM.txt (7 colonnes réelles, cp1252)
    mitm_lines = [
        "93000001\tA02BC01\tMEDTEST ALPHA 500 mg, comprimé\tLABO ALPHA\tEXPLOITANT ALPHA - LYON\t05/10/2026\thttp://base-donnees-publique.medicaments.gouv.fr/extrait.php?specid=93000001",
        # Exploitant vide (facultatif — 20 cas réels constatés)
        "93000002\tC03CA\tMEDTEST BETA 20 mg, gélule\tLABO BETA\t\t18/12/2024\thttp://base-donnees-publique.medicaments.gouv.fr/extrait.php?specid=93000002",
        # Doublon CIS -> ignoré
        "93000001\tA02BC01\tMEDTEST ALPHA 500 mg, comprimé\tLABO ALPHA\tEXPLOITANT ALPHA - LYON\t05/10/2026\thttp://base-donnees-publique.medicaments.gouv.fr/extrait.php?specid=93000001",
        # Code CIS orphelin -> ignoré
        "97777777\tN05BA01\tORPHELIN MITM\tLABO X\tEXPLOITANT X\t13/03/2026\thttp://base-donnees-publique.medicaments.gouv.fr/extrait.php?specid=97777777",
    ]
    (raw_dir / "CIS_MITM.txt").write_bytes(
        ("\r\n".join(mitm_lines) + "\r\n").encode("cp1252")
    )

    if include_metadata_json:
        meta = {
            "portal_url": "https://base-donnees-publique.medicaments.gouv.fr/telechargement",
            "portal_global_date": "03/11/2026",
            "files": {
                "CIS_CIP_Dispo_Spec.txt": {
                    "status": "ok",
                    "downloaded_at_utc": "2026-11-24T23:35:31Z",
                    "http_last_modified": "Mon, 02 Nov 2026 08:00:00 GMT",
                    "official_source_date": "03/11/2026",
                    "official_date_verified_from": "Page officielle BDPM /telechargement",
                },
                "CIS_HAS_SMR_bdpm.txt": {
                    "status": "ok",
                    "downloaded_at_utc": "2026-11-24T23:35:31Z",
                    "http_last_modified": None,
                    "official_source_date": "20/11/2026",
                    "official_date_verified_from": "Page officielle BDPM /telechargement",
                },
                "CIS_HAS_ASMR_bdpm.txt": {
                    "status": "ok",
                    "downloaded_at_utc": "2026-11-24T23:35:31Z",
                    "http_last_modified": None,
                    "official_source_date": "20/11/2026",
                    "official_date_verified_from": "Page officielle BDPM /telechargement",
                },
                "HAS_LiensPageCT_bdpm.txt": {
                    "status": "ok",
                    "downloaded_at_utc": "2026-11-24T23:35:31Z",
                    "http_last_modified": "Tue, 24 Nov 2026 07:00:00 GMT",
                    # Cas volontaire sans date officielle : Last-Modified NE DOIT PAS devenir la date officielle
                    "official_source_date": None,
                    "official_date_verified_from": "Non fournie par la source",
                },
                "CIS_MITM.txt": {
                    "status": "ok",
                    "downloaded_at_utc": "2026-11-24T23:35:31Z",
                    "http_last_modified": None,
                    "official_source_date": "13/10/2026",
                    "official_date_verified_from": "Page officielle BDPM /telechargement",
                },
            },
        }
        (raw_dir / "metadata_sources.json").write_text(
            json.dumps(meta, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


class TestLot2CSchemaAndImporters(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.raw_dir = self.base_path / "raw"
        self.backup_dir = self.base_path / "backups"
        self.db_path = self.base_path / "test_lot2c.sqlite3"
        _write_palier1_synthetic_files(self.raw_dir)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_no_automatic_migration_on_startup_and_explicit_backup_migrate_restore(self) -> None:
        # 1. Initialise une base purement Palier 1 (sans les 6 tables Lot 2C)
        import_bdpm_directory(
            raw_dir=self.raw_dir,
            db_path=self.db_path,
            include_lot2c=False,
        )
        before = inspect_schema_status(self.db_path)
        self.assertTrue(before["exists"])
        self.assertEqual(before["palier1_missing"], [])
        self.assertFalse(before["lot2c_migrated"])
        self.assertEqual(before["lot2c_present"], [])
        self.assertEqual(set(before["lot2c_missing"]), set(LOT2C_TABLES))

        # 2. Démarrage normal de l'application FastAPI : AUCUNE migration automatique !
        app = create_app(db_path=self.db_path)
        client = TestClient(app)
        self.assertEqual(client.get("/api/health").status_code, 200)
        self.assertEqual(client.get("/").status_code, 200)
        self.assertEqual(client.get("/medicaments/93000001").status_code, 200)

        after_startup = inspect_schema_status(self.db_path)
        self.assertFalse(after_startup["lot2c_migrated"])
        self.assertEqual(after_startup["lot2c_present"], [])

        # 3. `import_lot2c_sources` refuse de tourner tant que la migration explicite n'est pas faite
        _write_lot2c_synthetic_files(self.raw_dir)
        with self.assertRaises(RuntimeError):
            import_lot2c_sources(raw_dir=self.raw_dir, db_path=self.db_path)

        # 4. Migration explicite avec sauvegarde vérifiée préalable
        mig = migrate_lot2c_schema(
            self.db_path,
            create_backup=True,
            backup_dir=self.backup_dir,
        )
        self.assertFalse(mig["already_migrated"])
        self.assertTrue(mig["integrity_ok"])
        self.assertIsNotNone(mig["backup"])
        backup_file = Path(mig["backup"]["backup_path"])
        self.assertTrue(backup_file.exists())
        self.assertTrue(mig["backup"]["integrity_ok"])
        self.assertEqual(len(mig["backup"]["sha256"]), 64)

        migrated_status = inspect_schema_status(self.db_path)
        self.assertTrue(migrated_status["lot2c_migrated"])
        self.assertEqual(migrated_status["schema_version"], "2c")
        self.assertEqual(migrated_status["table_counts"]["specialites"], 3)
        self.assertEqual(migrated_status["table_counts"]["presentations"], 2)

        # 5. Migration idempotente si exécutée une seconde fois
        mig2 = migrate_lot2c_schema(
            self.db_path,
            create_backup=True,
            backup_dir=self.backup_dir,
        )
        self.assertTrue(mig2["already_migrated"])
        self.assertEqual(mig2["tables_created"], [])

        # 6. Restauration complète depuis la sauvegarde vérifiée -> retour exact à l'état Palier 1
        restored = restore_database(backup_file, target_db_path=self.db_path)
        self.assertTrue(restored["integrity_ok"])
        restored_status = inspect_schema_status(self.db_path)
        self.assertFalse(restored_status["lot2c_migrated"])
        self.assertEqual(set(restored_status["palier1_present"]), set(PALIER1_TABLES))
        self.assertEqual(restored_status["table_counts"]["specialites"], 3)

        # 7. Sauvegarde manuelle autonome vérifiable
        manual_bkp = backup_database(self.db_path, backup_dir=self.backup_dir, label="test_manuel")
        self.assertTrue(Path(manual_bkp["backup_path"]).exists())
        self.assertTrue(manual_bkp["integrity_ok"])

    def test_import_all_five_sources_duplicates_orphans_and_status_distinction(self) -> None:
        _write_lot2c_synthetic_files(self.raw_dir)
        report = import_bdpm_directory(raw_dir=self.raw_dir, db_path=self.db_path)

        # 1. Disponibilités : 6 lignes valides (dont 2 dates MAJ distinctes pour 93000002), 1 doublon exact, 1 orphelin, 1 malformée
        dispo_stats = report.files["CIS_CIP_Dispo_Spec.txt"]
        self.assertEqual(dispo_stats.import_status, "ok")
        self.assertEqual(dispo_stats.rows_imported, 6)
        self.assertEqual(dispo_stats.rows_skipped_duplicate, 1)
        self.assertEqual(dispo_stats.rows_skipped_orphan, 1)
        self.assertEqual(dispo_stats.rows_malformed, 1)

        # 2. Liens HAS CT : 4 liens valides (3 pour CT-90001 + 1 pour CT-90002), 1 doublon exact, 1 URL invalide
        ct_stats = report.files["HAS_LiensPageCT_bdpm.txt"]
        self.assertEqual(ct_stats.import_status, "ok")
        self.assertEqual(ct_stats.rows_imported, 4)
        self.assertEqual(ct_stats.rows_skipped_duplicate, 1)
        self.assertEqual(ct_stats.rows_malformed, 1)

        # 3. HAS SMR : 4 avis conservés (3 rattachés + 1 CIS historique non rattaché), 1 doublon exact
        smr_stats = report.files["CIS_HAS_SMR_bdpm.txt"]
        self.assertEqual(smr_stats.import_status, "ok")
        self.assertEqual(smr_stats.rows_imported, 4)
        self.assertEqual(smr_stats.rows_skipped_duplicate, 1)
        self.assertEqual(smr_stats.rows_skipped_orphan, 0)
        self.assertEqual(smr_stats.rows_historical_unattached, 1)

        # 4. HAS ASMR : 4 avis conservés (3 rattachés + 1 CIS historique non rattaché)
        asmr_stats = report.files["CIS_HAS_ASMR_bdpm.txt"]
        self.assertEqual(asmr_stats.import_status, "ok")
        self.assertEqual(asmr_stats.rows_imported, 4)
        self.assertEqual(asmr_stats.rows_skipped_orphan, 0)
        self.assertEqual(asmr_stats.rows_historical_unattached, 1)

        # 5. MITM : 2 spécialités valides, 1 doublon CIS, 1 CIS orphelin
        mitm_stats = report.files["CIS_MITM.txt"]
        self.assertEqual(mitm_stats.import_status, "ok")
        self.assertEqual(mitm_stats.rows_imported, 2)
        self.assertEqual(mitm_stats.rows_skipped_duplicate, 1)
        self.assertEqual(mitm_stats.rows_skipped_orphan, 1)

        with get_connection(self.db_path) as conn:
            # Vérifie que les 2 historiques de mise à jour de 93000002 (CIP13 NULL) sont conservés
            # et que la tension (code 2) n'est JAMAIS transformée en rupture (code 1)
            beta_rows = conn.execute(
                """
                SELECT code_cip13, code_statut, libelle_statut, date_debut, date_mise_a_jour
                FROM disponibilites_spec
                WHERE code_cis = '93000002'
                ORDER BY IFNULL(code_cip13, ''), date_mise_a_jour ASC
                """
            ).fetchall()
            self.assertEqual(len(beta_rows), 3)
            self.assertIsNone(beta_rows[0]["code_cip13"])
            self.assertIsNone(beta_rows[1]["code_cip13"])
            self.assertEqual(
                {beta_rows[0]["date_mise_a_jour"], beta_rows[1]["date_mise_a_jour"]},
                {"28/09/2026", "02/11/2026"},
            )
            self.assertEqual(beta_rows[2]["code_cip13"], "3400933000028")
            for row in beta_rows:
                self.assertEqual(row["code_statut"], "2")
                self.assertEqual(row["libelle_statut"], "Tension d'approvisionnement")

            # Vérifie la rupture distincte (code 1) et la remise à disposition (code 4) sur 93000001
            alpha_statuts = {
                (r["code_statut"], r["libelle_statut"], r["date_remise_disposition"])
                for r in conn.execute(
                    "SELECT code_statut, libelle_statut, date_remise_disposition FROM disponibilites_spec WHERE code_cis = '93000001'"
                ).fetchall()
            }
            self.assertIn(("1", "Rupture de stock", None), alpha_statuts)
            self.assertIn(("4", "Remise à disposition", "19/10/2026"), alpha_statuts)

            # Vérifie que CT-90001 conserve bien ses 3 URLs distinctes dans has_liens_ct
            ct_urls = [
                r["lien_page_ct"]
                for r in conn.execute(
                    "SELECT lien_page_ct FROM has_liens_ct WHERE code_dossier_has = 'CT-90001' ORDER BY lien_page_ct"
                ).fetchall()
            ]
            self.assertEqual(
                ct_urls,
                [
                    "https://www.has-sante.fr/jcms/p_90001_a",
                    "https://www.has-sante.fr/jcms/p_90001_b",
                    "https://www.has-sante.fr/jcms/p_90001_c",
                ],
            )

            # Vérifie que les 2 indications SMR de 93000001 restent distinctes (aucune note globale)
            smr_rows = conn.execute(
                "SELECT valeur_smr, date_avis, libelle_smr FROM has_avis_smr WHERE code_cis = '93000001' ORDER BY valeur_smr"
            ).fetchall()
            self.assertEqual(len(smr_rows), 2)
            self.assertEqual([r["valeur_smr"] for r in smr_rows], ["Important", "Insuffisant"])
            self.assertEqual(smr_rows[0]["date_avis"], "2026-10-07")

            # Vérifie que `Last-Modified` n'est JAMAIS copié dans `last_success_official_date`
            ct_fresh = conn.execute(
                "SELECT last_success_official_date, http_last_modified FROM sources_fraicheur WHERE source_key = 'HAS_LiensPageCT_bdpm.txt'"
            ).fetchone()
            self.assertIsNone(ct_fresh["last_success_official_date"])
            self.assertEqual(ct_fresh["http_last_modified"], "Tue, 24 Nov 2026 07:00:00 GMT")

    def test_medical_text_cleaning_preserves_inequalities_and_only_replaces_br(self) -> None:
        # Tests unitaires directs sur des expressions médicales piégeuses
        raw_expr = (
            "Enfants (< 18 ans) avec T-score < -3 DS et p < 0,0001.<br>"
            "• Sujets > 65 ans avec débit < 30 mL/min<BR/>"
            "• IMC < 19 kg/m²<br />Fin."
        )
        cleaned = clean_has_medical_text(raw_expr)
        self.assertIsNotNone(cleaned)
        assert cleaned is not None
        self.assertIn("< 18 ans", cleaned)
        self.assertIn("< -3 DS", cleaned)
        self.assertIn("p < 0,0001", cleaned)
        self.assertIn("> 65 ans", cleaned)
        self.assertIn("< 30 mL/min", cleaned)
        self.assertIn("< 19 kg/m²", cleaned)
        self.assertNotIn("<br>", cleaned.lower())
        self.assertNotIn("<br/>", cleaned.lower())
        self.assertEqual(len(cleaned.splitlines()), 4)

        # Vérifie que Jinja2 échappe automatiquement `<` et les balises éventuelles sans altérer le sens médical
        env = Environment(autoescape=True)
        rendered = env.from_string("<p>{{ txt }}</p>").render(txt=cleaned)
        self.assertIn("&lt; 18 ans", rendered)
        self.assertIn("p &lt; 0,0001", rendered)
        self.assertIn("&lt; -3 DS", rendered)

        # Vérifie également après import complet dans SQLite (SMR et ASMR)
        _write_lot2c_synthetic_files(self.raw_dir)
        import_bdpm_directory(raw_dir=self.raw_dir, db_path=self.db_path)
        with get_connection(self.db_path) as conn:
            smr_texts = [
                r["libelle_smr"]
                for r in conn.execute(
                    "SELECT libelle_smr FROM has_avis_smr WHERE code_cis = '93000001'"
                ).fetchall()
            ]
            combined_smr = "\n".join(smr_texts)
            self.assertIn("< 18 ans", combined_smr)
            self.assertIn("< -3 DS", combined_smr)
            self.assertIn("p < 0,0001", combined_smr)
            self.assertIn("> 65 ans", combined_smr)
            self.assertIn("< 19 kg/m²", combined_smr)
            self.assertNotIn("<br", combined_smr.lower())

            asmr_texts = "\n".join(
                r["libelle_asmr"]
                for r in conn.execute(
                    "SELECT libelle_asmr FROM has_avis_asmr WHERE code_cis = '93000001'"
                ).fetchall()
            )
            self.assertIn("p < 0,0001", asmr_texts)
            self.assertIn("< 18 ans", asmr_texts)
            self.assertNotIn("<br", asmr_texts.lower())

    def test_mitm_seven_real_columns_and_optional_exploitant(self) -> None:
        _write_lot2c_synthetic_files(self.raw_dir)
        import_bdpm_directory(raw_dir=self.raw_dir, db_path=self.db_path)

        with get_connection(self.db_path) as conn:
            r1 = conn.execute(
                "SELECT * FROM specialites_mitm WHERE code_cis = '93000001'"
            ).fetchone()
            self.assertIsNotNone(r1)
            self.assertEqual(r1["code_atc"], "A02BC01")
            self.assertEqual(r1["denomination"], "MEDTEST ALPHA 500 mg, comprimé")
            self.assertEqual(r1["titulaire"], "LABO ALPHA")
            self.assertEqual(r1["exploitant"], "EXPLOITANT ALPHA - LYON")
            self.assertEqual(r1["date_publication"], "05/10/2026")
            self.assertEqual(
                r1["lien_bdpm"],
                "http://base-donnees-publique.medicaments.gouv.fr/extrait.php?specid=93000001",
            )

            # Colonne facultative `exploitant` vide -> NULL propre (None)
            r2 = conn.execute(
                "SELECT * FROM specialites_mitm WHERE code_cis = '93000002'"
            ).fetchone()
            self.assertIsNotNone(r2)
            self.assertEqual(r2["code_atc"], "C03CA")
            self.assertIsNone(r2["exploitant"])
            self.assertEqual(r2["date_publication"], "18/12/2024")

    def test_idempotent_repeated_imports_no_duplicates_no_data_loss(self) -> None:
        _write_lot2c_synthetic_files(self.raw_dir)
        import_bdpm_directory(raw_dir=self.raw_dir, db_path=self.db_path)
        counts_first = inspect_schema_status(self.db_path)["table_counts"]

        # Second import complet + deux imports Lot 2C autonomes
        import_bdpm_directory(raw_dir=self.raw_dir, db_path=self.db_path)
        import_lot2c_sources(raw_dir=self.raw_dir, db_path=self.db_path)
        import_lot2c_sources(raw_dir=self.raw_dir, db_path=self.db_path)
        counts_after = inspect_schema_status(self.db_path)["table_counts"]

        self.assertEqual(counts_first, counts_after)
        self.assertEqual(counts_after["disponibilites_spec"], 6)
        self.assertEqual(counts_after["has_liens_ct"], 4)
        self.assertEqual(counts_after["has_avis_smr"], 4)
        self.assertEqual(counts_after["has_avis_asmr"], 4)
        self.assertEqual(counts_after["specialites_mitm"], 2)

    def test_protection_missing_empty_malformed_and_failed_download_preserve_data(self) -> None:
        _write_lot2c_synthetic_files(self.raw_dir)
        import_bdpm_directory(raw_dir=self.raw_dir, db_path=self.db_path)

        # Fige une ancienne date de dernier succès dans `sources_fraicheur` pour prouver qu'elle n'est JAMAIS écrasée
        old_success_ts = "2026-10-01T08:00:00Z"
        with get_connection(self.db_path) as conn:
            conn.execute(
                "UPDATE sources_fraicheur SET last_success_imported_at_utc = ?;",
                (old_success_ts,),
            )
            conn.commit()

        # Simule 4 types de pannes simultanées sur les fichiers facultatifs :
        # 1) CIS_MITM.txt absent (supprimé)
        (self.raw_dir / "CIS_MITM.txt").unlink()
        # 2) CIS_HAS_SMR_bdpm.txt vide (0 octet)
        (self.raw_dir / "CIS_HAS_SMR_bdpm.txt").write_bytes(b"")
        # 3) CIS_HAS_ASMR_bdpm.txt malformé (page d'erreur HTML)
        (self.raw_dir / "CIS_HAS_ASMR_bdpm.txt").write_text(
            "<html><body>503 Service Temporarily Unavailable</body></html>\n",
            encoding="utf-8",
        )
        # 4) CIS_CIP_Dispo_Spec.txt en échec de téléchargement dans metadata_sources.json
        meta_path = self.raw_dir / "metadata_sources.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["files"]["CIS_CIP_Dispo_Spec.txt"]["status"] = "error"
        meta["files"]["CIS_CIP_Dispo_Spec.txt"]["error"] = "HTTP Error 503: Service Unavailable"
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

        # Relance un réimport COMPLET (incluant CIS_bdpm.txt) : même avec `specialites` mis à jour,
        # aucune donnée Lot 2C existante ne doit être supprimée en cascade !
        report = import_bdpm_directory(raw_dir=self.raw_dir, db_path=self.db_path)

        self.assertEqual(report.files["CIS_MITM.txt"].import_status, "skipped_missing")
        self.assertTrue(report.files["CIS_MITM.txt"].preserved_previous_data)
        self.assertEqual(report.files["CIS_HAS_SMR_bdpm.txt"].import_status, "rejected_empty")
        self.assertTrue(report.files["CIS_HAS_SMR_bdpm.txt"].preserved_previous_data)
        self.assertEqual(report.files["CIS_HAS_ASMR_bdpm.txt"].import_status, "rejected_malformed")
        self.assertTrue(report.files["CIS_HAS_ASMR_bdpm.txt"].preserved_previous_data)
        self.assertEqual(report.files["CIS_CIP_Dispo_Spec.txt"].import_status, "preserved_download_error")
        self.assertTrue(report.files["CIS_CIP_Dispo_Spec.txt"].preserved_previous_data)
        # HAS_LiensPageCT_bdpm.txt reste valide et est bien mis à jour
        self.assertEqual(report.files["HAS_LiensPageCT_bdpm.txt"].import_status, "ok")

        # Vérifie que TOUTES les anciennes données valides sont toujours en base
        counts = inspect_schema_status(self.db_path)["table_counts"]
        self.assertEqual(counts["specialites_mitm"], 2)
        self.assertEqual(counts["has_avis_smr"], 4)
        self.assertEqual(counts["has_avis_asmr"], 4)
        self.assertEqual(counts["disponibilites_spec"], 6)
        self.assertEqual(counts["has_liens_ct"], 4)

        # Vérifie que `sources_fraicheur` distingue bien la dernière tentative de la dernière réussite
        with get_connection(self.db_path) as conn:
            for key, expected_import_status, expected_present, expected_dl in (
                ("CIS_MITM.txt", "skipped_missing", 0, "ok"),
                ("CIS_HAS_SMR_bdpm.txt", "rejected_empty", 1, "ok"),
                ("CIS_HAS_ASMR_bdpm.txt", "rejected_malformed", 1, "ok"),
                ("CIS_CIP_Dispo_Spec.txt", "preserved_download_error", 1, "error"),
            ):
                row = conn.execute(
                    "SELECT * FROM sources_fraicheur WHERE source_key = ?",
                    (key,),
                ).fetchone()
                self.assertEqual(row["import_status"], expected_import_status)
                self.assertEqual(row["file_present"], expected_present)
                self.assertEqual(row["download_status"], expected_dl)
                # La date de dernière réussite reste celle d'origine : jamais rajeunie artificiellement !
                self.assertEqual(row["last_success_imported_at_utc"], old_success_ts)
                self.assertNotEqual(row["last_attempt_at_utc"], old_success_ts)
                self.assertIsNotNone(row["last_attempt_error"])

    def test_rollback_on_sqlite_insert_failure_and_empty_master_file(self) -> None:
        _write_lot2c_synthetic_files(self.raw_dir)
        import_bdpm_directory(raw_dir=self.raw_dir, db_path=self.db_path)

        old_success_ts = "2026-10-15T12:00:00Z"
        with get_connection(self.db_path) as conn:
            conn.execute(
                "UPDATE sources_fraicheur SET last_success_imported_at_utc = ? WHERE source_key = 'CIS_CIP_Dispo_Spec.txt';",
                (old_success_ts,),
            )
            # Trigger SQLite qui provoque un échec en plein milieu de l'insertion sur la 2e ligne
            conn.execute(
                """
                CREATE TRIGGER trg_fail_second_dispo
                BEFORE INSERT ON disponibilites_spec
                WHEN (SELECT COUNT(*) FROM disponibilites_spec) >= 1
                BEGIN
                    SELECT RAISE(ABORT, 'simulated mid-insert sqlite failure');
                END;
                """
            )
            conn.commit()

        # Exécute import_lot2c_sources : DELETE + ligne 1 ont lieu dans le SAVEPOINT, puis ligne 2 échoue -> ROLLBACK !
        report = import_lot2c_sources(raw_dir=self.raw_dir, db_path=self.db_path)
        dispo_stats = report.files["CIS_CIP_Dispo_Spec.txt"]
        self.assertEqual(dispo_stats.import_status, "error_rollback")
        self.assertTrue(dispo_stats.preserved_previous_data)
        self.assertEqual(dispo_stats.active_rows_count, 6)

        with get_connection(self.db_path) as conn:
            # Les 6 lignes antérieures de disponibilites_spec sont 100 % préservées !
            cnt = conn.execute("SELECT COUNT(*) FROM disponibilites_spec;").fetchone()[0]
            self.assertEqual(cnt, 6)
            fresh = conn.execute(
                "SELECT import_status, last_success_imported_at_utc, last_attempt_error FROM sources_fraicheur WHERE source_key = 'CIS_CIP_Dispo_Spec.txt';"
            ).fetchone()
            self.assertEqual(fresh["import_status"], "error_rollback")
            self.assertEqual(fresh["last_success_imported_at_utc"], old_success_ts)
            self.assertIn("simulated mid-insert sqlite failure", fresh["last_attempt_error"])
            # Les autres sources valides (ex. MITM) ont bien réussi
            self.assertEqual(report.files["CIS_MITM.txt"].import_status, "ok")
            conn.execute("DROP TRIGGER trg_fail_second_dispo;")
            conn.commit()

        # Vérifie aussi qu'un CIS_bdpm.txt vide/corrompu est rejeté AVANT toute écriture SQLite
        (self.raw_dir / "CIS_bdpm.txt").write_bytes(b"")
        with self.assertRaises(ValueError):
            import_bdpm_directory(raw_dir=self.raw_dir, db_path=self.db_path)
        # Toutes les données existantes restent 100 % intactes
        status_after = inspect_schema_status(self.db_path)
        self.assertEqual(status_after["table_counts"]["specialites"], 3)
        self.assertEqual(status_after["table_counts"]["disponibilites_spec"], 6)
        self.assertEqual(status_after["table_counts"]["specialites_mitm"], 2)

    def test_historical_has_opinions_preserved_distinguished_and_reattached_when_cis_reappears(self) -> None:
        _write_lot2c_synthetic_files(self.raw_dir)
        import_bdpm_directory(raw_dir=self.raw_dir, db_path=self.db_path)

        with get_connection(self.db_path) as conn:
            # 98888888 est absent de specialites mais ses avis SMR & ASMR sont conservés et marqués `specialite_presente = 0`
            smr_hist = conn.execute(
                "SELECT code_cis, specialite_presente, valeur_smr FROM has_avis_smr WHERE code_cis = '98888888';"
            ).fetchone()
            self.assertIsNotNone(smr_hist)
            self.assertEqual(smr_hist["specialite_presente"], 0)
            self.assertEqual(smr_hist["valeur_smr"], "Modéré")

            asmr_hist = conn.execute(
                "SELECT code_cis, specialite_presente, valeur_asmr FROM has_avis_asmr WHERE code_cis = '98888888';"
            ).fetchone()
            self.assertIsNotNone(asmr_hist)
            self.assertEqual(asmr_hist["specialite_presente"], 0)

            # Les avis rattachés ont bien `specialite_presente = 1`
            attached_cnt = conn.execute(
                "SELECT COUNT(*) FROM has_avis_smr WHERE specialite_presente = 1;"
            ).fetchone()[0]
            self.assertEqual(attached_cnt, 3)

            smr_fresh = conn.execute(
                "SELECT active_rows_count, historical_unattached_count FROM sources_fraicheur WHERE source_key = 'CIS_HAS_SMR_bdpm.txt';"
            ).fetchone()
            self.assertEqual(smr_fresh["active_rows_count"], 4)
            self.assertEqual(smr_fresh["historical_unattached_count"], 1)

        # Si la spécialité 98888888 réapparaît ultérieurement dans CIS_bdpm.txt (sans même réimporter HAS),
        # ses avis historiques sont rattachés automatiquement (`specialite_presente` passe de 0 à 1) !
        cis_path = self.raw_dir / "CIS_bdpm.txt"
        existing_cis = cis_path.read_bytes().decode("cp1252")
        reappeared_line = (
            "98888888\tMEDTEST HISTORIQUE 10 mg, comprimé\tcomprimé\torale\tAutorisation active\t"
            "Procédure nationale\tCommercialisée\t01/01/2005\t\t\tLABO HIST\tNon\r\n"
        )
        cis_path.write_bytes((existing_cis + reappeared_line).encode("cp1252"))
        import_bdpm_directory(raw_dir=self.raw_dir, db_path=self.db_path, include_lot2c=False)

        with get_connection(self.db_path) as conn:
            smr_after = conn.execute(
                "SELECT specialite_presente FROM has_avis_smr WHERE code_cis = '98888888';"
            ).fetchone()
            asmr_after = conn.execute(
                "SELECT specialite_presente FROM has_avis_asmr WHERE code_cis = '98888888';"
            ).fetchone()
            self.assertEqual(smr_after["specialite_presente"], 1)
            self.assertEqual(asmr_after["specialite_presente"], 1)
            smr_fresh_after = conn.execute(
                "SELECT historical_unattached_count FROM sources_fraicheur WHERE source_key = 'CIS_HAS_SMR_bdpm.txt';"
            ).fetchone()
            self.assertEqual(smr_fresh_after["historical_unattached_count"], 0)

    def test_missing_dispo_cis_counted_safely_without_corrupting_valid_rows(self) -> None:
        # Simule 25 CIS absents (comme le décalage constaté entre Palier 1 et CIS_CIP_Dispo_Spec.txt)
        _write_lot2c_synthetic_files(self.raw_dir)
        dispo_path = self.raw_dir / "CIS_CIP_Dispo_Spec.txt"
        lines = [
            "93000001\t\t1\tRupture de stock\t10/10/2026\t02/11/2026\t\thttps://ansm.sante.fr/disponibilites/valid-1",
            "93000002\t\t2\tTension d'approvisionnement\t01/09/2026\t02/11/2026\t\thttps://ansm.sante.fr/disponibilites/valid-2",
        ]
        for idx in range(25):
            missing_cis = f"990000{idx:02d}"
            lines.append(
                f"{missing_cis}\t\t2\tTension d'approvisionnement\t01/10/2026\t02/11/2026\t\thttps://ansm.sante.fr/disponibilites/missing-{idx}"
            )
        dispo_path.write_bytes(("\r\n".join(lines) + "\r\n").encode("cp1252"))

        report = import_bdpm_directory(raw_dir=self.raw_dir, db_path=self.db_path)
        stats = report.files["CIS_CIP_Dispo_Spec.txt"]
        self.assertEqual(stats.import_status, "ok")
        self.assertEqual(stats.rows_imported, 2)
        self.assertEqual(stats.rows_skipped_orphan, 25)
        self.assertEqual(stats.rows_malformed, 0)

        with get_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT active_rows_count, last_attempt_skipped_orphan FROM sources_fraicheur WHERE source_key = 'CIS_CIP_Dispo_Spec.txt';"
            ).fetchone()
            self.assertEqual(row["active_rows_count"], 2)
            self.assertEqual(row["last_attempt_skipped_orphan"], 25)

    def test_eleven_sources_freshness_coherence_after_lot2c_import_without_palier1_reimport(self) -> None:
        # 1. Base purement Palier 1 avec dates d'octobre enregistrées
        import_bdpm_directory(raw_dir=self.raw_dir, db_path=self.db_path, include_lot2c=False)
        palier1_import_ts = "2026-10-26T17:01:48Z"
        with get_connection(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO import_metadata (key, value) VALUES ('imported_at_utc', ?);",
                (palier1_import_ts,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO import_metadata (key, value) VALUES ('official_bdpm_date', '29/09/2026');"
            )
            conn.commit()
        (self.raw_dir / "metadata_telechargement.txt").write_text(
            "downloaded_at_utc=2026-10-26T17:01:42Z\nofficial_bdpm_date=29/09/2026\n",
            encoding="utf-8",
        )

        # 2. Migration explicite puis import Lot 2C UNIQUEMENT (sans réimporter Palier 1)
        migrate_lot2c_schema(self.db_path, create_backup=True, backup_dir=self.backup_dir)
        _write_lot2c_synthetic_files(self.raw_dir)
        import_lot2c_sources(raw_dir=self.raw_dir, db_path=self.db_path)

        with get_connection(self.db_path) as conn:
            rows = {
                r["source_key"]: dict(r)
                for r in conn.execute("SELECT * FROM sources_fraicheur;").fetchall()
            }
            # Les 11 sources (6 Palier 1 + 5 Lot 2C) doivent TOUTES être présentes
            self.assertEqual(len(rows), 11)

            # Vérifie distinction stricte date officielle / téléchargement / import pour Palier 1
            cis_row = rows["CIS_bdpm.txt"]
            self.assertEqual(cis_row["last_success_official_date"], "29/09/2026")
            self.assertEqual(cis_row["last_success_downloaded_at_utc"], "2026-10-26T17:01:42Z")
            self.assertEqual(cis_row["last_success_imported_at_utc"], palier1_import_ts)
            self.assertEqual(cis_row["active_rows_count"], 3)

            # CIS_InfoImportantes.txt (génération en direct) -> AUCUNE date officielle inventée !
            info_row = rows["CIS_InfoImportantes.txt"]
            self.assertIsNone(info_row["last_success_official_date"])
            self.assertIn("direct", (info_row["last_success_official_date_origin"] or "").lower())

            # Les 5 sources Lot 2C gardent leur propre date officielle et date d'import distincte de Palier 1
            self.assertEqual(rows["CIS_MITM.txt"]["last_success_official_date"], "13/10/2026")
            self.assertEqual(rows["CIS_CIP_Dispo_Spec.txt"]["last_success_official_date"], "03/11/2026")
            self.assertNotEqual(rows["CIS_MITM.txt"]["last_success_imported_at_utc"], palier1_import_ts)

    def test_backup_restore_after_simulated_corruption_preserves_all_eleven_sources(self) -> None:
        _write_lot2c_synthetic_files(self.raw_dir)
        import_bdpm_directory(raw_dir=self.raw_dir, db_path=self.db_path)
        expected_counts = inspect_schema_status(self.db_path)["table_counts"]
        self.assertEqual(expected_counts["sources_fraicheur"], 11)

        # Crée une sauvegarde vérifiée
        bkp = backup_database(self.db_path, backup_dir=self.backup_dir, label="pre_incident")
        self.assertTrue(bkp["integrity_ok"])

        # Simule une suppression accidentelle massive sur la base de travail
        with get_connection(self.db_path) as conn:
            conn.execute("DELETE FROM specialites_mitm;")
            conn.execute("DELETE FROM has_avis_smr;")
            conn.execute("DELETE FROM sources_fraicheur;")
            conn.commit()
        corrupted_counts = inspect_schema_status(self.db_path)["table_counts"]
        self.assertEqual(corrupted_counts["specialites_mitm"], 0)
        self.assertEqual(corrupted_counts["sources_fraicheur"], 0)

        # Restaure depuis la sauvegarde -> retour exact aux 13 tables et 11 sources
        restored = restore_database(bkp["backup_path"], target_db_path=self.db_path)
        self.assertTrue(restored["integrity_ok"])
        after_restore_counts = inspect_schema_status(self.db_path)["table_counts"]
        self.assertEqual(after_restore_counts, expected_counts)


if __name__ == "__main__":
    unittest.main()
