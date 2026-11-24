"""Suite de tests dédiée au Lot 2B (pagination, filtres combinables, substances SA,
groupes génériques princeps/génériques, catalogue A–Z et conservation du contexte).

Ces tests utilisent un jeu synthétique isolé et ne modifient aucun des 17 tests existants.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from urllib.parse import unquote

from fastapi.testclient import TestClient

from app.importer.bdpm_importer import import_bdpm_directory
from app.main import create_app
from app.search.service import (
    get_generic_group_detail,
    get_substance_detail,
    list_active_substances,
    list_catalogue_medications,
    search_medications,
)


def _create_lot2b_synthetic_bdpm_files(target_dir: Path) -> None:
    """Crée un jeu synthétique riche permettant de tester tous les cas limites du Lot 2B :
    - 7 spécialités (lettres A, B, D) avec commercialisées et non commercialisées ;
    - distinction princeps (0), générique (1), générique multi-groupes/multi-rôles (1 et 2), hors groupe ;
    - conditions CIS_CPD soumises à prescription (liste I, prescription hospitalière) VS
      conditions NON soumises à prescription (usage hospitalier seul, instructions autorités) ;
    - une substance active SA (10001) avec deux orthographes officielles différentes ;
    - une fraction thérapeutique FT uniquement (99999) qui ne doit PAS figurer dans l'annuaire SA ;
    - une spécialité multi-substances (91000005) pour vérifier l'absence totale de doublons.
    """
    target_dir.mkdir(parents=True, exist_ok=True)

    cis_lines = [
        # 91000001 : Princeps commercialisé, comprimé, sans ordonnance, sans info ANSM
        "91000001\tALPHADOL 1000 mg, comprimé\tcomprimé\torale\tAutorisation active\tProcédure nationale\tCommercialisée\t01/01/2020\t\t\tLABO ONE\tNon",
        # 91000002 : Générique commercialisé (groupe 801 rôle 1 + groupe 802 rôle 2 -> multi-groupes!), comprimé, liste I, info ANSM
        "91000002\tALPHADOL GENERIQUE 1000 mg, comprimé\tcomprimé\torale\tAutorisation active\tProcédure nationale\tCommercialisée\t01/02/2020\t\t\tLABO GEN\tNon",
        # 91000003 : Générique NON commercialisé (groupe 801 rôle 1), comprimé, liste I
        "91000003\tALPHADOL ANCIEN 1000 mg, comprimé\tcomprimé\torale\tAutorisation active\tProcédure nationale\tNon commercialisée\t01/03/2018\t\t\tLABO OLD\tNon",
        # 91000004 : Hors groupe générique, commercialisé, gélule, CIS_CPD = 'réservé à l'usage HOSPITALIER' SEULEMENT (pas de prescription!)
        "91000004\tALPHAHOSP 500 mg, gélule\tgélule\torale\tAutorisation active\tProcédure nationale\tCommercialisée\t01/04/2021\t\t\tLABO HOSP\tNon",
        # 91000005 : Spécialité multi-substances (2 SA + 1 FT), commercialisée, comprimé, prescription hospitalière, info ANSM
        "91000005\tBETACOMBO 500 mg/250 mg, comprimé\tcomprimé\torale\tAutorisation active\tProcédure nationale\tCommercialisée\t01/05/2022\t\t\tLABO COMBO\tOui",
        # 91000006 : Hors groupe, commercialisé, comprimé, CIS_CPD = 'à ne prendre que sur instructions des autorités compétentes' (pas de prescription!)
        "91000006\tDELTAIODE 65 mg, comprimé\tcomprimé\torale\tAutorisation active\tProcédure nationale\tCommercialisée\t01/06/2021\t\t\tLABO PUB\tNon",
        # 91000007 : Princeps commercialisé, solution buvable, stupéfiants
        "91000007\tALPHASOL 10 mg/mL, solution buvable\tsolution buvable\torale\tAutorisation active\tProcédure nationale\tCommercialisée\t01/07/2023\t\t\tLABO SOL\tNon",
    ]
    (target_dir / "CIS_bdpm.txt").write_bytes(
        ("\r\n".join(cis_lines) + "\r\n").encode("cp1252")
    )

    compo_lines = [
        # Code 10001 : même code SA avec 2 orthographes officielles différentes
        "91000001\tcomprimé\t10001\tPARACÉTAMOL FICTIF\t1000 mg\tun comprimé\tSA\t1",
        "91000002\tcomprimé\t10001\tPARACÉTAMOL FICTIF\t1000 mg\tun comprimé\tSA\t1",
        "91000003\tcomprimé\t10001\tFICTIF (PARACÉTAMOL)\t1000 mg\tun comprimé\tSA\t1",
        "91000004\tgélule\t10002\tSUBSTANCE HOSPITALE FICTIVE\t500 mg\tune gélule\tSA\t1",
        # 91000005 : 2 SA + 1 FT (code 99999 uniquement FT!)
        "91000005\tcomprimé\t10001\tPARACÉTAMOL FICTIF\t500 mg\tun comprimé\tSA\t1",
        "91000005\tcomprimé\t10003\tCODÉINE FICTIVE\t250 mg\tun comprimé\tSA\t2",
        "91000005\tcomprimé\t99999\tFRACTION THERAPEUTIQUE SEULE\t200 mg\tun comprimé\tFT\t2",
        "91000006\tcomprimé\t10004\tIODURE FICTIF\t65 mg\tun comprimé\tSA\t1",
        "91000007\tsolution\t10005\tMORPHINE FICTIVE\t10 mg\t1 mL\tSA\t1",
    ]
    (target_dir / "CIS_COMPO_bdpm.txt").write_bytes(
        ("\r\n".join(compo_lines) + "\r\n").encode("cp1252")
    )

    cip_lines = [
        "91000001\t3100001\tboîte de 8 comprimés\tPrésentation active\tDéclaration de commercialisation\t01/01/2020\t3400931000011\toui\t65%\t1,95\t2,97\t1,02\t",
        "91000002\t3100002\tboîte de 16 comprimés\tPrésentation active\tDéclaration de commercialisation\t01/02/2020\t3400931000028\toui\t65%\t2,10\t3,12\t1,02\t",
    ]
    (target_dir / "CIS_CIP_bdpm.txt").write_bytes(
        ("\n".join(cip_lines) + "\n").encode("utf-8")
    )

    cpd_lines = [
        "91000002\tliste I",
        "91000003\tliste I",
        # Condition NON soumise à prescription (identique aux cas réels identifiés dans BDPM)
        "91000004\tréservé à l'usage HOSPITALIER",
        "91000005\tprescription hospitalière",
        # Condition NON soumise à prescription
        "91000006\tà ne prendre que sur instructions des autorités compétentes",
        "91000007\tstupéfiants",
    ]
    (target_dir / "CIS_CPD_bdpm.txt").write_bytes(
        ("\r\n".join(cpd_lines) + "\r\n").encode("cp1252")
    )

    gener_lines = [
        # Groupe 801 : 1 princeps (91000001) + 2 génériques (91000002, 91000003)
        "801\tPARACÉTAMOL FICTIF 1000 mg - ALPHADOL 1000 mg\t91000001\t0\t1",
        "801\tPARACÉTAMOL FICTIF 1000 mg - ALPHADOL 1000 mg\t91000002\t1\t2",
        "801\tPARACÉTAMOL FICTIF 1000 mg - ALPHADOL 1000 mg\t91000003\t1\t3",
        # Groupe 802 : 91000002 apparaît AUSSI avec rôle 2 (multi-groupes / multi-rôles)
        "802\tPARACÉTAMOL FICTIF COMPLEMENTAIRE\t91000002\t2\t1",
        # Groupe 803 : princeps 91000007
        "803\tMORPHINE FICTIVE 10 mg/mL - ALPHASOL\t91000007\t0\t1",
    ]
    (target_dir / "CIS_GENER_bdpm.txt").write_bytes(
        ("\r\n".join(gener_lines) + "\r\n").encode("cp1252")
    )

    info_lines = [
        "91000002\t2026-01-01\t2027-01-01\t<a href='https://ansm.sante.fr/infos/point-fictif-1'>Point de situation fictif ANSM sur ALPHADOL</a>",
        "91000005\t2026-02-01\t2027-02-01\t<a href='https://ansm.sante.fr/infos/point-fictif-2'>Communication fictive ANSM sur BETACOMBO</a>",
    ]
    (target_dir / "CIS_InfoImportantes.txt").write_bytes(
        ("\r\n".join(info_lines) + "\r\n").encode("cp1252")
    )

    (target_dir / "metadata_telechargement.txt").write_text(
        "downloaded_at_utc=2026-11-24T12:00:00Z\nofficial_bdpm_date=29/09/2026\n",
        encoding="utf-8",
    )


class TestLot2BNavigationAndFilters(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.raw_dir = self.base_path / "raw"
        self.db_path = self.base_path / "lot2b_bdpm.sqlite3"
        _create_lot2b_synthetic_bdpm_files(self.raw_dir)
        import_bdpm_directory(raw_dir=self.raw_dir, db_path=self.db_path)
        self.app = create_app(db_path=self.db_path)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_search_pagination_total_count_and_out_of_range(self) -> None:
        # 5 spécialités commencent par ALPHA (91000001..04 et 91000007)
        p1 = search_medications("alpha", db_path=self.db_path, page=1, per_page=2)
        self.assertEqual(p1["status"], "ok")
        self.assertEqual(p1["total_count"], 5)
        self.assertEqual(p1["count"], 2)
        self.assertEqual(p1["pagination"]["total_pages"], 3)
        self.assertFalse(p1["pagination"]["has_prev"])
        self.assertTrue(p1["pagination"]["has_next"])
        self.assertFalse(p1["pagination"]["page_out_of_range"])

        p2 = search_medications("alpha", db_path=self.db_path, page=2, per_page=2)
        self.assertEqual(p2["count"], 2)
        self.assertTrue(p2["pagination"]["has_prev"])
        self.assertTrue(p2["pagination"]["has_next"])

        p3 = search_medications("alpha", db_path=self.db_path, page=3, per_page=2)
        self.assertEqual(p3["count"], 1)
        self.assertFalse(p3["pagination"]["has_next"])

        # Aucune spécialité en double entre les pages
        all_cis = (
            [r["code_cis"] for r in p1["results"]]
            + [r["code_cis"] for r in p2["results"]]
            + [r["code_cis"] for r in p3["results"]]
        )
        self.assertEqual(len(all_cis), 5)
        self.assertEqual(len(set(all_cis)), 5)

        # Comportement cohérent pour une page inexistante (page=99)
        p99 = search_medications("alpha", db_path=self.db_path, page=99, per_page=2)
        self.assertEqual(p99["status"], "page_out_of_range")
        self.assertEqual(p99["count"], 0)
        self.assertEqual(p99["total_count"], 5)
        self.assertEqual(p99["results"], [])
        self.assertTrue(p99["pagination"]["page_out_of_range"])
        self.assertEqual(p99["pagination"]["prev_page"], 3)

        # Vérification côté page HTML
        web_p99 = self.client.get("/", params={"q": "alpha", "per_page": "5", "page": "99"})
        self.assertEqual(web_p99.status_code, 200)
        self.assertIn("Page demandée inexistante", web_p99.text)

    def test_generic_role_distinction_and_no_duplicates_across_multiple_groups(self) -> None:
        in_group = search_medications("", db_path=self.db_path, generic_role="in_group")
        self.assertEqual(in_group["status"], "ok")
        self.assertEqual(in_group["total_count"], 4)
        cis_in_group = [r["code_cis"] for r in in_group["results"]]
        # 91000002 est dans 2 groupes (801 rôle 1 et 802 rôle 2) : doit apparaître 1 seule fois
        self.assertEqual(cis_in_group.count("91000002"), 1)
        self.assertEqual(set(cis_in_group), {"91000001", "91000002", "91000003", "91000007"})

        princeps_only = search_medications("", db_path=self.db_path, generic_role="princeps")
        self.assertEqual(princeps_only["total_count"], 2)
        self.assertEqual(
            {r["code_cis"] for r in princeps_only["results"]},
            {"91000001", "91000007"},
        )
        for item in princeps_only["results"]:
            self.assertEqual(item["generic_status"], "princeps")

        generic_only = search_medications("", db_path=self.db_path, generic_role="generic")
        self.assertEqual(generic_only["total_count"], 2)
        cis_generics = [r["code_cis"] for r in generic_only["results"]]
        self.assertEqual(cis_generics.count("91000002"), 1)
        self.assertEqual(set(cis_generics), {"91000002", "91000003"})
        for item in generic_only["results"]:
            self.assertEqual(item["generic_status"], "generic")

    def test_prescription_filter_based_on_real_cpd_labels(self) -> None:
        res = search_medications("", db_path=self.db_path, prescription_only=True)
        cis_set = {r["code_cis"] for r in res["results"]}
        # Doivent être inclus : liste I (91000002, 91000003), prescription hospitalière (91000005), stupéfiants (91000007)
        self.assertEqual(cis_set, {"91000002", "91000003", "91000005", "91000007"})
        # Ne doivent PAS être inclus bien qu'ayant une ligne dans CIS_CPD :
        # 91000004 (réservé usage HOSPITALIER seul) et 91000006 (instructions autorités)
        self.assertNotIn("91000004", cis_set)
        self.assertNotIn("91000006", cis_set)

    def test_combined_filters_and_empty_combinations(self) -> None:
        # Combinaison : commercialisé + comprimé + générique + sur prescription + infos ANSM disponibles
        combo = search_medications(
            "alphadol",
            db_path=self.db_path,
            commercialise_only=True,
            forme="comprimé",
            generic_role="generic",
            prescription_only=True,
            with_ansm_info=True,
        )
        self.assertEqual(combo["status"], "ok")
        self.assertEqual(combo["total_count"], 1)
        self.assertEqual(combo["results"][0]["code_cis"], "91000002")

        # Combinaison contradictoire (princeps + infos ANSM disponibles -> 0 résultat)
        empty_combo = search_medications(
            "alphadol",
            db_path=self.db_path,
            generic_role="princeps",
            with_ansm_info=True,
        )
        self.assertEqual(empty_combo["status"], "no_results")
        self.assertEqual(empty_combo["total_count"], 0)
        self.assertEqual(empty_combo["results"], [])

    def test_multi_substance_medication_never_duplicated(self) -> None:
        # 91000005 possède 2 SA + 1 FT
        res = search_medications("betacombo", db_path=self.db_path, with_ansm_info=True)
        self.assertEqual(res["total_count"], 1)
        self.assertEqual(len(res["results"]), 1)
        self.assertEqual(res["results"][0]["code_cis"], "91000005")
        self.assertEqual(len(res["results"][0]["compositions"]), 3)

    def test_ansm_terminology_in_html_pages(self) -> None:
        home = self.client.get("/", params={"q": "alphadol", "with_ansm_info": "true"})
        self.assertEqual(home.status_code, 200)
        self.assertIn("Informations ANSM disponibles", home.text)
        self.assertIn("info(s) ANSM disponible(s)", home.text)

        detail = self.client.get("/medicaments/91000002")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("Informations ANSM disponibles", detail.text)
        self.assertIn("Point de situation fictif ANSM sur ALPHADOL", detail.text)

    def test_active_substances_only_sa_and_grouped_aliases(self) -> None:
        listing = list_active_substances(db_path=self.db_path, page=1, per_page=10)
        self.assertEqual(listing["status"], "ok")
        codes = [s["code_substance"] for s in listing["results"]]
        # Exactement 5 codes SA distincts (10001..10005) ; 99999 (FT uniquement) est exclu !
        self.assertEqual(listing["total_count"], 5)
        self.assertIn("10001", codes)
        self.assertNotIn("99999", codes)

        # Le code 10001 regroupe ses deux orthographes officielles sur une seule entrée
        sub_10001 = next(s for s in listing["results"] if s["code_substance"] == "10001")
        self.assertEqual(sub_10001["denomination_principale"], "PARACÉTAMOL FICTIF")
        self.assertIn("FICTIF (PARACÉTAMOL)", sub_10001["autres_denominations"])
        self.assertEqual(sub_10001["specialites_count"], 4)
        self.assertEqual(sub_10001["commercialisees_count"], 3)

        # Page détaillée de la substance 10001 (sans doublon et paginée)
        detail = get_substance_detail("10001", db_path=self.db_path, page=1, per_page=2)
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail["total_count"], 4)
        self.assertEqual(detail["pagination"]["total_pages"], 2)
        self.assertIn("FICTIF (PARACÉTAMOL)", detail["autres_denominations"])

        # Code FT uniquement (99999) ou inconnu -> None / HTTP 404
        self.assertIsNone(get_substance_detail("99999", db_path=self.db_path))
        self.assertEqual(self.client.get("/substances/99999").status_code, 404)
        self.assertEqual(self.client.get("/api/substances/99999").status_code, 404)
        self.assertEqual(self.client.get("/substances/INCONNU").status_code, 404)

        # Page hors plage sur substance
        sub_p99 = self.client.get("/substances/10001", params={"page": "99", "per_page": "5"})
        self.assertEqual(sub_p99.status_code, 200)
        self.assertIn("Page demandée inexistante", sub_p99.text)

    def test_generic_group_page_separates_princeps_and_generics(self) -> None:
        grp = get_generic_group_detail("801", db_path=self.db_path)
        self.assertIsNotNone(grp)
        assert grp is not None
        self.assertEqual(grp["princeps_count"], 1)
        self.assertEqual(grp["generics_count"], 2)
        self.assertEqual(grp["princeps"][0]["code_cis"], "91000001")
        self.assertEqual({g["code_cis"] for g in grp["generics"]}, {"91000002", "91000003"})

        web_grp = self.client.get("/generiques/801")
        self.assertEqual(web_grp.status_code, 200)
        self.assertIn("ALPHADOL 1000 mg, comprimé", web_grp.text)
        self.assertIn("Spécialité(s) de référence (Princeps)", web_grp.text)
        self.assertIn("Médicaments génériques du groupe", web_grp.text)

        self.assertEqual(self.client.get("/generiques/999999").status_code, 404)
        self.assertEqual(self.client.get("/api/generics/999999").status_code, 404)

    def test_alphabetical_catalogue_pagination_and_empty_letter(self) -> None:
        # Pagination par défaut à 20 spécialités par page
        cat_default = list_catalogue_medications(db_path=self.db_path, letter="A")
        self.assertEqual(cat_default["pagination"]["per_page"], 20)

        cat_a = list_catalogue_medications(db_path=self.db_path, letter="A", page=1, per_page=2)
        self.assertEqual(cat_a["status"], "ok")
        self.assertEqual(cat_a["total_count"], 5)
        self.assertEqual(cat_a["pagination"]["total_pages"], 3)
        self.assertEqual(cat_a["count"], 2)

        cat_a_comm = list_catalogue_medications(
            db_path=self.db_path, letter="A", page=1, per_page=10, commercialise_only=True
        )
        self.assertEqual(cat_a_comm["total_count"], 4)

        # Lettre vide (même avec page=50) : aucune erreur serveur et message explicite
        cat_z = list_catalogue_medications(db_path=self.db_path, letter="Z", page=50, per_page=10)
        self.assertEqual(cat_z["status"], "no_results")
        self.assertEqual(cat_z["total_count"], 0)
        web_z = self.client.get("/catalogue", params={"lettre": "Z", "page": "50"})
        self.assertEqual(web_z.status_code, 200)
        self.assertIn("Aucune spécialité trouvée", web_z.text)

        # Une page inexistante (50) est ramenée à la dernière page disponible (3)
        cat_out = list_catalogue_medications(db_path=self.db_path, letter="A", page=50, per_page=2)
        self.assertEqual(cat_out["status"], "ok")
        self.assertEqual(cat_out["pagination"]["page"], 3)
        self.assertEqual(cat_out["pagination"]["requested_page"], 50)
        self.assertTrue(cat_out["pagination"]["page_clamped"])
        self.assertEqual(len(cat_out["results"]), 1)

        # Côté Web : redirection propre vers la dernière page disponible en conservant les filtres
        web_clamped = self.client.get(
            "/catalogue",
            params={"lettre": "A", "q": "alphadol", "commercialise_only": "true", "per_page": "1", "page": "50"},
            follow_redirects=False,
        )
        self.assertEqual(web_clamped.status_code, 302)
        loc = unquote(web_clamped.headers["location"])
        self.assertIn("lettre=A", loc)
        self.assertIn("q=alphadol", loc)
        self.assertIn("commercialise_only=true", loc)
        self.assertIn("page=2", loc)

        web_cat = self.client.get("/catalogue", params={"lettre": "A", "commercialise_only": "true"})
        self.assertEqual(web_cat.status_code, 200)
        self.assertIn("Catalogue alphabétique", web_cat.text)
        self.assertIn("ALPHADOL 1000 mg", web_cat.text)

    def test_context_preservation_return_to_links(self) -> None:
        resp = self.client.get(
            "/",
            params={
                "q": "alphadol",
                "commercialise_only": "true",
                "generic_role": "generic",
                "with_ansm_info": "true",
            },
        )
        self.assertEqual(resp.status_code, 200)
        decoded_html = unquote(resp.text)
        self.assertIn("/medicaments/91000002?return_to=", decoded_html)
        self.assertIn("commercialise_only=true", decoded_html)
        self.assertIn("generic_role=generic", decoded_html)
        self.assertIn("with_ansm_info=true", decoded_html)

        # Depuis le catalogue A–Z
        cat_resp = self.client.get("/catalogue", params={"lettre": "B"})
        self.assertEqual(cat_resp.status_code, 200)
        self.assertIn("/medicaments/91000005?return_to=", unquote(cat_resp.text))
        self.assertIn("lettre=B", unquote(cat_resp.text))

        # Depuis une fiche substance
        sub_resp = self.client.get("/substances/10001")
        self.assertEqual(sub_resp.status_code, 200)
        self.assertIn("/medicaments/91000001?return_to=", unquote(sub_resp.text))
        self.assertIn("/substances/10001", unquote(sub_resp.text))

    def test_sql_parameter_safety_in_filters_and_api(self) -> None:
        # Vérifie que des entrées malveillantes sont traitées comme simples paramètres liés (?)
        api_resp = self.client.get(
            "/api/search",
            params={
                "q": "'; DROP TABLE specialites; --",
                "forme": "' OR 1=1 --",
                "generic_role": "princeps' OR '1'='1",
            },
        )
        self.assertEqual(api_resp.status_code, 200)
        payload = api_resp.json()
        self.assertEqual(payload["status"], "no_results")
        self.assertEqual(payload["total_count"], 0)
        # La base est toujours parfaitement intacte
        health = self.client.get("/api/health").json()
        self.assertEqual(health["specialites_count"], 7)

    def test_dynamic_counts_distinguish_total_and_commercialisees(self) -> None:
        # 7 spécialités en base dont 6 commercialisées (91000003 est Non commercialisée)
        health = self.client.get("/api/health").json()
        self.assertEqual(health["provenance"]["specialites_count"], "7")
        self.assertEqual(health["provenance"]["commercialisees_count"], "6")

        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn("Aperçu des spécialités commercialisées", home.text)
        self.assertIn(">6</span> commercialisées sur <span class=\"mono\">7</span> spécialités référencées", home.text)

        filtered = self.client.get("/", params={"q": "alphadol", "commercialise_only": "true"})
        self.assertEqual(filtered.status_code, 200)
        self.assertIn("spécialités commercialisées", filtered.text)
        self.assertIn("Filtre actif : commercialisées uniquement", filtered.text)

    def test_user_friendly_filter_labels_and_ansm_wording(self) -> None:
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn("Type de spécialité", home.text)
        self.assertIn("Appartenant à un groupe générique", home.text)
        self.assertIn("Génériques uniquement", home.text)
        self.assertIn("Princeps uniquement", home.text)
        self.assertIn("Médicaments soumis à prescription", home.text)
        self.assertIn("Informations ANSM disponibles", home.text)
        self.assertNotIn("Rôle groupe générique", home.text)
        self.assertNotIn("Soumis à prescription (<code>CIS_CPD</code>)", home.text)
        self.assertNotIn("Alertes ANSM", home.text)

    def test_catalogue_internal_search_confined_accents_special_chars_and_counts(self) -> None:
        # 1. Confinement strict à la lettre sélectionnée : BETACOMBO (lettre B) ne doit PAS sortir sous la lettre A
        cross = list_catalogue_medications(db_path=self.db_path, letter="A", query="betacombo")
        self.assertEqual(cross["status"], "no_results")
        self.assertEqual(cross["total_count"], 0)

        # 2. Accents, majuscules et espaces superflus + confinement (ne renvoie pas BETACOMBO malgré même substance)
        accented = list_catalogue_medications(
            db_path=self.db_path, letter="A", query="  PARACÉTAMOL   fictif  "
        )
        self.assertEqual(accented["status"], "ok")
        self.assertEqual(accented["total_count"], 3)
        self.assertEqual(
            {r["code_cis"] for r in accented["results"]},
            {"91000001", "91000002", "91000003"},
        )
        # Les compteurs généraux A–Z ne sont PAS faussés par la recherche interne
        count_a = next(e["count"] for e in accented["letters"] if e["letter"] == "A")
        self.assertEqual(count_a, 5)

        # Avec commercialise_only=True, les compteurs A–Z reflètent uniquement les commercialisées
        comm_search = list_catalogue_medications(
            db_path=self.db_path, letter="A", query="alphahosp", commercialise_only=True
        )
        self.assertEqual(comm_search["total_count"], 1)
        count_a_comm = next(e["count"] for e in comm_search["letters"] if e["letter"] == "A")
        self.assertEqual(count_a_comm, 4)

        # 3. Protection contre les jokers SQL LIKE ('%' et '_')
        for special_q in ("%", "%%%", "_", "___", "alpha_dol"):
            res = list_catalogue_medications(db_path=self.db_path, letter="A", query=special_q)
            self.assertEqual(res["total_count"], 0, f"Le joker {special_q!r} ne doit pas tout matcher")

    def test_catalogue_api_endpoint_dosages_and_no_duplicates(self) -> None:
        # Route API /api/catalogue par défaut (per_page=20, lettre A)
        resp_default = self.client.get("/api/catalogue")
        self.assertEqual(resp_default.status_code, 200)
        data = resp_default.json()
        self.assertEqual(data["letter"], "A")
        self.assertEqual(data["pagination"]["per_page"], 20)
        self.assertEqual(data["total_count"], 5)

        # Lettre B : spécialité multi-substances (91000005 : 2 SA + 1 FT) -> zéro doublon, dosages SA distincts
        resp_b = self.client.get("/api/catalogue", params={"lettre": "B"})
        self.assertEqual(resp_b.status_code, 200)
        data_b = resp_b.json()
        self.assertEqual(data_b["total_count"], 1)
        self.assertEqual(len(data_b["results"]), 1)
        item = data_b["results"][0]
        self.assertEqual(item["code_cis"], "91000005")
        # Conserve exactement les deux dosages officiels SA associés à leur substance (sans inclure la FT 200 mg)
        self.assertEqual(
            item["official_sa_dosages"],
            ["PARACÉTAMOL FICTIF : 500 mg", "CODÉINE FICTIVE : 250 mg"],
        )
        self.assertEqual(
            [(e["code_substance"], e["substance"], e["dosage"]) for e in item["official_sa_dosage_entries"]],
            [
                ("10001", "PARACÉTAMOL FICTIF", "500 mg"),
                ("10003", "CODÉINE FICTIVE", "250 mg"),
            ],
        )

        # Recadrage vers la dernière page via /api/catalogue
        resp_clamped = self.client.get(
            "/api/catalogue",
            params={"lettre": "A", "q": "alphadol", "per_page": 2, "page": 99},
        )
        self.assertEqual(resp_clamped.status_code, 200)
        clamped_json = resp_clamped.json()
        self.assertEqual(clamped_json["status"], "ok")
        self.assertEqual(clamped_json["pagination"]["page"], 2)
        self.assertTrue(clamped_json["pagination"]["page_clamped"])
        self.assertEqual(len(clamped_json["results"]), 1)

        # Lettre vide via /api/catalogue : aucune erreur serveur
        resp_empty = self.client.get("/api/catalogue", params={"lettre": "Z", "page": 10})
        self.assertEqual(resp_empty.status_code, 200)
        self.assertEqual(resp_empty.json()["status"], "no_results")
        self.assertEqual(resp_empty.json()["results"], [])

    def test_catalogue_context_preservation_with_query_and_page(self) -> None:
        resp = self.client.get(
            "/catalogue",
            params={
                "lettre": "A",
                "q": "alphadol",
                "commercialise_only": "true",
                "per_page": "5",
                "page": "1",
            },
        )
        self.assertEqual(resp.status_code, 200)
        decoded = unquote(resp.text)
        self.assertIn("/medicaments/91000001?return_to=", decoded)
        self.assertIn("lettre=A", decoded)
        self.assertIn("q=alphadol", decoded)
        self.assertIn("commercialise_only=true", decoded)


def _create_dosage_edge_cases_bdpm_files(target_dir: Path) -> None:
    """Jeu synthétique dédié aux règles strictes d'affichage des dosages SA dans le catalogue :
    - 92000001 (lettre M) : 1 seule substance active avec dosage officiel simple (500 mg) ;
    - 92000002 (lettre D) : 2 SA complètes + 1 FT (ne doit jamais mélanger la FT) ;
    - 92000003 (lettre T) : 3 SA complètes ;
    - 92000004 (lettre P) : 2 SA dont 1 sel sans dosage SA (dosage uniquement sur la FT) -> résumé masqué ;
    - 92000005 (lettre Q) : 4 SA complètes (> 3 substances) -> résumé masqué ;
    - 92000006 (lettre I) : 2 SA dont l'ordre en base SQLite (Y 300 mg puis X 600 mg) diffère du nom commercial (X/Y 600 mg/300 mg) ;
    - 92000007 (lettre J) : 2 éléments pharmaceutiques distincts (comprimé jour / comprimé nuit) -> résumé masqué ;
    - 92000008 (lettre R) : 2 SA avec références de dosage différentes (dose délivrée vs dose mesurée) -> résumé masqué.
    """
    target_dir.mkdir(parents=True, exist_ok=True)

    cis_lines = [
        "92000001\tMONODOSE 500 mg, comprimé\tcomprimé\torale\tAutorisation active\tProcédure nationale\tCommercialisée\t01/01/2022\t\t\tLABO M\tNon",
        "92000002\tDUOCARD 10 mg/5 mg, comprimé\tcomprimé\torale\tAutorisation active\tProcédure nationale\tCommercialisée\t01/01/2022\t\t\tLABO D\tNon",
        "92000003\tTRICOMBO, comprimé à croquer\tcomprimé à croquer\torale\tAutorisation active\tProcédure nationale\tCommercialisée\t01/01/2022\t\t\tLABO T\tNon",
        "92000004\tPARTIALMIX 600 mg/300 mg, comprimé pelliculé\tcomprimé pelliculé\torale\tAutorisation active\tProcédure nationale\tCommercialisée\t01/01/2022\t\t\tLABO P\tNon",
        "92000005\tQUADRIPLUS, solution buvable\tsolution buvable\torale\tAutorisation active\tProcédure nationale\tCommercialisée\t01/01/2022\t\t\tLABO Q\tNon",
        "92000006\tINVERSECOMBO X/Y 600 mg/300 mg, comprimé pelliculé\tcomprimé pelliculé\torale\tAutorisation active\tProcédure nationale\tCommercialisée\t01/01/2022\t\t\tLABO I\tNon",
        "92000007\tJOURNUIT, comprimé\tcomprimé\torale\tAutorisation active\tProcédure nationale\tCommercialisée\t01/01/2022\t\t\tLABO J\tNon",
        "92000008\tREFDIFF 100/6 microgrammes, solution pour inhalation\tsolution pour inhalation\tinhalée\tAutorisation active\tProcédure nationale\tCommercialisée\t01/01/2022\t\t\tLABO R\tNon",
    ]
    (target_dir / "CIS_bdpm.txt").write_bytes(("\r\n".join(cis_lines) + "\r\n").encode("cp1252"))

    compo_lines = [
        # 1 substance active
        "92000001\tcomprimé\t20000\tSUBSTANCE UNIQUE\t500 mg\tun comprimé\tSA\t1",
        # 2 substances actives + 1 FT
        "92000002\tcomprimé\t20001\tSUBSTANCE ALPHA\t10 mg\tun comprimé\tSA\t1",
        "92000002\tcomprimé\t20002\tSUBSTANCE BETA\t5 mg\tun comprimé\tSA\t2",
        "92000002\tcomprimé\t29999\tFRACTION EXCLUE\t99 mg\tun comprimé\tFT\t2",
        # 3 substances actives
        "92000003\tcomprimé\t20003\tCARBONATE FICTIF\t80 mg\tun comprimé\tSA\t1",
        "92000003\tcomprimé\t20004\tBICARBONATE FICTIF\t133,5 mg\tun comprimé\tSA\t2",
        "92000003\tcomprimé\t20005\tALGINATE FICTIF\t250 mg\tun comprimé\tSA\t3",
        # 2 SA dont 1 sans dosage SA (dosage uniquement porté par FT)
        "92000004\tcomprimé\t20006\tLAMIVUDINE FICTIVE\t300 mg\tun comprimé\tSA\t1",
        "92000004\tcomprimé\t20007\tSULFATE D'ABACAVIR FICTIF\t\t\tSA\t2",
        "92000004\tcomprimé\t20008\tABACAVIR FICTIF\t600 mg\tun comprimé\tFT\t2",
        # 4 substances actives (> 3)
        "92000005\tsolution\t20010\tACIDE UN\t10 mg\t100 ml\tSA\t1",
        "92000005\tsolution\t20011\tACIDE DEUX\t20 mg\t100 ml\tSA\t2",
        "92000005\tsolution\t20012\tACIDE TROIS\t30 mg\t100 ml\tSA\t3",
        "92000005\tsolution\t20013\tACIDE QUATRE\t40 mg\t100 ml\tSA\t4",
        # Ordre des lignes SQLite (Y=300 mg puis X=600 mg) opposé au nom commercial (X/Y 600 mg/300 mg)
        "92000006\tcomprimé\t20020\tSUBSTANCE Y\t300 mg\tun comprimé\tSA\t2",
        "92000006\tcomprimé\t20021\tSUBSTANCE X\t600 mg\tun comprimé\tSA\t1",
        # Multi-éléments (comprimé jour + comprimé nuit)
        "92000007\tcomprimé jour\t20030\tPARACÉTAMOL FICTIF\t500 mg\tun comprimé\tSA\t1",
        "92000007\tcomprimé nuit\t20031\tDOXYLAMINE FICTIVE\t7,5 mg\tun comprimé\tSA\t2",
        # Références de dosage différentes (dose délivrée vs dose mesurée)
        "92000008\tsolution\t20040\tSUBSTANCE INHALEE A\t100 microgrammes\tune dose délivrée\tSA\t1",
        "92000008\tsolution\t20041\tSUBSTANCE INHALEE B\t6 microgrammes\tune dose mesurée\tSA\t2",
    ]
    (target_dir / "CIS_COMPO_bdpm.txt").write_bytes(("\r\n".join(compo_lines) + "\r\n").encode("cp1252"))
    (target_dir / "CIS_CIP_bdpm.txt").write_bytes(b"")
    (target_dir / "CIS_CPD_bdpm.txt").write_bytes(b"")
    (target_dir / "CIS_GENER_bdpm.txt").write_bytes(b"")
    (target_dir / "CIS_InfoImportantes.txt").write_bytes(b"")
    (target_dir / "metadata_telechargement.txt").write_text(
        "downloaded_at_utc=2026-11-24T12:00:00Z\nofficial_bdpm_date=29/09/2026\n",
        encoding="utf-8",
    )


class TestCatalogueMultiSubstanceDosages(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.raw_dir = self.base_path / "raw"
        self.db_path = self.base_path / "dosages_bdpm.sqlite3"
        _create_dosage_edge_cases_bdpm_files(self.raw_dir)
        import_bdpm_directory(raw_dir=self.raw_dir, db_path=self.db_path)
        self.app = create_app(db_path=self.db_path)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_single_substance_displays_simple_dosage(self) -> None:
        api = self.client.get("/api/catalogue", params={"lettre": "M"}).json()
        self.assertEqual(api["total_count"], 1)
        item = api["results"][0]
        self.assertEqual(item["official_sa_dosages"], ["500 mg"])
        self.assertEqual(len(item["official_sa_dosage_entries"]), 1)
        self.assertEqual(item["official_sa_dosage_entries"][0]["code_substance"], "20000")

        html = self.client.get("/catalogue", params={"lettre": "M"}).text
        self.assertIn('<span class="catalogue-dosages"><span class="mono">500 mg</span></span>', html)
        self.assertNotIn("SUBSTANCE UNIQUE :", html)

    def test_two_substances_display_substance_dosage_pairs(self) -> None:
        api = self.client.get("/api/catalogue", params={"lettre": "D"}).json()
        item = api["results"][0]
        self.assertEqual(
            item["official_sa_dosages"],
            ["SUBSTANCE ALPHA : 10 mg", "SUBSTANCE BETA : 5 mg"],
        )
        self.assertEqual(
            [(e["code_substance"], e["dosage"]) for e in item["official_sa_dosage_entries"]],
            [("20001", "10 mg"), ("20002", "5 mg")],
        )

        html = self.client.get("/catalogue", params={"lettre": "D"}).text
        self.assertIn("SUBSTANCE ALPHA", html)
        self.assertIn("10 mg", html)
        self.assertIn("SUBSTANCE BETA", html)
        self.assertIn("5 mg", html)
        self.assertNotIn("FRACTION EXCLUE", html)
        self.assertNotIn("99 mg", html)

    def test_three_substances_display_all_three_pairs(self) -> None:
        api = self.client.get("/api/catalogue", params={"lettre": "T"}).json()
        item = api["results"][0]
        self.assertEqual(
            item["official_sa_dosages"],
            [
                "CARBONATE FICTIF : 80 mg",
                "BICARBONATE FICTIF : 133,5 mg",
                "ALGINATE FICTIF : 250 mg",
            ],
        )

        html = self.client.get("/catalogue", params={"lettre": "T"}).text
        self.assertIn("CARBONATE FICTIF", html)
        self.assertIn("80 mg", html)
        self.assertIn("BICARBONATE FICTIF", html)
        self.assertIn("133,5 mg", html)
        self.assertIn("ALGINATE FICTIF", html)
        self.assertIn("250 mg", html)

    def test_multi_substance_with_missing_sa_dosage_hides_partial_summary(self) -> None:
        api = self.client.get("/api/catalogue", params={"lettre": "P"}).json()
        item = api["results"][0]
        # Aucune info partielle ni déduction depuis la FT 600 mg
        self.assertEqual(item["official_sa_dosages"], [])
        self.assertEqual(item["official_sa_dosage_entries"], [])

        cat_html = self.client.get("/catalogue", params={"lettre": "P"}).text
        self.assertIn("PARTIALMIX 600 mg/300 mg", cat_html)
        self.assertNotIn('class="catalogue-dosages"', cat_html)
        self.assertNotIn("LAMIVUDINE FICTIVE", cat_html)

        # La fiche détaillée continue bien d'exposer toute la composition officielle (SA + FT)
        detail_html = self.client.get("/medicaments/92000004").text
        self.assertIn("LAMIVUDINE FICTIVE", detail_html)
        self.assertIn("SULFATE D&#39;ABACAVIR FICTIF", detail_html)
        self.assertIn("ABACAVIR FICTIF", detail_html)
        self.assertIn("600 mg", detail_html)
        self.assertIn("300 mg", detail_html)

    def test_more_than_three_substances_hides_catalogue_summary(self) -> None:
        api = self.client.get("/api/catalogue", params={"lettre": "Q"}).json()
        item = api["results"][0]
        self.assertEqual(item["official_sa_dosages"], [])
        self.assertEqual(item["official_sa_dosage_entries"], [])

        cat_html = self.client.get("/catalogue", params={"lettre": "Q"}).text
        self.assertIn("QUADRIPLUS", cat_html)
        self.assertNotIn('class="catalogue-dosages"', cat_html)
        self.assertNotIn("ACIDE UN", cat_html)

        detail_html = self.client.get("/medicaments/92000005").text
        for name in ("ACIDE UN", "ACIDE DEUX", "ACIDE TROIS", "ACIDE QUATRE"):
            self.assertIn(name, detail_html)

    def test_order_differing_from_commercial_name_keeps_exact_substance_mapping(self) -> None:
        api = self.client.get("/api/catalogue", params={"lettre": "I"}).json()
        item = api["results"][0]
        # Même si le nom commercial dit "X/Y 600 mg/300 mg", chaque substance garde son dosage exact en base
        self.assertEqual(
            item["official_sa_dosages"],
            ["SUBSTANCE Y : 300 mg", "SUBSTANCE X : 600 mg"],
        )
        self.assertEqual(
            [(e["code_substance"], e["substance"], e["dosage"]) for e in item["official_sa_dosage_entries"]],
            [("20020", "SUBSTANCE Y", "300 mg"), ("20021", "SUBSTANCE X", "600 mg")],
        )
        # Vérifie explicitement qu'aucune inversion de dosage n'a été produite
        self.assertNotIn("SUBSTANCE Y : 600 mg", item["official_sa_dosages"])
        self.assertNotIn("SUBSTANCE X : 300 mg", item["official_sa_dosages"])

        cat_html = self.client.get("/catalogue", params={"lettre": "I"}).text
        self.assertIn('<span class="catalogue-dosage-substance">SUBSTANCE Y</span> : <span class="mono">300 mg</span>', cat_html)
        self.assertIn('<span class="catalogue-dosage-substance">SUBSTANCE X</span> : <span class="mono">600 mg</span>', cat_html)

    def test_ambiguous_multi_element_and_differing_references_hide_summary(self) -> None:
        api_j = self.client.get("/api/catalogue", params={"lettre": "J"}).json()
        self.assertEqual(api_j["results"][0]["official_sa_dosages"], [])
        self.assertNotIn('class="catalogue-dosages"', self.client.get("/catalogue", params={"lettre": "J"}).text)

        api_r = self.client.get("/api/catalogue", params={"lettre": "R"}).json()
        self.assertEqual(api_r["results"][0]["official_sa_dosages"], [])
        self.assertNotIn('class="catalogue-dosages"', self.client.get("/catalogue", params={"lettre": "R"}).text)


if __name__ == "__main__":
    unittest.main()
