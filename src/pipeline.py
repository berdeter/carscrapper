"""Pipeline en étapes rejouables.

    scrape  ->  match  ->  details  ->  resolve  ->  (MarketEstimator)
      |          |           |            |
 raw_listings  catalog_   raw_listings  resolution_log + resolved_cars.json
   .json      matches.json  (fusion)     (+ llm_decisions.json)

Chaque étape lit le résultat PERSISTÉ de la précédente : on peut donc en rejouer une sans refaire
les autres, en particulier rejouer `match` ou `resolve` sans re-scraper.

  scrape   réseau   les scrapers ne font qu'extraire des faits bruts ; ils sont fusionnés dans
                    raw_listings.json (historique de prix, first/last_seen, activité).
  match    local    BrandFilter + ModelCatalogMatcher -> (marque_id, modele_id) de chaque annonce.
                    À rejouer quand les alias / marques autorisées changent.
  details  réseau   scrape_details, une seule fois par annonce retenue (sauf `refresh`).
  resolve  local    Variantes candidates relues dans models.json COURANT (donc toujours à jour, y compris
                    les simulations 1000 km), puis VariantResolver (règles puis LLM mémorisé). À rejouer
                    quand une règle, SpecHints ou models.json change : seules les annonces dont l'entrée
                    a changé sont recalculées.

Exemples :
    python pipeline.py                                 # tout (scraping inclus)
    python pipeline.py --only scrape --max-pages 10
    python pipeline.py --only match,resolve            # rejeu hors-ligne, aucun scraping
    python pipeline.py --only resolve --force          # re-résoudre tout (règles seules ; --llm pour le LLM)
"""
import argparse
import copy
import dataclasses
import glob
import hashlib
import importlib.util
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from cardata import CarListing, CarScraperBase, DetailsGoneError, EnrichedCarListing
from spec_hints import SpecHints
from stagestore import StageStore
from variant_resolver import VariantResolver

STAGES = ("scrape", "match", "details", "resolve")
MAX_DETAILS_ATTEMPTS = 3  # au-delà, une fiche en échec n'est plus retentée (sauf --retry-failed-details)

_LISTING_FIELDS = set(CarListing.__dataclass_fields__)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def listing_from_dict(d: Dict[str, Any]) -> CarListing:
    return CarListing(**{k: v for k, v in d.items() if k in _LISTING_FIELDS})


def listing_key(listing: CarListing) -> str:
    return listing.url or f"{listing.source}|{listing.title}"


