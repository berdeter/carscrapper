"""Résolution de la variante technique d'une annonce (étape `resolve` du pipeline).

Le VariantResolver est le SEUL endroit qui résout une variante : le ModelCatalogMatcher se limite à
identifier marque/modèle, et les scrapers n'extraient que des faits bruts.

Ordre des étapes : candidat unique -> batterie -> année/génération -> WLTP -> LLM (mémorisé).
Corrections v3 : l'étape batterie ne prend plus « le premier candidat dans la tolérance » (résultat
dépendant de l'ordre du catalogue) : elle retient le plus proche s'il est nettement meilleur, sinon
réduit l'ensemble des candidats pour les étapes suivantes ; les étapes année/WLTP/LLM travaillent sur cet
ensemble réduit (avant, le WLTP ignorait le filtre par année).

Changements v2 :
  * les heuristiques jadis codées dans les scrapers (batterie/WLTP déduits) viennent de `SpecHints` ;
  * `resolve_traced()` indique la méthode qui a tranché (battery, year, wltp, llm, ...) ;
  * `force=True` permet de rejouer une résolution déjà faite ;
  * les décisions du LLM sont mémorisées (clé = modèle + prompt) : rejouer ne rappelle pas Ollama ;
  * le prompt ne contient plus le prix ni le kilométrage (volatils, sans rapport avec la variante) ;
  * langchain n'est importé que si le LLM est activé (`use_llm_fallback=False` marche sans) ;
  * `config_signature()` résume les règles/réglages, ce qui permet au pipeline de savoir quand une
    annonce doit être re-résolue.
"""
import hashlib
import re
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from spec_hints import SpecHints

RULES_VERSION = "3"


class VariantDecision(BaseModel):
    """Schéma de sortie strict pour l'arbitrage par le LLM local."""
    variante_id: Optional[str] = Field(
        default=None,
        description="L'identifiant exact (variante_id) choisi parmi les candidats, ou null si impossible à déterminer avec certitude."
    )
    confiance: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Niveau de confiance entre 0.0 et 1.0."
    )
    justification: str = Field(
        default="",
        description="Courte explication factuelle du choix (ex: 'Finition First Edition associée à la batterie 52 kWh')."
    )


