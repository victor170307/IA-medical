"""Fonctions de normalisation de texte pour la recherche de médicaments.

Pourquoi normaliser ?
Dans les fichiers officiels de la BDPM, les noms de spécialités et de substances
peuvent contenir des majuscules, des accents (« PARACÉTAMOL » ou « PARACETAMOL »),
des virgules ou des espaces multiples.
La normalisation permet de comparer proprement la saisie de l'utilisateur avec
les données officielles sans jamais modifier le texte officiel affiché.
"""

from __future__ import annotations

import re
import unicodedata


def normalize_text(text: str | None) -> str:
    """Normalise une chaîne pour la recherche :
    - convertit en minuscules ;
    - retire les accents (ex. 'é' -> 'e', 'œ' -> 'oe') ;
    - remplace la ponctuation par des espaces ;
    - fusionne les espaces multiples.
    """
    if not text:
        return ""

    # Remplacement explicite des ligatures fréquentes en français médical
    cleaned = text.replace("œ", "oe").replace("Œ", "oe").replace("æ", "ae").replace("Æ", "ae")

    # Décomposition Unicode (NFD) pour séparer les lettres de leurs accents
    decomposed = unicodedata.normalize("NFD", cleaned)
    without_accents = "".join(
        ch for ch in decomposed if unicodedata.category(ch) != "Mn"
    )

    # Passage en minuscules
    lowered = without_accents.lower()

    # Remplacement du symbole '%' par ' pour cent ' pour unifier ex: '0,05%' et '0,05 POUR CENT'
    lowered = lowered.replace("%", " pour cent ")

    # Normalisation des nombres décimaux français (ex: "1,00 mg" -> "1.00 mg" et "1,00")
    # On remplace toute ponctuation non alphanumérique par un espace
    alnum_spaces = re.sub(r"[^a-z0-9]+", " ", lowered)

    # Séparation des nombres collés aux unités de dosage courantes (ex: '1000mg' -> '1000 mg', '1g' -> '1 g')
    alnum_spaces = re.sub(
        r"\b(\d+)\s*(mg|g|kg|mcg|ug|ml|cl|dl|l|ui|mui|mmol|meq)\b",
        r"\1 \2",
        alnum_spaces,
    )

    # Réduction des espaces multiples
    return re.sub(r"\s+", " ", alnum_spaces).strip()


def tokenize_query(query: str | None) -> list[str]:
    """Découpe une requête utilisateur en mots-clés normalisés non vides."""
    normalized = normalize_text(query)
    if not normalized:
        return []
    tokens = normalized.split(" ")
    # Si l'utilisateur préfixe un identifiant numérique par 'cis' ou 'cip' (ex: 'cis 66296030'),
    # on retire ce préfixe descriptif pour permettre la recherche directe sur le code.
    if len(tokens) > 1 and any(any(ch.isdigit() for ch in t) for t in tokens):
        filtered = [t for t in tokens if t not in ("cis", "cip", "code")]
        if filtered:
            return filtered
    return tokens
