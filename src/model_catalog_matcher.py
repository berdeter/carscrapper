import json
import re
from typing import Any, Dict, List, Optional, Tuple
from cardata import CarListing, EnrichedCarListing
import unicodedata
from pathlib import Path

import paths


def _normalize_text(text: str) -> str:
    """Normalise une chaîne (minuscules, sans accents, ponctuation unifiée)."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    
    # Nettoyage des générations Stellantis courantes (ex: "c3 4" -> "c3", "208 2" -> "208")
    no_gen = re.sub(
        r"\b(c3|c4|c5|208|2008|308|3008|5008|corsa|mokka|astra)\s+[1-9]\b",
        r"\1",
        no_accents.lower(),
    )
    # Remplace tirets, deux-points et ponctuation par des espaces simples
    cleaned = re.sub(r"[-_:./]+", " ", no_gen)
    return re.sub(r"\s+", " ", cleaned).strip()


class ModelCatalogMatcher:
    def __init__(self, json_path: str | Path = paths.CATALOG):
        self.json_path = Path(json_path)
        self._brand_index: Dict[str, Dict[str, Any]] = {}
        self._load_catalog()

    def _get_candidate_aliases(self, model_obj: dict) -> list[str]:
        """Construit la liste complète et intelligente des alias à partir de nom_canonique et alias_scraping."""
        candidates = set()

        # 1. Ajout systématique du nom_canonique normalisé (ex: "ë-C3" -> "e c3")
        canon = _normalize_text(model_obj.get("nom_canonique", ""))
        if canon:
            candidates.add(canon)
            candidates.add(canon.replace(" ", ""))  # ex: "ec3", "600e"

        # 2. Si c'est une variante 100% EV, dérivation automatique du nom de base nu
        # Ex: "e c3" -> "c3", "600e" -> "600", "corsa e" -> "corsa"
        if model_obj.get("type_ev") == "Variante 100% EV":
            base_name = re.sub(r"^e\s+|\s+e$|(?<=\d)e$", "", canon).strip()
            if base_name and len(base_name) >= 2:
                candidates.add(base_name)

        # 3. Ajout des alias manuels du catalogue
        for alias in model_obj.get("alias_scraping", []):
            norm_alias = _normalize_text(alias)
            if norm_alias:
                candidates.add(norm_alias)

        # Tri par longueur décroissante pour matcher les noms composés en premier
        # (ex: "c3 aircross" avant "c3", "500x" avant "500")
        return sorted(candidates, key=len, reverse=True)        

    def _load_catalog(self) -> None:
        """Charge le catalogue et génère les règles de correspondance via _get_candidate_aliases."""
        with self.json_path.open("r", encoding="utf-8") as f:
            data = json.load(f)

        for brand_entry in data.get("catalogue_filtrage", []):
            brand_keys = {
                _normalize_text(brand_entry["marque_id"]),
                _normalize_text(brand_entry["marque_nom"]),
            }

            model_rules: List[Tuple[str, Dict[str, Any]]] = []
            for model in brand_entry.get("modeles", []):
                # UTILISATION ICI DE LA MÉTHODE INTELLIGENTE
                aliases = self._get_candidate_aliases(model)
                for alias in aliases:
                    if alias:
                        model_rules.append((alias, model))

            # Tri par longueur décroissante pour matcher d'abord les alias les plus spécifiques
            model_rules.sort(key=lambda x: len(x[0]), reverse=True)

            brand_payload = {
                "marque_id": brand_entry["marque_id"],
                "marque_nom": brand_entry["marque_nom"],
                "feeling_marque": brand_entry["feeling_marque"],
                "model_rules": model_rules,
            }

            for key in brand_keys:
                if key:
                    self._brand_index[key] = brand_payload

    def match_listing(self, listing: CarListing) -> Optional[EnrichedCarListing]:
        """
        Cherche la voiture dans models.json.
        - Retourne None si la marque ou le modèle n'est pas dans la liste (rejet).
        - Retourne un EnrichedCarListing si trouvé.
        """
        norm_brand = _normalize_text(listing.brand)
        brand_data = self._brand_index.get(norm_brand)
        if not brand_data or not brand_data["model_rules"]:
            return None

        # On combine le champ model et le titre pour maximiser les chances de détection
        search_target = f" {_normalize_text(listing.model)} {_normalize_text(listing.title)} "

        matched_model: Optional[Dict[str, Any]] = None
        for norm_alias, model_obj in brand_data["model_rules"]:
            # Vérification par frontières de mots pour éviter les faux positifs partiels
            pattern = rf"(?:^|\s){re.escape(norm_alias)}(?:$|\s)"
            if re.search(pattern, search_target):
                matched_model = model_obj
                break
            if re.search(pattern, self._clean_for_matching(search_target)):
                matched_model = model_obj
                break  

        if not matched_model:
            return None

        # Le matcher identifie la marque et le modèle, rien de plus : la variante est résolue par le
        # VariantResolver (étape `resolve` du pipeline), qui est le seul endroit à le faire.
        variants = matched_model.get("variantes", [])

        return EnrichedCarListing(
            listing=listing,
            marque_id=brand_data["marque_id"],
            marque_nom=brand_data["marque_nom"],
            feeling_marque=brand_data["feeling_marque"],
            modele_id=matched_model["id"],
            nom_canonique=matched_model["nom_canonique"],
            segment=matched_model["segment"],
            type_ev=matched_model["type_ev"],
            variantes_candidates=variants,
            variante_resolue=None,
            url=listing.url,
        )

    def filter_and_enrich_listings(
        self,
        database: List[CarListing]
    ) -> List[EnrichedCarListing]:
        """Filtre la base scrapée et ne conserve que les offres reconnues, enrichies."""
        enriched_results: List[EnrichedCarListing] = []

        for car in database:
            enriched = self.match_listing(car)
            if enriched is not None:
                enriched_results.append(enriched)

        return enriched_results
    
    def _clean_for_matching(self, text: str) -> str:
        if not text:
            return ""
        t = text.lower().strip()
        # Supprime le numéro de génération isolé juste après le nom du modèle (ex: "c3 4 electric" -> "c3 electric")
        t = re.sub(
            r"\b(c3|c4|c5|208|2008|308|3008|5008|corsa|mokka|astra)\s+[1-9]\b",
            r"\1",
            t,
        )
        return re.sub(r"\s+", " ", t)    


if __name__ == "__main__":
    from variant_resolver import VariantResolver

    sample_db = [
        CarListing(brand="Volkswagen", model="ID.3", title="VW ID.3 Pro Performance 58 kWh", price=21500, source="Autoscout"),
        CarListing(brand="Tesla", model="Model 3", title="Tesla Model 3 Propulsion", price=24900, source="Autoscout"),
        CarListing(brand="Renault", model="R5", title="Renault 5 E-Tech 52 kWh Iconic", price=28900, source="Autoscout"),
        CarListing(brand="Škoda", model="Epiq", title="Skoda Epiq First Edition", price=26500, source="Autoscout"),
        CarListing(brand="Citroën", model="C3", title="Citroën ë-C3 Max", price=19900, source="Autoscout"),
        CarListing(
            source="Autoscout",
            title="Skoda Epiq First Edition",
            brand="Škoda",
            model="Epiq",
            price=26500,
            range_wltp_km=441
        )
    ]

    model_matcher: ModelCatalogMatcher = ModelCatalogMatcher()
    enriched_cars = model_matcher.filter_and_enrich_listings(sample_db)
    print("filter_and_enrich_listings ==> ", enriched_cars)

    resolver = VariantResolver(model_name="qwen2.5-coder:14b", use_llm_fallback=True)
    resolved_cars = [resolver.resolve(car) for car in enriched_cars]
    print("after VariantResolver ==>", resolved_cars)