def _merge_listing(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """Les valeurs non nulles du nouveau scrape écrasent les anciennes ; les champs absents du
    nouveau scrape (ex. SoH issu d'une fiche de détail) sont conservés."""
    merged = dict(old)
    for k, v in new.items():
        if v is not None:
            merged[k] = v
    return merged


class CarPipeline:

    def __init__(
        self,
        scrapers: Optional[List[Any]] = None,
        helper: Optional[Any] = None,
        model_name: str = "qwen2.5:14b",
        resolved_db_path: str = "resolved_cars_cache.json",
        data_dir: str = ".",
        use_llm_fallback: bool = False,
        spec_hints: Optional[SpecHints] = None,
        resolver: Optional[VariantResolver] = None,
        catalog_path: str = "models.json",
    ):
        """
        scrapers   : liste de scrapers ; None = découverte automatique (paresseuse) ; [] = aucun.
        data_dir   : dossier des magasins d'étapes (raw_listings.json, catalog_matches.json, ...).
        resolver   : résolveur pré-construit (sinon créé avec model_name / use_llm_fallback).
        catalog_path : référentiel des modèles (le même que celui du ModelCatalogMatcher). Il doit contenir
                       les simulations (`simulation_humaine_1000km`) : lancer d'abord le simulateur.
        """
        self.helper = helper
        self.model_name = model_name
        self.resolved_db_path = resolved_db_path
        self.use_llm_fallback = use_llm_fallback
        self.spec_hints = spec_hints or SpecHints()
        self.catalog_path = catalog_path
        self._catalog_cache: Optional[Tuple[float, Dict[Tuple[str, str], Tuple[Dict, Dict]]]] = None
        self._resolver = resolver
        self._scrapers = scrapers

        d = Path(data_dir)
        self.raw = StageStore(d / "raw_listings.json")
        self.matches = StageStore(d / "catalog_matches.json")
        self.resolution_log = StageStore(d / "resolution_log.json")
        self.llm_cache = StageStore(d / "llm_decisions.json")

    # ------------------------------------------------------------------
    # Scrapers
    # ------------------------------------------------------------------
    @property
    def scrapers(self) -> List[Any]:
        if self._scrapers is None:
            self._scrapers = self._discover_scrapers(helper=self.helper)
        return self._scrapers

    @staticmethod
    def _discover_scrapers(helper: Optional[Any] = None) -> List[Any]:
        scrapers_list = []
        search_paths = glob.glob("*scraper.py") + glob.glob("src/*scraper.py")

        for filepath in set(search_paths):
            module_name = os.path.splitext(os.path.basename(filepath))[0]
            try:
                spec = importlib.util.spec_from_file_location(module_name, filepath)
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
            except Exception as e:
                print(f"⚠️ Impossible d'importer {filepath} : {e}")

        def get_subclasses(cls):
            subs = cls.__subclasses__()
            for sub in subs:
                subs.extend(get_subclasses(sub))
            return subs

        seen = set()
        for cls in get_subclasses(CarScraperBase):
            if cls in seen:
                continue
            seen.add(cls)
            try:
                scrapers_list.append(cls(helper=helper))
            except Exception as e:
                print(f"❌ Impossible d'instancier {cls.__name__} : {e}")

        return scrapers_list

    # ------------------------------------------------------------------
    # Utilitaires
    # ------------------------------------------------------------------
    def _active_keys(self, keys: Optional[Iterable[str]] = None) -> List[str]:
        """Clés des annonces actives (toutes si `keys` est None)."""
        pool = list(keys) if keys is not None else list(self.raw.keys())
        return [k for k in pool if k in self.raw and self.raw.get(k).get("active", True)]

    def _catalog(self) -> Dict[Tuple[str, str], Tuple[Dict[str, Any], Dict[str, Any]]]:
        """{(marque_id, modele_id): (entrée marque, entrée modèle)} ; relu si models.json a changé."""
        path = Path(self.catalog_path)
        mtime = path.stat().st_mtime
        if self._catalog_cache is None or self._catalog_cache[0] != mtime:
            data = json.loads(path.read_text(encoding="utf-8"))
            index = {}
            for brand in data.get("catalogue_filtrage", []):
                for model in brand.get("modeles", []):
                    index[(brand["marque_id"], model["id"])] = (brand, model)
            self._catalog_cache = (mtime, index)
        return self._catalog_cache[1]

    def _get_resolver(self, use_llm: Optional[bool]) -> VariantResolver:
        if self._resolver is not None:
            return self._resolver
        return VariantResolver(
            model_name=self.model_name,
            use_llm_fallback=self.use_llm_fallback if use_llm is None else use_llm,
            hints=self.spec_hints,
            decision_cache=self.llm_cache,
        )

    # ------------------------------------------------------------------
    # Étape 1 : scrape (réseau)
    # ------------------------------------------------------------------
    def stage_scrape(
        self, scraper: Any, max_pages: int = 10, verbose: bool = True, mark_missing: bool = False
    ) -> List[str]:
        """Fusionne les annonces du scraper dans raw_listings.json. Renvoie leurs clés.

        mark_missing : marque `active=False` les annonces de ce scraper non revues. À n'activer que
                       pour un scraping complet (pas plafonné par max_pages)."""
        name = scraper.__class__.__name__
        if verbose:
            print(f"\n🚀 [scrape] {name}")
        listings = scraper.scrape_base_data(max_pages=max_pages) or []
        now = _now()
        keys: List[str] = []
        new = changed = 0
        for lst in listings:
            data = dataclasses.asdict(lst)
            key = listing_key(lst)
            rec = self.raw.get(key)
            if rec is None:
                rec = {
                    "scraper": name, "first_seen": now, "last_seen": now, "seen_count": 1,
                    "active": True, "details_at": None, "listing": data,
                    "price_history": [[now, data.get("price")]],
                }
                new += 1
            else:
                old_price = rec["listing"].get("price")
                rec["listing"] = _merge_listing(rec["listing"], data)
                rec.update(scraper=name, last_seen=now, seen_count=rec.get("seen_count", 1) + 1, active=True)
                rec.pop("gone_at", None)
                new_price = rec["listing"].get("price")
                if new_price != old_price:
                    rec["price_history"].append([now, new_price])
                    changed += 1
            self.raw.put(key, rec)
            keys.append(key)

        gone = 0
        if mark_missing and listings:
            seen = set(keys)
            for k, rec in self.raw.items():
                if rec.get("scraper") == name and k not in seen and rec.get("active", True):
                    rec["active"], rec["gone_at"] = False, now
                    gone += 1
            self.raw.touch()
        self.raw.save()
        if verbose:
            print(f"   {len(listings)} annonces : {new} nouvelles, {changed} prix modifiés"
                  + (f", {gone} disparues" if mark_missing else "") + f" (total stocké : {len(self.raw)})")
        return keys

    # ------------------------------------------------------------------
    # Étape 2 : match (local, rejouable)
    # ------------------------------------------------------------------
    def stage_match(self, keys: Optional[Iterable[str]] = None, force: bool = False,
                    verbose: bool = True) -> Dict[str, int]:
        """BrandFilter + ModelCatalogMatcher : mémorise (marque_id, modele_id) ou la raison du rejet.
        Sans `force`, ne traite que les annonces jamais appariées."""
        from brandfilter import BrandFilter  # imports tardifs : modules du projet
        from model_catalog_matcher import ModelCatalogMatcher

        todo = [k for k in self._active_keys(keys) if force or k not in self.matches]
        stats = {"processed": len(todo), "matched": 0, "brand_rejected": 0, "model_unknown": 0}
        if not todo:
            return stats

        listings = [listing_from_dict(self.raw.get(k)["listing"]) for k in todo]
        brand_filter = BrandFilter()
        kept = brand_filter.brand_filter(listings)
        enriched = ModelCatalogMatcher(json_path=self.catalog_path).filter_and_enrich_listings(kept)
        by_key = {listing_key(e.listing): e for e in enriched}

        now = _now()
        for k, lst in zip(todo, listings):
            e = by_key.get(k)
            if e is not None:
                self.matches.put(k, {"matched": True, "at": now, "marque_id": e.marque_id, "modele_id": e.modele_id})
                stats["matched"] += 1
            elif not brand_filter.is_brand_allowed(lst.brand):
                self.matches.put(k, {"matched": False, "reason": "brand", "at": now})
                stats["brand_rejected"] += 1
            else:
                self.matches.put(k, {"matched": False, "reason": "model", "at": now})
                stats["model_unknown"] += 1
        self.matches.save()
        if verbose:
            print(f"[match] {stats['processed']} annonces : {stats['matched']} retenues, "
                  f"{stats['brand_rejected']} marques écartées, {stats['model_unknown']} modèles hors référentiel.")
        return stats

    # ------------------------------------------------------------------
    # Étape 3 : details (réseau, une fois par annonce retenue)
    # ------------------------------------------------------------------
    def stage_details(self, keys: Optional[Iterable[str]] = None, refresh: bool = False,
                      only_matched: bool = True, verbose: bool = True,
                      retry_failed: bool = False) -> Dict[str, int]:
        """Lit la fiche de détail des annonces retenues.

        Une fiche n'est marquée lue (`details_at`) que si scrape_details a RÉUSSI. En cas d'erreur, l'échec est
        compté (`details_failures`, `details_error`) et la fiche est retentée aux exécutions suivantes, jusqu'à
        MAX_DETAILS_ATTEMPTS essais (`retry_failed` lève cette limite). Une fiche disparue (DetailsGoneError,
        HTTP 404/410) rend l'annonce inactive."""
        by_name = {s.__class__.__name__: s for s in self.scrapers}
        todo, abandoned = [], 0
        for k in self._active_keys(keys):
            rec = self.raw.get(k)
            if only_matched and not (self.matches.get(k) or {}).get("matched"):
                continue
            if rec.get("details_at") and not refresh:
                continue
            if rec.get("details_failures", 0) >= MAX_DETAILS_ATTEMPTS and not (retry_failed or refresh):
                abandoned += 1
                continue
            todo.append(k)

        stats = {"todo": len(todo), "done": 0, "no_scraper": 0, "errors": 0, "gone": 0, "abandoned": abandoned}
        for i, k in enumerate(todo, 1):
            rec = self.raw.get(k)
            scraper = by_name.get(rec.get("scraper"))
            if scraper is None:
                stats["no_scraper"] += 1
                continue
            listing = listing_from_dict(rec["listing"])
            now = _now()
            try:
                result = scraper.scrape_details(listing)
            except DetailsGoneError as e:
                rec.update(active=False, gone_at=now, details_error=f"disparue : {e}")
                self.raw.touch()
                stats["gone"] += 1
                continue
            except Exception as e:
                rec["details_failures"] = rec.get("details_failures", 0) + 1
                rec["details_error"] = f"{e.__class__.__name__}: {e}"[:300]
                rec["details_last_try"] = now
                self.raw.touch()
                stats["errors"] += 1
                if verbose:
                    print(f"[details] Échec {rec['details_failures']}/{MAX_DETAILS_ATTEMPTS} sur {k} : {rec['details_error']}")
                continue
            updated = getattr(result, "listing", result)  # certains scrapers renvoient un wrapper
            if not isinstance(updated, CarListing):
                updated = listing
            old_price = rec["listing"].get("price")
            rec["listing"] = _merge_listing(rec["listing"], dataclasses.asdict(updated))
            if rec["listing"].get("price") != old_price:
                rec["price_history"].append([now, rec["listing"].get("price")])
            rec["details_at"] = now
            for f in ("details_failures", "details_error", "details_last_try"):
                rec.pop(f, None)
            self.raw.touch()
            stats["done"] += 1
            if i % 20 == 0:
                self.raw.save()  # robuste aux interruptions : l'étape est longue (réseau)
        self.raw.save()
        if verbose:
            print(f"[details] {stats['done']}/{stats['todo']} fiches lues ({stats['errors']} échecs à retenter, "
                  f"{stats['gone']} annonces disparues, {stats['no_scraper']} sans scraper disponible"
                  + (f", {abandoned} abandonnées après {MAX_DETAILS_ATTEMPTS} échecs : --retry-failed-details"
                     if abandoned else "") + ").")
        return stats

    # ------------------------------------------------------------------
    # Étape 4 : resolve (local, rejouable)
    # ------------------------------------------------------------------
    def _rebuild_enriched(self, key: str) -> Optional[EnrichedCarListing]:
        """Reconstitue l'EnrichedCarListing : faits bruts (raw) + marque/modèle (matches) + données du
        catalogue COURANT (variantes, simulations, feeling)."""
        rec, m = self.raw.get(key), self.matches.get(key)
        if not rec or not m or not m.get("matched"):
            return None
        entry = self._catalog().get((m["marque_id"], m["modele_id"]))
        if entry is None:  # modèle retiré du référentiel depuis le matching
            return None
        brand, model = entry
        return EnrichedCarListing(
            listing=listing_from_dict(rec["listing"]),
            marque_id=brand["marque_id"], marque_nom=brand["marque_nom"], feeling_marque=brand["feeling_marque"],
            modele_id=model["id"], nom_canonique=model["nom_canonique"], segment=model["segment"],
            type_ev=model["type_ev"], variantes_candidates=copy.deepcopy(model.get("variantes", [])),
            variante_resolue=None, url=rec["listing"].get("url"),
        )

    @staticmethod
    def _resolution_signature(enriched: EnrichedCarListing, resolver: VariantResolver) -> str:
        """Empreinte de TOUT ce qui peut influencer la résolution (le prix et le km n'y figurent pas ; les
        valeurs de simulation non plus : elles sont relues à chaque fois depuis le catalogue)."""
        l = enriched.listing
        payload = {
            "title": l.title, "brand": l.brand, "model": l.model, "year": l.year, "battery": l.battery_kwh,
            "hp": l.power_hp, "wltp": l.range_wltp_km, "source": l.source, "url": l.url,
            "candidates": sorted(
                [v.get("variante_id"), v.get("nom_variante"), v.get("generation"),
                 v.get("donnees_techniques", {}).get("batterie_utile_kwh"),
                 v.get("donnees_techniques", {}).get("autonomie_wltp_km"),
                 v.get("donnees_techniques", {}).get("puissance_moteur_kw"),
                 v.get("mots_cles")]
                for v in enriched.variantes_candidates
            ),
            "config": resolver.config_signature(),
        }
        return hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()

    @staticmethod
    def _apply_logged(enriched: EnrichedCarListing, logged: Dict[str, Any]) -> None:
        vid = logged.get("variante_id")
        enriched.variantes_possibles = list(logged.get("pool_ids") or ([vid] if vid else []))
        if vid is not None:
            for v in enriched.variantes_candidates:
                if v.get("variante_id") == vid:
                    enriched.variante_resolue = v
                    break

    def _live_urls(self) -> set:
        """URL des annonces qui DOIVENT figurer dans le cache final : actives, appariées, dont le modèle existe
        encore dans le catalogue courant."""
        catalog = self._catalog()
        live = set()
        for k in self._active_keys():
            m = self.matches.get(k)
            if m and m.get("matched") and (m["marque_id"], m["modele_id"]) in catalog:
                live.add(self.raw.get(k)["listing"].get("url"))
        return live

    def _handle_orphans(self, out_cache: Any, prune: bool, verbose: bool) -> int:
        """Fiches du cache final qu'aucune annonce active du magasin brut ne justifie (anciennes exécutions,
        annonces disparues...) : elles ne sont jamais mises à jour et faussent l'estimateur. Les signale, ou les
        retire avec `prune` (après sauvegarde du fichier)."""
        live = self._live_urls()
        orphans = [d for d in out_cache.all_records() if d.get("url") not in live]
        if not orphans:
            return 0
        if prune:
            stamp = f"{self.resolved_db_path}.bak-{datetime.now():%Y%m%d-%H%M%S}"
            backup, n = stamp, 1
            while os.path.exists(backup):  # ne jamais écraser une sauvegarde existante
                backup, n = f"{stamp}-{n}", n + 1
            shutil.copy2(self.resolved_db_path, backup)
            out_cache.db.remove(doc_ids=[d.doc_id for d in orphans])
            if verbose:
                print(f"[resolve] {len(orphans)} fiche(s) orpheline(s) retirée(s) de {self.resolved_db_path} "
                      f"(sauvegarde : {backup}).")
        elif verbose:
            print(f"[resolve] ⚠️ {len(orphans)} fiche(s) orpheline(s) dans {self.resolved_db_path} : aucune annonce "
                  f"active du magasin brut ne les justifie, elles ne sont donc jamais mises à jour. "
                  f"Relancer avec --prune pour les retirer (sauvegarde automatique). Exemples :")
            for d in orphans[:10]:
                lst = d.get("listing") or {}
                print(f"     {lst.get('source')} | {lst.get('title')} | {d.get('url')}")
        return len(orphans)

    def stage_resolve(self, keys: Optional[Iterable[str]] = None, force: bool = False,
                      use_llm: Optional[bool] = None, verbose: bool = True,
                      prune: bool = False) -> List[EnrichedCarListing]:
        """Résout les variantes et écrit le cache final (resolved_db_path). Sans `force`, les annonces
        dont l'empreinte d'entrée est inchangée ne sont pas recalculées (le LLM n'est donc pas rappelé).
        Signale (ou, avec `prune`, retire) les fiches orphelines du cache final."""
        from cache import CarCache

        resolver = self._get_resolver(use_llm)
        out_cache = CarCache(db_path=self.resolved_db_path)
        results: List[EnrichedCarListing] = []
        stats = {"recomputed": 0, "reused": 0, "resolved": 0, "unresolved": 0, "changed": 0}
        changes: List[Tuple[str, Any, Any, str]] = []
        methods: Dict[str, int] = {}

        for k in self._active_keys(keys):
            enriched = self._rebuild_enriched(k)
            if enriched is None:
                continue
            sig = self._resolution_signature(enriched, resolver)
            prev = self.resolution_log.get(k)
            if prev and prev.get("signature") == sig and not force:
                self._apply_logged(enriched, prev)
                stats["reused"] += 1
            else:
                enriched, trace = resolver.resolve_traced(enriched)
                new_id = trace.get("variante_id")
                if trace.get("llm_error"):
                    # Erreur transitoire (Ollama injoignable...) : on ne fige pas ce résultat dans le journal,
                    # l'annonce sera recalculée à la prochaine exécution.
                    stats["unresolved"] += 1
                    out_cache.upsert(listing_data=dataclasses.asdict(enriched))
                    results.append(enriched)
                    continue
                if prev and prev.get("variante_id") != new_id:
                    stats["changed"] += 1
                    changes.append((k, prev.get("variante_id"), new_id, trace["method"]))
                self.resolution_log.put(k, {"signature": sig, "at": datetime.now().isoformat(timespec="seconds"), **trace})
                methods[trace["method"]] = methods.get(trace["method"], 0) + 1
                stats["recomputed"] += 1
            stats["resolved" if enriched.variante_resolue else "unresolved"] += 1

            data = dataclasses.asdict(enriched)
            out_cache.upsert(listing_data=data)
            results.append(enriched)

        self.resolution_log.save()
        self.llm_cache.save()
        self._handle_orphans(out_cache, prune, verbose)
        if verbose:
            print(f"[resolve] {len(results)} annonces : {stats['recomputed']} recalculées "
                  f"({', '.join(f'{m}={n}' for m, n in sorted(methods.items())) or '-'}), "
                  f"{stats['reused']} inchangées ; {stats['resolved']} résolues, {stats['unresolved']} non résolues.")
            if changes:
                print(f"   ↳ {stats['changed']} variante(s) modifiée(s) par rapport à la résolution précédente :")
                for k, old, new, method in changes[:15]:
                    print(f"     {k[-60:]}: {old} -> {new} ({method})")
        return results

    # ------------------------------------------------------------------
    # Orchestration (API historique conservée)
    # ------------------------------------------------------------------
    def run(
        self,
        stages: Sequence[str] = STAGES,
        scrapers: Optional[List[Any]] = None,
        max_pages: int = 10,
        force_match: bool = False,
        force_resolve: bool = False,
        refresh_details: bool = False,
        retry_failed_details: bool = False,
        use_llm: Optional[bool] = None,
        mark_missing: bool = False,
        verbose: bool = True,
        prune: bool = False,
    ) -> List[EnrichedCarListing]:
        """Exécute les étapes demandées. Après un `scrape`, les étapes suivantes ne portent que sur
        les annonces vues dans ce scraping ; sans `scrape` (rejeu), elles portent sur toutes les annonces actives."""
        unknown = set(stages) - set(STAGES)
        if unknown:
            raise ValueError(f"Étapes inconnues : {sorted(unknown)} (valides : {STAGES})")

        keys: Optional[List[str]] = None
        if "scrape" in stages:
            keys = []
            for scraper in (scrapers if scrapers is not None else self.scrapers):
                try:
                    keys += self.stage_scrape(scraper, max_pages, verbose, mark_missing)
                except Exception as e:
                    print(f"❌ Erreur sur {scraper.__class__.__name__} : {e}")
        if "match" in stages:
            self.stage_match(keys, force=force_match, verbose=verbose)
        if "details" in stages:
            self.stage_details(keys, refresh=refresh_details, verbose=verbose, retry_failed=retry_failed_details)
        if "resolve" in stages:
            return self.stage_resolve(keys, force=force_resolve, use_llm=use_llm, verbose=verbose, prune=prune)
        return []

    def run_single(self, scraper: Any, max_pages: int = 10, verbose: bool = True) -> List[Any]:
        """Compatible avec les `__main__` existants des scrapers."""
        return self.run(scrapers=[scraper], max_pages=max_pages, verbose=verbose)

    def run_all(self, max_pages_per_scraper: int = 10, verbose: bool = True) -> List[Any]:
        if not self.scrapers:
            print("⚠️ Aucun scraper actif trouvé.")
            return []
        if verbose:
            print(f"=== EXÉCUTION DU PIPELINE GLOBAL ({len(self.scrapers)} sources) ===")
        cars = self.run(max_pages=max_pages_per_scraper, verbose=verbose)
        if verbose:
            print(f"\n✨ Pipeline global terminé : {len(cars)} véhicules dans {self.resolved_db_path}.")
        return cars

    def resolved_cars(self) -> List[EnrichedCarListing]:
        """Annonces actives déjà résolues, relues depuis les magasins (aucun réseau, aucun LLM)."""
        out = []
        for k in self._active_keys():
            enriched = self._rebuild_enriched(k)
            logged = self.resolution_log.get(k)
            if enriched is not None and logged:
                self._apply_logged(enriched, logged)
                out.append(enriched)
        return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Pipeline voitures électriques (étapes rejouables)")
    ap.add_argument("--only", help=f"étapes à exécuter, séparées par des virgules parmi {STAGES}")
    ap.add_argument("--from", dest="from_stage", choices=STAGES, help="exécute cette étape et les suivantes")
    ap.add_argument("--max-pages", type=int, default=10)
    ap.add_argument("--scraper", help="limite le scraping à ce scraper (nom de classe, sous-chaîne)")
    ap.add_argument("--force-match", action="store_true", help="rejoue le matching sur toutes les annonces")
    ap.add_argument("--force-resolve", action="store_true", help="re-résout toutes les annonces")
    ap.add_argument("--refresh-details", action="store_true", help="relit aussi les fiches déjà lues")
    ap.add_argument("--retry-failed-details", action="store_true",
                    help=f"retente aussi les fiches abandonnées après {MAX_DETAILS_ATTEMPTS} échecs")
    ap.add_argument("--headless", action="store_true", help="navigateur Playwright invisible (exécution sans surveillance)")
    ap.add_argument("--mark-missing", action="store_true", help="marque inactives les annonces disparues")
    ap.add_argument("--llm", action="store_true", help="active l'arbitrage LLM en dernier recours (désactivé par défaut)")
    ap.add_argument("--no-llm", action="store_true", help=argparse.SUPPRESS)  # compatibilité : c'est le défaut
    ap.add_argument("--prune", action="store_true",
                    help="retire du cache final les fiches orphelines (sauvegarde automatique)")
    ap.add_argument("--data-dir", default=".")
    ap.add_argument("--catalog", default="models.json", help="référentiel (avec les simulations 1000 km)")
    ap.add_argument("--db", default="resolved_cars.json", help="cache final lu par MarketEstimator")
    args = ap.parse_args()

    if args.only:
        stages = tuple(s.strip() for s in args.only.split(","))
    elif args.from_stage:
        stages = STAGES[STAGES.index(args.from_stage):]
    else:
        stages = STAGES

    helper = None
    if "scrape" in stages or "details" in stages:
        from carhelper import CarHelper
        helper = CarHelper(enrich_details=True, headless=args.headless)

    pipe = CarPipeline(helper=helper, resolved_db_path=args.db, data_dir=args.data_dir,
                       use_llm_fallback=args.llm and not args.no_llm, catalog_path=args.catalog,
                       scrapers=None if helper else [])
    selected = None
    if args.scraper:
        selected = [s for s in pipe.scrapers if args.scraper.lower() in s.__class__.__name__.lower()]
    pipe.run(stages=stages, scrapers=selected, max_pages=args.max_pages, force_match=args.force_match,
             force_resolve=args.force_resolve, refresh_details=args.refresh_details,
             retry_failed_details=args.retry_failed_details,
             mark_missing=args.mark_missing, use_llm=args.llm and not args.no_llm, prune=args.prune)


if __name__ == "__main__":
    main()
