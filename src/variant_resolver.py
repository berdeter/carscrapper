"""Résolution de la variante technique d'une annonce (étape `resolve` du pipeline).

Le VariantResolver est le SEUL endroit qui résout une variante : le ModelCatalogMatcher se limite à
identifier marque/modèle, et les scrapers n'extraient que des faits bruts.

v4 (2026-11-23) : règles déterministes, le LLM n'est plus utilisé par défaut.
Chaque étape choisit une variante si elle est sans ambiguïté, sinon RÉDUIT l'ensemble des candidats
pour les étapes suivantes :
  0. candidat unique
  0b. année    : pré-filtre à ±1 an (écarte les générations impossibles).
  1. batterie  : d'abord la capacité NOMINALE citée dans le nom de la variante (« 50 kWh »), à ±1 kWh,
                 puis toutes les capacités (utile + nominale) avec une tolérance relative (max 2,5 kWh / 8 %).
                 Les valeurs aberrantes de l'annonce (< 15 ou > 160 kWh, ex. « 51-5-kwh » lu 5.0) sont ignorées.
  2. mots-clés : mots distinctifs du nom de la variante (« SE », « 55 », « Extended », « Grande »...) et
                 `mots_cles` optionnels du catalogue, cherchés dans titre + modèle + URL.
  3. puissance : kW/ch/pk lus dans le texte (sinon champ power_hp), comparés à
                 `donnees_techniques.puissance_moteur_kw` (nombre ou liste) si TOUS les candidats l'ont.
  4. année     : plage de génération stricte, puis ±1 an seulement si la stricte n'en laisse aucune.
  5. WLTP
  6. (optionnel) LLM, désactivé par défaut.
Si plusieurs variantes restent possibles, la résolution échoue mais `variantes_possibles` (ids) est
renseigné : le MarketEstimator s'en sert pour une estimation prudente.
"""
import hashlib
import os
import re
import unicodedata
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from spec_hints import SpecHints

RULES_VERSION = "4"
LLM_SCHEMA_VERSION = "2"
DEFAULT_OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://192.168.0.23:11434/")

_HP_PER_KW = 1.35962
_KWH_UTILE = re.compile(r"(\d+(?:[.,]\d+)?)\s*kwh\s+utile", re.IGNORECASE)
_KWH_ANY = re.compile(r"(\d+(?:[.,]\d+)?)\s*kwh", re.IGNORECASE)
_POWER_KW_TXT = re.compile(r"(?<![\d.,])(\d{2,3})\s*-?\s*kw\b", re.IGNORECASE)
_POWER_HP_TXT = re.compile(r"(?<![\d.,])(\d{2,3})\s*-?\s*(?:ch|cv|pk|ps|hp)\b", re.IGNORECASE)
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}.*$", re.IGNORECASE)
_UNITS = {"kw", "kwh", "ch", "cv", "pk", "ps", "hp", "km", "kms", "ans", "mois"}
_GENERIC_TOKENS = {"range", "batterie", "battery", "edition", "kwh", "utile"}

BATTERY_MIN, BATTERY_MAX = 15.0, 160.0
POWER_TOLERANCE_KW = 4.0


def _norm(text: Any) -> str:
    s = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return s.lower()


def _tokens(text: Any) -> List[str]:
    return re.findall(r"[a-z0-9]+", _norm(text))


def _num(s: str) -> float:
    return float(s.replace(",", "."))