class VariantResolver:
    def __init__(
        self,
        model_name: str = "qwen2.5-coder:14b",
        base_url: str = "http://192.168.0.23:11434/",
        min_llm_confidence: float = 0.75,
        use_llm_fallback: bool = True,
        hints: Optional[SpecHints] = None,
        decision_cache: Optional[Any] = None,
    ):
        """
        hints          : heuristiques de spécifications (batterie/WLTP) ; défaut = SpecHints().
        decision_cache : objet avec get(key)/put(key, value) (ex. StageStore) pour mémoriser le LLM.
        """
        self.model_name = model_name
        self.min_llm_confidence = min_llm_confidence
        self.use_llm_fallback = use_llm_fallback
        self.hints = hints or SpecHints()
        self.decision_cache = decision_cache
        self._llm = None
        self._last_llm_info: Dict[str, Any] = {}
        if use_llm_fallback:
            from langchain_ollama import ChatOllama  # import tardif : inutile pour un rejeu sans LLM

            base_llm = ChatOllama(model=model_name, base_url=base_url, temperature=0.0)
            self._llm = base_llm.with_structured_output(VariantDecision)

    def config_signature(self) -> str:
        """Change quand une règle ou un réglage modifie potentiellement le résultat."""
        return f"{RULES_VERSION}|{self.hints.signature}|llm={int(bool(self.use_llm_fallback))}|{self.model_name}|{self.min_llm_confidence}"

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_generation_years(gen_str: str) -> Tuple[Optional[int], Optional[int]]:
        """Extrait les bornes d'années d'une chaîne comme 'Phase 1 (2019-2023)' ou 'Gén 1 (2025+)'."""
        match_range = re.search(r"\((\d{4})\s*-\s*(\d{4})\)", gen_str)
        if match_range:
            return int(match_range.group(1)), int(match_range.group(2))

        match_plus = re.search(r"\((\d{4})\+\)", gen_str)
        if match_plus:
            return int(match_plus.group(1)), None

        return None, None

    def _filter_by_year(
        self, year: Optional[Any], candidates: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Élimine les variantes incompatibles avec l'année du véhicule (avec marge d'1 an pour les fins de série)."""
        if year is None:
            return candidates

        # Conversion / extraction robuste de l'année en int (gère '07-2022', '2022', etc.)
        if isinstance(year, str):
            match = re.search(r'\b(20\d{2}|19\d{2})\b', year)
            if match:
                try:
                    year = int(match.group(1))
                except ValueError:
                    return candidates
            else:
                try:
                    year = int(year)
                except ValueError:
                    return candidates
        elif not isinstance(year, int):
            try:
                year = int(year)
            except (ValueError, TypeError):
                return candidates

        compatible = []
        for var in candidates:
            start_y, end_y = self._parse_generation_years(var.get("generation", ""))
            if start_y is None:
                compatible.append(var)
                continue

            # Tolérance de +/- 1 an pour les immatriculations tardives ou pré-séries
            min_ok = start_y - 1
            max_ok = (end_y + 1) if end_y is not None else 2030

            if min_ok <= year <= max_ok:
                compatible.append(var)

        return compatible if compatible else candidates

    def _match_by_wltp_range(
        self, range_wltp_km: Optional[int], candidates: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """Trouve la variante dont l'autonomie WLTP est la plus proche (tolérance +/- 15 km sans ambiguïté)."""
        if range_wltp_km is None or not candidates:
            return None

        scored: List[Tuple[int, Dict[str, Any]]] = []
        for var in candidates:
            ref_wltp = var.get("donnees_techniques", {}).get("autonomie_wltp_km")
            if ref_wltp is not None:
                diff = abs(ref_wltp - range_wltp_km)
                if diff <= 15:
                    scored.append((diff, var))

        if not scored:
            return None

        scored.sort(key=lambda x: x[0])
        # Si un seul candidat est dans la fenêtre ou nettement plus proche que le second
        if len(scored) == 1 or (scored[1][0] - scored[0][0] >= 10):
            return scored[0][1]

        return None

    # ------------------------------------------------------------------
    def _resolve_with_llm(
        self, enriched: Any, candidates: List[Dict[str, Any]], raw_description: str = ""
    ) -> Optional[Dict[str, Any]]:
        """Sollicite le LLM local pour trancher parmi les variantes candidates (décisions mémorisées)."""
        self._last_llm_info = {}
        if not self._llm:
            return None

        listing = enriched.listing
        candidates_summary = [
            {
                "variante_id": v["variante_id"],
                "nom_variante": v["nom_variante"],
                "generation": v["generation"],
                "batterie_utile_kwh": v["donnees_techniques"]["batterie_utile_kwh"],
                "autonomie_wltp_km": v["donnees_techniques"]["autonomie_wltp_km"],
            }
            for v in candidates
        ]

        # Le prix et le kilométrage sont volontairement absents : volatils et sans rapport avec la
        # variante ; leur présence rendrait la mémorisation inutile (le prompt changerait à chaque scrape).
        prompt = (
            "Tu es un expert du marché automobile électrique européen (2026).\n"
            "Identifie la variante technique exacte correspondant à cette annonce d'occasion.\n"
            "RÈGLES STRICTES :\n"
            "- Choisis UNIQUEMENT un 'variante_id' présent dans la liste des candidats fournie.\n"
            "- Utilise tes connaissances des finitions/séries spéciales (ex: First Edition, Iconic, La Prima, etc.), "
            "de la puissance en ch/kW, de l'année et du texte de l'annonce.\n"
            "- Si l'information est véritablement insuffisante pour trancher sans deviner au hasard, renvoie variante_id=null.\n\n"
            f"ANNONCE :\n"
            f"- Marque / Modèle : {enriched.marque_nom} {enriched.nom_canonique}\n"
            f"- Titre : {listing.title}\n"
            f"- Modèle brut : {listing.model}\n"
            f"- Année : {listing.year}\n"
            f"- Puissance : {listing.power_hp} ch | Batterie : {listing.battery_kwh} kWh | WLTP : {listing.range_wltp_km} km\n"
            f"- Description complémentaire : {raw_description[:1500]}\n\n"
            f"VARIANTES CANDIDATES :\n{candidates_summary}"
        )

        key = hashlib.sha1(f"{self.model_name}\n{prompt}".encode("utf-8")).hexdigest()
        cached = self.decision_cache.get(key) if self.decision_cache is not None else None
        from_cache = cached is not None
        if cached is None:
            try:
                decision: VariantDecision = self._llm.invoke(prompt)
            except Exception as e:  # erreur transitoire : non mémorisée
                print(f"[VariantResolver] Erreur Ollama sur '{listing.title}': {e}")
                return None
            cached = {
                "variante_id": decision.variante_id,
                "confiance": decision.confiance,
                "justification": decision.justification,
                "model": self.model_name,
            }
            if self.decision_cache is not None:
                self.decision_cache.put(key, cached)

        self._last_llm_info = {"llm_cached": from_cache, "llm_confidence": cached.get("confiance")}
        # Le seuil est appliqué à la lecture : le modifier ne nécessite pas de rappeler le LLM.
        if cached.get("variante_id") and (cached.get("confiance") or 0.0) >= self.min_llm_confidence:
            for v in candidates:
                if v["variante_id"] == cached["variante_id"]:
                    return v
        return None

    # ------------------------------------------------------------------
    def _effective_specs(self, listing: Any) -> Tuple[Optional[float], Optional[int], Dict[str, bool]]:
        """Batterie / WLTP : valeurs de l'annonce en priorité, sinon déduites par les heuristiques."""
        hint = self.hints.infer(listing)
        used = {"battery": False, "wltp": False}
        battery = listing.battery_kwh
        if battery is None and hint.get("battery_kwh") is not None:
            battery, used["battery"] = hint["battery_kwh"], True
        wltp = listing.range_wltp_km
        if wltp is None and hint.get("range_wltp_km") is not None:
            wltp, used["wltp"] = hint["range_wltp_km"], True
        return battery, wltp, used

    @staticmethod
    def _variant_kwh_values(var: Dict[str, Any]) -> List[float]:
        """Capacités possibles d'une variante : batterie utile + toute valeur « <n> kWh » du nom (nominal commercial)."""
        vals: List[float] = []
        utile = var.get("donnees_techniques", {}).get("batterie_utile_kwh")
        if utile is not None:
            vals.append(float(utile))
        for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*kwh", str(var.get("nom_variante", "")), re.IGNORECASE):
            vals.append(float(m.group(1).replace(",", ".")))
        return vals

    def _match_by_battery(
        self, battery: float, candidates: List[Dict[str, Any]], tolerance: float = 2.5, margin: float = 1.0
    ) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """Renvoie (candidats dans la tolérance, gagnant sans ambiguïté ou None).
        Le gagnant est le plus proche, à condition de battre le suivant d'au moins `margin` kWh."""
        scored: List[Tuple[float, Dict[str, Any]]] = []
        for var in candidates:
            vals = self._variant_kwh_values(var)
            if vals:
                dist = min(abs(v - battery) for v in vals)
                if dist <= tolerance:
                    scored.append((dist, var))
        if not scored:
            return [], None
        scored.sort(key=lambda x: x[0])
        pool = [v for _, v in scored]
        if len(scored) == 1 or scored[1][0] - scored[0][0] >= margin:
            return pool, scored[0][1]
        return pool, None

    def resolve_traced(
        self, enriched: Any, raw_description: str = "", force: bool = False
    ) -> Tuple[Any, Dict[str, Any]]:
        """Comme resolve(), mais renvoie aussi une trace {"method", "variante_id", "candidates", "pool", ...}."""
        trace: Dict[str, Any] = {
            "method": "none", "variante_id": None, "candidates": len(enriched.variantes_candidates or []),
        }
        if force:
            enriched.variante_resolue = None

        def done(var: Dict[str, Any], method: str) -> Tuple[Any, Dict[str, Any]]:
            enriched.variante_resolue = var
            trace.update(method=method, variante_id=var.get("variante_id"))
            return enriched, trace

        if enriched.variante_resolue is not None:
            return done(enriched.variante_resolue, "preset")

        pool = list(enriched.variantes_candidates or [])
        if not pool:
            trace["method"] = "no_candidates"
            return enriched, trace

        # 0. Un seul candidat : rien à trancher
        if len(pool) == 1:
            return done(pool[0], "single")

        listing = enriched.listing
        battery, wltp, hinted = self._effective_specs(listing)

        # 1. Batterie (valeur de l'annonce, sinon déduite) : plus proche si nettement meilleur, sinon on réduit
        if battery is not None:
            within, winner = self._match_by_battery(battery, pool)
            if winner is not None:
                return done(winner, "battery_hint" if hinted["battery"] else "battery")
            if within:
                pool = within

        # 2. Année / génération
        pool = self._filter_by_year(listing.year, pool)
        if len(pool) == 1:
            return done(pool[0], "year")

        # 3. Autonomie WLTP
        wltp_match = self._match_by_wltp_range(wltp, pool)
        if wltp_match is not None:
            return done(wltp_match, "wltp_hint" if hinted["wltp"] else "wltp")

        # 4. Arbitrage par LLM local (fallback), sur l'ensemble restant
        trace["pool"] = len(pool)
        if self.use_llm_fallback:
            llm_match = self._resolve_with_llm(enriched, pool, raw_description=raw_description)
            trace.update(self._last_llm_info)
            if llm_match is not None:
                return done(llm_match, "llm")

        return enriched, trace

    def resolve(self, enriched: Any, raw_description: str = "", force: bool = False) -> Any:
        """Tente de peupler `enriched.variante_resolue` si elle est encore à None (ou si `force`)."""
        return self.resolve_traced(enriched, raw_description, force)[0]


if __name__ == "__main__":
    # Rejeu de la résolution sur les annonces déjà stockées, SANS re-scraper.
    #   python variant_resolver.py [--force] [--no-llm] [--data-dir .] [--db resolved_cars.json]
    import argparse

    from pipeline import CarPipeline

    ap = argparse.ArgumentParser(description="Rejoue l'étape de résolution de variante (aucun scraping).")
    ap.add_argument("--force", action="store_true", help="re-résoudre même les annonces inchangées")
    ap.add_argument("--no-llm", action="store_true", help="règles seulement, sans appel au LLM")
    ap.add_argument("--data-dir", default=".")
    ap.add_argument("--catalog", default="models.json")
    ap.add_argument("--db", default="resolved_cars.json")
    args = ap.parse_args()

    pipe = CarPipeline(scrapers=[], resolved_db_path=args.db, data_dir=args.data_dir, catalog_path=args.catalog)
    pipe.stage_resolve(force=args.force, use_llm=not args.no_llm)
