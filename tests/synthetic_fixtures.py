"""Jeux de données synthétiques de test (strictement séparés des données réelles BDPM).

Ces données utilisent des noms fictifs explicites ('SYNTHETICDOL', 'SYNTHETIMOX')
pour vérifier le comportement de l'importateur, de la recherche et de l'interface
sans jamais mélanger données de test et données médicales réelles.
"""

from __future__ import annotations

from pathlib import Path


def create_synthetic_bdpm_files(target_dir: Path) -> None:
    """Crée un jeu complet de fichiers au format tabulaire BDPM dans `target_dir`."""
    target_dir.mkdir(parents=True, exist_ok=True)

    # 1. CIS_bdpm.txt (encodé en cp1252 comme le fichier historique de la BDPM,
    # avec une ligne valide, une deuxième spécialité, un doublon, et une ligne malformée)
    cis_lines = [
        "90000001\tSYNTHETICDOL 1000 mg, comprimé pelliculé\tcomprimé pelliculé\torale\tAutorisation active\tProcédure nationale\tCommercialisée\t15/01/2020\t\t\tLABO FICTIF TEST\tNon",
        "90000002\tSYNTHETICDOL 500 mg, gélule\tgélule\torale\tAutorisation active\tProcédure nationale\tCommercialisée\t15/01/2020\tWarning disponibilité\t\tLABO FICTIF TEST\tNon",
        "90000003\tSYNTHETIMOX 1 g, poudre pour suspension buvable\tpoudre pour suspension buvable\torale\tAutorisation active\tProcédure nationale\tCommercialisée\t10/06/2018\t\t\tLABO ALPHA SYNTH\tOui",
        # Doublon volontaire sur 90000001
        "90000001\tSYNTHETICDOL 1000 mg, comprimé pelliculé\tcomprimé pelliculé\torale\tAutorisation active\tProcédure nationale\tCommercialisée\t15/01/2020\t\t\tLABO FICTIF TEST\tNon",
        # Ligne malformée (code CIS invalide)
        "ABC\tLIGNE INVALIDE\tcomprimé\torale\tAutorisation active\tProcédure nationale\tCommercialisée\t01/01/2020",
        # Ligne malformée (trop peu de colonnes)
        "90000099\tINCOMPLET",
    ]
    (target_dir / "CIS_bdpm.txt").write_bytes(
        ("\r\n".join(cis_lines) + "\r\n").encode("cp1252")
    )

    # 2. CIS_COMPO_bdpm.txt (avec substance accentuée, valeur de dosage manquante sur une ligne,
    # et un enregistrement orphelin dont le CIS 99999999 n'existe pas dans CIS_bdpm.txt)
    compo_lines = [
        "90000001\tcomprimé\t11111\tPARACÉTAMOL SYNTHÉTIQUE\t1000 mg\tun comprimé\tSA\t1",
        "90000002\tgélule\t11111\tPARACÉTAMOL SYNTHÉTIQUE\t500 mg\tune gélule\tSA\t1",
        "90000003\tpoudre\t22222\tAMOXICILLINE SYNTHÉTIQUE\t\tun sachet\tSA\t1",
        # Orphelin (CIS 99999999 absent de CIS_bdpm.txt)
        "99999999\tcomprimé\t33333\tSUBSTANCE ORPHELINE\t10 mg\tun comprimé\tSA\t1",
    ]
    (target_dir / "CIS_COMPO_bdpm.txt").write_bytes(
        ("\r\n".join(compo_lines) + "\r\n").encode("cp1252")
    )

    # 3. CIS_CIP_bdpm.txt (encodé en UTF-8 pour vérifier la détection mixte UTF-8 / cp1252)
    cip_lines = [
        "90000001\t3000001\tplaquette(s) de 8 comprimés\tPrésentation active\tDéclaration de commercialisation\t20/01/2020\t3400930000011\toui\t65%\t1,95\t2,97\t1,02\t",
        "90000003\t3000003\tboîte de 14 sachets\tPrésentation active\tDéclaration de commercialisation\t01/07/2018\t3400930000035\toui\t65%\t3,50\t4,52\t1,02\t",
    ]
    (target_dir / "CIS_CIP_bdpm.txt").write_bytes(
        ("\n".join(cip_lines) + "\n").encode("utf-8")
    )

    # 4. CIS_CPD_bdpm.txt
    cpd_lines = [
        "90000003\tliste I",
    ]
    (target_dir / "CIS_CPD_bdpm.txt").write_bytes(
        ("\r\n".join(cpd_lines) + "\r\n").encode("cp1252")
    )

    # 5. CIS_GENER_bdpm.txt
    gener_lines = [
        "501\tPARACÉTAMOL SYNTHÉTIQUE 1000 mg - SYNTHETICDOL 1000 mg\t90000001\t0\t1",
        "502\tAMOXICILLINE SYNTHÉTIQUE 1 g - PRINCEPSFICTIF 1 g, poudre\t90000003\t1\t1",
    ]
    (target_dir / "CIS_GENER_bdpm.txt").write_bytes(
        ("\r\n".join(gener_lines) + "\r\n").encode("cp1252")
    )

    # 6. CIS_InfoImportantes.txt (avec balise HTML comme dans la vraie BDPM)
    info_lines = [
        "90000003\t2025-01-10\t2027-01-10\t<a target='_blank' href='https://ansm.sante.fr/actualites/alerte-fictive-test'>Alerte fictive de sécurité pour test unitaire</a>",
    ]
    (target_dir / "CIS_InfoImportantes.txt").write_bytes(
        ("\r\n".join(info_lines) + "\r\n").encode("cp1252")
    )

    # 7. Métadonnées de téléchargement
    (target_dir / "metadata_telechargement.txt").write_text(
        "downloaded_at_utc=2026-10-26T12:00:00Z\nofficial_bdpm_date=29/09/2026\n",
        encoding="utf-8",
    )