class VariantResolver:
    def __init__(
        self,
        model_name: str = "qwen2.5:14b",
        base_url: str = DEFAULT_OLLAMA_URL,
        min_llm_confidence: float = 0.75,
        use_llm_fallback: bool = False,
        hints: Optional[SpecHints] = None,
        decision_cache: Optional[Any] = None,
    ):
        """
        hints          : heuristiques de spécifications (batterie/WLTP) ; défaut = SpecHints().
        decision_cache : objet avec get(key)/put(key, value) (ex. StageStore) pour mémoriser le LLM.
        use_llm_fallback : désactivé par défaut depuis la v4 (les règles suffisent sur les données observées).
        """
        self.model_name = model_name
        self.min_llm_confidence = min_llm_confidence
        self.use_llm_fallback = use_llm_fallback
        self.hints = hints or SpecHints()
        self.decision_cache = decision_cache
        self._llm = None
        self._last_llm_info: Dict[str, Any] = {}
        if use_llm_fallback:
            from langchain_ollama import ChatOllama  # import tardif : inutile sans LLM
            from pydantic import BaseModel, Field

            class VariantDecision(BaseModel):
                """Schéma strict : aucun champ n'a de valeur par défaut, le modèle doit tout remplir."""
                variante_id: Optional[str] = Field(description="variante_id choisi parmi les candidats, ou null.")
                confiance: float = Field(ge=0.0, le=1.0, description="Confiance entre 0.0 et 1.0.")
                justification: str = Field(description="Courte explication factuelle du choix.")

            base_llm = ChatOllama(model=model_name, base_url=base_url, temperature=0.0)
            self._llm = base_llm.with_structured_output(VariantDecision)

    def config_signature(self) -> str:
        """Change quand une règle ou un réglage modifie potentiellement le résultat."""
        llm = f"llm={self.model_name}|{self.min_llm_confidence}" if self.use_llm_fallback else "llm=0"
        return f"{RULES_VERSION}|{self.hints.signature}|{llm}"

    # ------------------------------------------------------------------
    # Lecture de l'annonce
    # ------------------------------------------------------------------
    @staticmethod
    def listing_text(listing: Any) -> str:
        """Titre + modèle + chemin de l'URL, sans les identifiants techniques (AutoScout `-cat_...`, UUID MyWay)."""
        path = urlparse(getattr(listing, "url", None) or "").path
        path = re.sub(r"[-_]cat_.*$", "", path)
        path = _UUID.sub("", path)
        return " ".join(str(getattr(listing, a, None) or "") for a in ("title", "model")) + " " + path

    @staticmethod
    def listing_power_kw(listing: Any, text: str) -> Optional[float]:
        """Puissance en kW : « 160kw » dans le texte, sinon « 136 pk / 113 ch », sinon le champ power_hp."""
        m = _POWER_KW_TXT.search(text)
        if m and 30 <= int(m.group(1)) <= 400:
            return float(m.group(1))
        m = _POWER_HP_TXT.search(text)
        if m and 40 <= int(m.group(1)) <= 600:
            return int(m.group(1)) / _HP_PER_KW
        hp = getattr(listing, "power_hp", None)
        if hp:
            try:
                return float(hp) / _HP_PER_KW
            except (TypeError, ValueError):
                return None
        return None

    @staticmethod
    def _listing_keyword_space(text: str) -> Tuple[Set[str], str]:
        """Jetons de l'annonce (les nombres suivis d'une unité — « 55 kWh », « 40 kW » — sont exclus) et
        version « phrase » pour les mots-clés à plusieurs mots."""
        toks = _tokens(text)
        kept = {t for i, t in enumerate(toks)
                if not (t.isdigit() and i + 1 < len(toks) and toks[i + 1] in _UNITS)}
        return kept, " " + " ".join(toks) + " "

    # ------------------------------------------------------------------
    # Caractéristiques des variantes
    # ------------------------------------------------------------------
    @staticmethod
    def _battery_values(var: Dict[str, Any]) -> Tuple[List[float], List[float]]:
        """(capacités nominales citées dans le nom, toutes les capacités connues)."""
        name = str(var.get("nom_variante", ""))
        utile_named = {_num(x) for x in _KWH_UTILE.findall(name)}
        nominal = [v for v in (_num(x) for x in _KWH_ANY.findall(name)) if v not in utile_named]
        allv = list(nominal) + list(utile_named)
        utile = var.get("donnees_techniques", {}).get("batterie_utile_kwh")
        if utile is not None:
            allv.append(float(utile))
        return nominal, allv

    @staticmethod
    def _variant_power(var: Dict[str, Any]) -> List[float]:
        p = var.get("donnees_techniques", {}).get("puissance_moteur_kw")
        if p is None:
            return []
        return [float(x) for x in (p if isinstance(p, list) else [p])]

    @staticmethod
    def _variant_keywords(var: Dict[str, Any]) -> Tuple[Set[str], List[str]]:
        """(jetons du nom hors parenthèses, mots-clés explicites du catalogue normalisés)."""
        name = str(var.get("nom_variante", "")).split("(")[0]
        toks = {t for t in _tokens(name) if t not in _GENERIC_TOKENS}
        phrases = [" " + " ".join(_tokens(k)) + " " for k in var.get("mots_cles", []) if _tokens(k)]
        return toks, phrases

    @staticmethod
    def _parse_generation_years(gen_str: str) -> Tuple[Optional[int], Optional[int]]:
        """'Phase 1 (2019-2023)' -> (2019, 2023) ; 'Gén 1 (2025+)' -> (2025, None)."""
        m = re.search(r"\((\d{4})\s*-\s*(\d{4})\)", gen_str or "")
        if m:
            return int(m.group(1)), int(m.group(2))
        m = re.search(r"\((\d{4})\+\)", gen_str or "")
        if m:
            return int(m.group(1)), None
        return None, None

    @staticmethod
    def _parse_year(year: Any) -> Optional[int]:
        if year is None:
            return None
        if isinstance(year, (int, float)):
            return int(year)
        m = re.search(r"\b(20\d{2}|19\d{2})\b", str(year))
        return int(m.group(1)) if m else None

    # ------------------------------------------------------------------
    # Étapes : chacune renvoie (pool réduit, gagnant ou None)
    # ------------------------------------------------------------------
    def _step_battery(self, battery: Optional[float], pool: List[Dict[str, Any]]):
        if battery is None or not (BATTERY_MIN <= battery <= BATTERY_MAX):
            return pool, None
        # a) capacité nominale citée dans le nom (« 50 kWh ») : c'est ce qu'affichent les annonces
        hits = [v for v in pool if any(abs(n - battery) <= 1.0 for n in self._battery_values(v)[0])]
        if len(hits) == 1:
            return hits, hits[0]
        if len(hits) > 1:
            pool = hits
        # b) toutes les capacités, tolérance relative ; le plus proche s'il est nettement meilleur
        tol = max(2.5, 0.08 * battery)
        scored = []
        for v in pool:
            vals = self._battery_values(v)[1]
            if vals:
                d = min(abs(x - battery) for x in vals)
                if d <= tol:
                    scored.append((d, v))
        if not scored:
            return pool, None
        scored.sort(key=lambda x: x[0])
        within = [v for _, v in scored]
        if len(scored) == 1 or scored[1][0] - scored[0][0] >= 1.0:
            return within, scored[0][1]
        return within, None

    def _step_keywords(self, text: str, pool: List[Dict[str, Any]]):
        listing_toks, listing_phrase = self._listing_keyword_space(text)
        names = [self._variant_keywords(v) for v in pool]
        common = set.intersection(*(t for t, _ in names)) if names else set()
        hits = []
        for v, (toks, phrases) in zip(pool, names):
            distinctive = toks - common
            if (distinctive & listing_toks) or any(p in listing_phrase for p in phrases):
                hits.append(v)
        if len(hits) == 1:
            return hits, hits[0]
        return (hits or pool), None

    def _step_power(self, kw: Optional[float], pool: List[Dict[str, Any]]):
        if kw is None or not all(self._variant_power(v) for v in pool):
            return pool, None
        hits = [v for v in pool if any(abs(p - kw) <= POWER_TOLERANCE_KW for p in self._variant_power(v))]
        if len(hits) == 1:
            return hits, hits[0]
        return (hits or pool), None

    def _step_year(self, year: Any, pool: List[Dict[str, Any]], strict: bool = True):
        """strict : plage exacte, puis ±1 an si aucune ; sinon (pré-filtre) ±1 an seulement."""
        y = self._parse_year(year)
        if y is None:
            return pool, None

        def compatible(margin: int) -> List[Dict[str, Any]]:
            out = []
            for v in pool:
                start, end = self._parse_generation_years(v.get("generation", ""))
                if start is None or (start - margin <= y <= (end + margin if end is not None else 2100)):
                    out.append(v)
            return out

        hits = (compatible(0) or compatible(1)) if strict else compatible(1)
        if len(hits) == 1:
            return hits, hits[0]
        return (hits or pool), None

    @staticmethod
    def _step_wltp(wltp: Optional[int], pool: List[Dict[str, Any]]):
        if wltp is None:
            return pool, None
        scored = []
        for v in pool:
            ref = v.get("donnees_techniques", {}).get("autonomie_wltp_km")
            if ref is not None and abs(ref - wltp) <= 15:
                scored.append((abs(ref - wltp), v))
        if not scored:
            return pool, None
        scored.sort(key=lambda x: x[0])
        if len(scored) == 1 or scored[1][0] - scored[0][0] >= 10:
            return [v for _, v in scored], scored[0][1]
        return [v for _, v in scored], None

    # ------------------------------------------------------------------
    def _effective_specs(self, listing: Any) -> Tuple[Optional[float], Optional[int], Dict[str, bool]]:
        """Batterie / WLTP : valeurs plausibles de l'annonce, sinon déduites par les heuristiques."""
        hint = self.hints.infer(listing)
        used = {"battery": False, "wltp": False}
        battery = listing.battery_kwh
        if battery is not None and not (BATTERY_MIN <= float(battery) <= BATTERY_MAX):
            battery = None  # valeur aberrante (ex. « 51-5-kwh » lu 5.0)
        if battery is None and hint.get("battery_kwh") is not None:
            battery, used["battery"] = hint["battery_kwh"], True
        wltp = listing.range_wltp_km
        if wltp is None and hint.get("range_wltp_km") is not None:
            wltp, used["wltp"] = hint["range_wltp_km"], True
        return battery, wltp, used

    def resolve_traced(
        self, enriched: Any, raw_description: str = "", force: bool = False
    ) -> Tuple[Any, Dict[str, Any]]:
        """Renvoie (enriched, trace). trace = {"method", "variante_id", "candidates", "pool_ids", ...}."""
        trace: Dict[str, Any] = {
            "method": "none", "variante_id": None, "candidates": len(enriched.variantes_candidates or []),
        }
        if force:
            enriched.variante_resolue = None

        def done(var: Dict[str, Any], method: str):
            enriched.variante_resolue = var
            enriched.variantes_possibles = [var.get("variante_id")]
            trace.update(method=method, variante_id=var.get("variante_id"))
            return enriched, trace

        if enriched.variante_resolue is not None:
            return done(enriched.variante_resolue, "preset")

        pool = list(enriched.variantes_candidates or [])
        if not pool:
            trace["method"] = "no_candidates"
            return enriched, trace
        if len(pool) == 1:
            return done(pool[0], "single")

        listing = enriched.listing
        battery, wltp, hinted = self._effective_specs(listing)
        text = self.listing_text(listing)
        # Les variantes d'une génération incompatible avec l'année (même à ±1 an) sont écartées d'emblée :
        # un mot-clé ambigu (« Mokka-e ») ne peut alors plus désigner une génération impossible.
        steps = [
            ("year", lambda p: self._step_year(listing.year, p, strict=False)),
            ("battery_hint" if hinted["battery"] else "battery", lambda p: self._step_battery(battery, p)),
            ("keyword", lambda p: self._step_keywords(text, p)),
            ("power", lambda p: self._step_power(self.listing_power_kw(listing, text), p)),
            ("year", lambda p: self._step_year(listing.year, p)),
            ("wltp_hint" if hinted["wltp"] else "wltp", lambda p: self._step_wltp(wltp, p)),
        ]
        for method, step in steps:
            pool, winner = step(pool)
            if winner is not None:
                return done(winner, method)
            if len(pool) == 1:
                return done(pool[0], method)

        if self.use_llm_fallback:
            llm_match = self._resolve_with_llm(enriched, pool, raw_description=raw_description)
            trace.update(self._last_llm_info)
            if llm_match is not None:
                return done(llm_match, "llm")

        enriched.variantes_possibles = [v.get("variante_id") for v in pool]
        trace["pool_ids"] = enriched.variantes_possibles
        return enriched, trace

    def resolve(self, enriched: Any, raw_description: str = "", force: bool = False) -> Any:
        return self.resolve_traced(enriched, raw_description, force)[0]

    # ------------------------------------------------------------------
    # LLM (optionnel, désactivé par défaut)
    # ------------------------------------------------------------------
    def _resolve_with_llm(self, enriched: Any, candidates: List[Dict[str, Any]], raw_description: str = ""):
        self._last_llm_info = {}
        if not self._llm:
            return None
        listing = enriched.listing
        summary = [
            {"variante_id": v["variante_id"], "nom_variante": v["nom_variante"], "generation": v["generation"],
             "batterie_utile_kwh": v["donnees_techniques"]["batterie_utile_kwh"],
             "autonomie_wltp_km": v["donnees_techniques"]["autonomie_wltp_km"]}
            for v in candidates
        ]
        prompt = (
            "Tu es un expert du marché automobile électrique européen.\n"
            "Identifie la variante technique exacte correspondant à cette annonce d'occasion.\n"
            "Choisis UNIQUEMENT un 'variante_id' de la liste, ou null si l'information est insuffisante.\n"
            "Remplis obligatoirement 'confiance' (0 à 1) et 'justification'.\n\n"
            f"ANNONCE :\n- Marque / Modèle : {enriched.marque_nom} {enriched.nom_canonique}\n"
            f"- Titre : {listing.title}\n- Modèle brut : {listing.model}\n- URL : {listing.url}\n"
            f"- Année : {listing.year}\n- Puissance : {listing.power_hp} ch | Batterie : {listing.battery_kwh} kWh"
            f" | WLTP : {listing.range_wltp_km} km\n- Description : {raw_description[:1500]}\n\n"
            f"VARIANTES CANDIDATES :\n{summary}"
        )
        key = hashlib.sha1(f"{self.model_name}\n{LLM_SCHEMA_VERSION}\n{prompt}".encode("utf-8")).hexdigest()
        cached = self.decision_cache.get(key) if self.decision_cache is not None else None
        from_cache = cached is not None
        if cached is None:
            try:
                decision = self._llm.invoke(prompt)
            except Exception as e:  # erreur transitoire : ni mémorisée, ni journalisée par le pipeline
                print(f"[VariantResolver] Erreur Ollama sur '{listing.title}': {e}")
                self._last_llm_info = {"llm_error": True}
                return None
            cached = {"variante_id": decision.variante_id, "confiance": decision.confiance,
                      "justification": decision.justification, "model": self.model_name}
            if self.decision_cache is not None:
                self.decision_cache.put(key, cached)
        self._last_llm_info = {"llm_cached": from_cache, "llm_confidence": cached.get("confiance")}
        if cached.get("variante_id") and (cached.get("confiance") or 0.0) >= self.min_llm_confidence:
            for v in candidates:
                if v["variante_id"] == cached["variante_id"]:
                    return v
        return None


if __name__ == "__main__":
    # Rejeu de la résolution sur les annonces déjà stockées, SANS re-scraper.
    #   python variant_resolver.py [--force] [--llm] [--data-dir data] [--db data/resolved_cars.json]
    import argparse

    import paths
    from pipeline import CarPipeline

    ap = argparse.ArgumentParser(description="Rejoue l'étape de résolution de variante (aucun scraping).")
    ap.add_argument("--force", action="store_true", help="re-résoudre même les annonces inchangées")
    ap.add_argument("--llm", action="store_true", help="active l'arbitrage LLM en dernier recours")
    ap.add_argument("--no-llm", action="store_true", help=argparse.SUPPRESS)  # compatibilité (défaut)
    ap.add_argument("--data-dir", default=str(paths.DATA_DIR))
    ap.add_argument("--catalog", default=str(paths.CATALOG))
    ap.add_argument("--db", default=str(paths.RESOLVED_DB))
    args = ap.parse_args()

    pipe = CarPipeline(scrapers=[], resolved_db_path=args.db, data_dir=args.data_dir, catalog_path=args.catalog)
    pipe.stage_resolve(force=args.force, use_llm=args.llm)
