import copy
import dataclasses
import json
import os
import re
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional
import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
import pandas as pd

import paths
from references import MissingReferenceError, load_catalog, resolve_references


class MarketEstimator:
  # Les voitures de référence ne sont plus figées ici : elles sont choisies par l'utilisateur
  # (data/references.json, batch\\set_reference_variant.bat) et complétées depuis models.json (voir references.py).
  def __init__(
      self,
      output_filename: str = paths.EXPORT_BASENAME,
      current_year: Optional[int] = None,
      brands_json_path: str | Path = paths.BRANDS,
      roadtrip_ok_lost_min: float = 30.0,
      roadtrip_long_lost_min: float = 60.0,
      roadtrip_max_lost_min: float = 90.0,
      roadtrip_min_range_km: float = 120.0,
      roadtrip_gate_segments: tuple = ("B",),
      ev_exponent: float = 0.65,
      impossible_sim_min: float = 1200.0,
      soh_loss_per_year: float = 2.0,
      soh_expected_floor: float = 70.0,
      soh_factor_max: float = 1.10,
      platform_factors: Optional[Dict[str, float]] = None,
      passive_cooling_factor: float = 0.95,
      catalog_path: str | Path = paths.CATALOG,
      references_path: str | Path = paths.REFERENCES,
  ):
    """
    Barrière road trip (1000 km, rythme habituel) : ne retire que les voitures pour lesquelles
    c'est impraticable, pas celles qui sont simplement moins bonnes que la référence.
    Paliers du temps perdu vs thermique (minutes) :
        < roadtrip_ok_lost_min (30)                -> "OK"
        < roadtrip_long_lost_min (60)              -> "Long"
        <= roadtrip_max_lost_min (90)              -> "Très long"
        > roadtrip_max_lost_min                    -> "Rédhibitoire"
      roadtrip_min_range_km  : autonomie autoroute (100 % -> réserve) minimale ; en dessous -> "Impossible"
                               (de même qu'une simulation irréalisable).
      roadtrip_gate_segments : segments où "Rédhibitoire" / "Impossible" exclut la voiture. Les autres (ex. A) sont
                               conservés et seulement étiquetés.
      ev_exponent            : poids du temps de trajet dans la valorisation ((ref/voiture) ** exposant).
      impossible_sim_min     : temps 1000 km RETENU pour une voiture incapable du trajet (simulation
                               « irréalisable ») : plus pénalisant que toute voiture réalisable du catalogue
                               (max observé ≈ 1036 min, Twingo ZE). Avant : coefficient neutre 1,0.
      soh_loss_per_year      : perte de SoH « normale » par an, déjà contenue dans la décote liée à l'âge.
      soh_expected_floor     : SoH attendu minimal, quel que soit l'âge.
      soh_factor_max         : plafond du bonus SoH (un SoH 100 % déclaré sur une vieille voiture est
                               souvent une valeur par défaut du vendeur).
      platform_factors       : qualité selon le type de plateforme de la variante (champ `plateforme` de models.json) :
                               dediee 1,00 ; multi_energie 0,95 ; convertie 0,90 (compromis d'implantation et de
                               refroidissement de la batterie, vieillissement plus rapide).
      passive_cooling_factor : multiplicateur supplémentaire si la batterie n'a pas de refroidissement actif ("air passif").
    Coef. Plateforme = qualité de la plateforme de la voiture / qualité de celle de la référence du segment
    (comme le coef. marque) ; plateforme inconnue -> 1,0.
    Coefficient SoH : SoH mesuré / SoH attendu pour l'âge. Seul l'écart à l'usure normale compte (l'usure
    normale est déjà dans la décote liée à l'âge) ; SoH inconnu -> 1,0.
    """
    # Un simple nom de fichier est placé dans output/ ; un chemin explicite est respecté.
    out = Path(output_filename)
    self.output_filename = str(out if out.parent != Path(".") else paths.OUTPUT_DIR / out)
    today = date.today()
    self.current_year = current_year or today.year
    # Date de référence en années fractionnaires (ex. 23/11/2026 -> 2026.89) ; si `current_year` est imposé,
    # on se place au milieu de cette année-là.
    self.reference_year_frac = (
        today.year + (today.timetuple().tm_yday - 0.5) / 365.25
        if current_year is None or current_year == today.year else current_year + 0.5
    )
    self.roadtrip_max_lost_min = roadtrip_max_lost_min
    self.impossible_sim_min = impossible_sim_min
    self.soh_loss_per_year = soh_loss_per_year
    self.soh_expected_floor = soh_expected_floor
    self.soh_factor_max = soh_factor_max
    self.platform_factors = platform_factors or {"dediee": 1.00, "multi_energie": 0.95, "convertie": 0.90}
    self.passive_cooling_factor = passive_cooling_factor
    self.roadtrip_ok_lost_min = roadtrip_ok_lost_min
    self.roadtrip_long_lost_min = roadtrip_long_lost_min
    self.roadtrip_min_range_km = roadtrip_min_range_km
    self.roadtrip_gate_segments = tuple(str(s).upper() for s in roadtrip_gate_segments)
    self.ev_exponent = ev_exponent
    self.brands_json_path = Path(brands_json_path)
    self.brand_feelings: Dict[str, float] = self._load_brand_feelings()
    # Lève MissingReferenceError tant qu'une référence de segment n'est pas choisie : pas d'estimation sans elle.
    self.REFERENCE_BENCHMARKS = self._load_benchmarks(Path(catalog_path), Path(references_path))

  @staticmethod
  def _normalize_brand(name: Any) -> str:
    """Normalise le nom ou l'identifiant de la marque (minuscules, sans accents, alphanumérique)."""
    if not name:
      return ""
    nfkd = unicodedata.normalize("NFKD", str(name))
    without_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", without_accents.lower())

  def _load_brand_feelings(self) -> Dict[str, float]:
    """Charge le score feeling_global.score depuis le JSON des marques (indexé par id et nom normalisé)."""
    mapping: Dict[str, float] = {}
    if not self.brands_json_path.exists():
      print(
          f"⚠️ Fichier marques introuvable : {self.brands_json_path}."
          " Utilisation des valeurs par défaut."
      )
      return mapping

    with self.brands_json_path.open("r", encoding="utf-8") as f:
      data = json.load(f)

    for marque in data.get("marques", []):
      score = marque.get("feeling_global", {}).get("score")
      if score is not None:
        val = float(score)
        for key in (marque.get("id"), marque.get("nom")):
          norm_k = self._normalize_brand(key)
          if norm_k:
            mapping[norm_k] = val
    return mapping

  def _load_benchmarks(self, catalog_path: Path, references_path: Path) -> Dict[str, Dict[str, Any]]:
    """Construit les références par segment : prix neuf choisi + données relues dans le catalogue."""
    benchmarks = {}
    for seg, ref in resolve_references(load_catalog(catalog_path), references_path).items():
      b, m, v = ref["marque"], ref["modele"], ref["variante"]
      tech, sim = v.get("donnees_techniques", {}), v.get("simulation_humaine_1000km", {})
      benchmarks[seg] = {
          "ref_name": f"{b['marque_nom']} {m['nom_canonique']} — {v['nom_variante']}",
          "real_new_price": ref["prix_neuf"],
          "wltp_km": tech.get("autonomie_wltp_km"),
          "dc_kw": tech.get("puissance_dc_max_kw"),
          "ref_feeling": self._get_brand_feeling(b.get("marque_id"), b.get("marque_nom"), default_fallback=80.0),
          "ref_sim_min": float(sim["temps_minutes"]),
          "ref_plateforme": v.get("plateforme") or {},
      }
    return benchmarks

  def _get_brand_feeling(
      self, marque_id: Any, marque_nom: Any, default_fallback: float = 80.0
  ) -> float:
    """Récupère le score de feeling à jour depuis le JSON en ignorant la valeur obsolète du cache/scraping."""
    for candidate in (marque_id, marque_nom):
      norm_c = self._normalize_brand(candidate)
      if norm_c and norm_c in self.brand_feelings:
        return self.brand_feelings[norm_c]
    return default_fallback

  @staticmethod
  def _parse_year(raw_year: Any) -> Optional[int]:
    """Extrait proprement l'année en int (gère '06-2021', '2021', etc.)."""
    if raw_year is None:
      return None
    if isinstance(raw_year, (int, float)):
      return int(raw_year)
    if isinstance(raw_year, str):
      match = re.search(r"\b(20\d{2}|19\d{2})\b", raw_year)
      if match:
        return int(match.group(1))
      try:
        return int(float(raw_year))
      except ValueError:
        return None
    return None

  @staticmethod
  def _parse_loose_number(raw: Any) -> Optional[float]:
    """Convertit '71.712 km', '71712.0', '91,5 %', 91.2 ... en nombre (séparateurs de milliers gérés)."""
    if raw is None:
      return None
    if isinstance(raw, bool):
      return None
    if isinstance(raw, (int, float)):
      return None if raw != raw else float(raw)
    s = re.sub(r"[^\d.,]", "", str(raw).replace("\xa0", " ").replace("\u202f", " "))
    if not s:
      return None
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", s):  # séparateurs de milliers
      s = re.sub(r"[.,]", "", s)
    else:
      s = s.replace(",", ".")
    try:
      return float(s)
    except ValueError:
      return None

  @classmethod
  def _parse_mileage(cls, raw_mileage: Any) -> Optional[int]:
    """Nettoie et convertit le kilométrage en int."""
    val = cls._parse_loose_number(raw_mileage)
    return int(val) if val is not None and 0 <= val <= 1_000_000 else None

  @classmethod
  def _parse_soh(cls, raw_soh: Any) -> Optional[int]:
    """Nettoie et convertit le SoH en int (0.92 -> 92, '91,5 %' -> 91 ; hors 20-100 -> None)."""
    val = cls._parse_loose_number(raw_soh)
    if val is None:
      return None
    if 0 < val <= 1.0:
      val *= 100.0
    return int(val) if 20 <= val <= 100 else None

  def _flatten_record(self, item: Any) -> dict:
    """Aplatit un enregistrement qu'il s'agisse d'un objet dataclass ou d'un dictionnaire imbriqué (cache JSON)."""
    if hasattr(item, "asdict"):
      d = item.asdict()
    elif hasattr(item, "__dataclass_fields__"):
      d = dataclasses.asdict(item)
    elif isinstance(item, dict):
      d = item
    else:
      return {}

    marque_id = d.get("marque_id")
    marque_nom = d.get("marque_nom")
    segment = str(d.get("segment", "B")).upper().strip()
    bench = self.REFERENCE_BENCHMARKS.get(segment, self.REFERENCE_BENCHMARKS["B"])

    # Ignore d.get("feeling_marque") issu du cache/scraping et lit directement depuis le JSON recalibré
    feeling_marque = self._get_brand_feeling(
        marque_id=marque_id,
        marque_nom=marque_nom,
        default_fallback=bench["ref_feeling"],
    )

    flat = {
        "marque_id": marque_id,
        "marque_nom": marque_nom,
        "modele_id": d.get("modele_id"),
        "norm_model": d.get("nom_canonique") or d.get("modele_id"),
        "norm_brand": marque_nom,
        "segment": segment,
        "feeling_marque": feeling_marque,
    }

    # Annonce imbriquée sous "listing" (dict ou objet) ; sinon enregistrement plat : les champs sont dans d.
    listing = d.get("listing")
    if listing is None:
      listing = d

    def lget(key: str, *alt: str) -> Any:
      for k in (key, *alt):
        v = listing.get(k) if isinstance(listing, dict) else getattr(listing, k, None)
        if v is not None:
          return v
      return None

    raw_price = lget("price", "prix")
    flat["source"] = lget("source")
    flat["url"] = lget("url")
    flat["listing_wltp"] = lget("range_wltp_km")
    flat["listing_batt"] = lget("battery_kwh")

    price = self._parse_loose_number(raw_price)
    flat["price"] = price if price and price > 0 else None
    flat["mileage_km"] = self._parse_mileage(lget("mileage_km"))
    flat["year"] = self._parse_year(lget("year"))
    flat["soh_percent"] = self._parse_soh(lget("soh_percent"))

    # Variante non résolue : au lieu de prendre arbitrairement candidates[0], on valorise la voiture avec la
    # variante la PLUS DÉFAVORABLE encore possible (temps 1000 km le plus long) et on garde la plus favorable
    # pour indiquer le potentiel (« Indice si meilleure variante »).
    var_obj = d.get("variante_resolue")
    flat["variante_resolue_flag"] = isinstance(var_obj, dict)
    best_obj = var_obj if isinstance(var_obj, dict) else None
    flat["nb_variantes_possibles"] = 1 if isinstance(var_obj, dict) else 0
    if not isinstance(var_obj, dict):
      candidates = [c for c in (d.get("variantes_candidates") or []) if isinstance(c, dict)]
      possible_ids = set(d.get("variantes_possibles") or [])
      possibles = [c for c in candidates if c.get("variante_id") in possible_ids] or candidates
      if possibles:
        var_obj = max(possibles, key=self._sim_cost)
        best_obj = min(possibles, key=self._sim_cost)
        flat["nb_variantes_possibles"] = len(possibles)

    sim = {}
    if isinstance(var_obj, dict):
      tech = var_obj.get("donnees_techniques", {})
      flat["norm_battery_kwh"] = (
          tech.get("batterie_utile_kwh") or flat["listing_batt"]
      )
      flat["wltp_km"] = tech.get("autonomie_wltp_km") or flat["listing_wltp"]
      flat["dc_kw"] = tech.get("puissance_dc_max_kw")
      flat["variante_nom"] = var_obj.get("nom_variante")
      plat = var_obj.get("plateforme") or {}
      flat["plateforme_type"] = plat.get("type")
      flat["plateforme_label"] = self._platform_label(plat)
      flat["platform_quality"] = self._platform_quality(plat)
      if not flat["variante_resolue_flag"] and flat["nb_variantes_possibles"] > 1:
        flat["variante_nom"] = (
            f"? {flat['nb_variantes_possibles']} possibles, pire cas : {var_obj.get('nom_variante')}"
        )
      sim = var_obj.get("simulation_humaine_1000km") or {}
    else:
      flat["norm_battery_kwh"] = flat["listing_batt"]
      flat["wltp_km"] = flat["listing_wltp"]
      flat["dc_kw"] = None
      flat["variante_nom"] = None

    # Simulation humaine 1000 km (temps total, temps perdu vs thermique, autonomie autoroute)
    flat["sim_humaine_1000km_min"] = sim.get("temps_minutes")
    flat["temps_perdu_min"] = sim.get("temps_perdu_min")
    flat["autonomie_autoroute_km"] = sim.get("autonomie_autoroute_km")
    flat["roadtrip_realisable"] = sim.get("realisable")
    best_sim = (best_obj or {}).get("simulation_humaine_1000km") or sim
    flat["best_sim_min"] = best_sim.get("temps_minutes")
    flat["best_temps_perdu_min"] = best_sim.get("temps_perdu_min")
    flat["best_autonomie_autoroute_km"] = best_sim.get("autonomie_autoroute_km")
    flat["best_roadtrip_realisable"] = best_sim.get("realisable")

    return flat

  PLATFORM_LABELS = {"dediee": "Dédiée EV", "multi_energie": "Multi-énergie", "convertie": "Thermique convertie"}

  def _platform_quality(self, plat: Dict[str, Any]) -> Optional[float]:
    """Qualité absolue d'une plateforme (None si type inconnu)."""
    q = self.platform_factors.get((plat or {}).get("type"))
    if q is None:
      return None
    if (plat.get("refroidissement_batterie") or "").lower() == "air passif":
      q *= self.passive_cooling_factor
    return q

  def _platform_label(self, plat: Dict[str, Any]) -> Optional[str]:
    if not plat or not plat.get("type"):
      return None
    label = self.PLATFORM_LABELS.get(plat["type"], plat["type"])
    if (plat.get("refroidissement_batterie") or "").lower() == "air passif":
      label += ", air passif"
    if plat.get("confiance") == "faible":
      label += " (?)"
    return label

  @staticmethod
  def _sim_cost(variant: Dict[str, Any]) -> float:
    """Temps 1000 km d'une variante (infini si irréalisable ou inconnu) : sert à trouver le pire / meilleur cas."""
    sim = variant.get("simulation_humaine_1000km") or {}
    t = sim.get("temps_minutes")
    return float("inf") if not sim.get("realisable") or t is None else float(t)

  def _ev_factor(self, sim_min: Any, ref_sim_min: float, realisable: Any = None) -> Optional[float]:
    """(temps référence / temps voiture) ** exposant.
    Simulation irréalisable -> pénalité (impossible_sim_min) ; simulation absente -> None (pas d'estimation),
    au lieu de l'ancien 1,0 qui valait autant que la référence."""
    if realisable is not None and not pd.isna(realisable) and not bool(realisable):
      return round((ref_sim_min / self.impossible_sim_min) ** self.ev_exponent, 3)
    car_sim_min = pd.to_numeric(sim_min, errors="coerce")
    if pd.notnull(car_sim_min) and car_sim_min > 0:
      return round((ref_sim_min / car_sim_min) ** self.ev_exponent, 3)
    return None

  def _soh_factor(self, soh: Any, age: float) -> float:
    """SoH mesuré rapporté au SoH attendu pour l'âge (l'usure normale est déjà dans la décote d'âge)."""
    soh = pd.to_numeric(soh, errors="coerce")
    if pd.isnull(soh):
      return 1.0
    expected = max(self.soh_expected_floor, 100.0 - self.soh_loss_per_year * age)
    return round(min(self.soh_factor_max, float(soh) / expected), 3)

  ROADTRIP_EXCLUDED = ("Rédhibitoire", "Impossible")

  def _roadtrip_status(self, realisable: Any, lost: Any, rng: Any) -> str:
    lost = pd.to_numeric(lost, errors="coerce")
    rng = pd.to_numeric(rng, errors="coerce")
    if realisable is not None and not pd.isna(realisable) and not bool(realisable):
      return "Impossible"
    if pd.isnull(lost):
      return "Inconnu"
    if pd.notnull(rng) and rng < self.roadtrip_min_range_km:
      return "Impossible"
    if lost < self.roadtrip_ok_lost_min:
      return "OK"
    if lost < self.roadtrip_long_lost_min:
      return "Long"
    if lost <= self.roadtrip_max_lost_min:
      return "Très long"
    return "Rédhibitoire"

  @staticmethod
  def _age_depreciation(age: float) -> float:
    """Décote liée à l'âge, CONTINUE en fonction de l'âge (en années fractionnaires) :
      * 10 % dès l'immatriculation, puis +5 %/an jusqu'à 15 % à 1 an ;
      * +8 %/an de 1 à 5 ans (47 % à 5 ans) ;
      * +5 %/an au-delà.
    Aux âges entiers 1..n on retrouve exactement l'ancien barème (15 %, 23 %, 31 %...) ; seule la 1re année
    change : avant, 0 an et 1 an donnaient tous deux 15 %."""
    a = max(0.0, age)
    if a <= 1.0:
      return 0.10 + 0.05 * a
    if a <= 5.0:
      return 0.15 + 0.08 * (a - 1.0)
    return 0.47 + 0.05 * (a - 5.0)

  def _compute_valuation(self, row: pd.Series) -> pd.Series:
    seg = row.get("segment", "B")
    bench = self.REFERENCE_BENCHMARKS.get(seg, self.REFERENCE_BENCHMARKS["B"])

    ref_price = bench["real_new_price"]
    ref_feeling = bench["ref_feeling"]
    ref_sim_min = bench["ref_sim_min"]

    # Coefficient électrique basé sur la simulation humaine 1000 km (pire cas si la variante est incertaine)
    ev_factor = self._ev_factor(
        row.get("sim_humaine_1000km_min"), ref_sim_min, row.get("roadtrip_realisable"))
    ev_factor_best = self._ev_factor(
        row.get("best_sim_min"), ref_sim_min, row.get("best_roadtrip_realisable"))

    year = pd.to_numeric(row.get("year"), errors="coerce")
    mileage = pd.to_numeric(row.get("mileage_km"), errors="coerce")

    # Âge en années fractionnaires : immatriculation supposée au milieu de l'année (seule l'année est connue).
    age = max(0.0, self.reference_year_frac - (int(year) + 0.5)) if pd.notnull(year) else 1.0
    km = float(mileage) if pd.notnull(mileage) else (age * 15000.0)

    # 1. Décote liée à l'âge (continue, voir _age_depreciation)
    age_depreciation = self._age_depreciation(age)

    # 2. Ajustement kilométrique basé sur l'écart par rapport à la normale (15 000 km/an)
    expected_km = age * 15000.0
    km_diff = km - expected_km

    if km_diff > 0:
      km_depreciation = (km_diff / 10000.0) * 0.01
    else:
      km_depreciation = max(-0.05, (km_diff / 10000.0) * 0.005)

    # 3. Plafonnement global à 90% (garde 10% de valeur résiduelle minimum)
    total_depreciation = max(0.0, min(0.90, age_depreciation + km_depreciation))
    usage_factor = round(1.0 - total_depreciation, 3)

    # Facteur de marque recalculé depuis le JSON mis à jour, relatif au modèle de référence du segment
    feeling = pd.to_numeric(row.get("feeling_marque"), errors="coerce")
    feeling_val = float(feeling) if pd.notnull(feeling) else ref_feeling
    brand_factor = round(1.0 + (feeling_val - ref_feeling) / 100.0, 3)

    # Facteur SoH : écart à l'usure normale pour l'âge (voir _soh_factor)
    soh = pd.to_numeric(row.get("soh_percent"), errors="coerce")
    soh_factor = self._soh_factor(soh, age)

    # Facteur plateforme, relatif à la plateforme de la référence du segment (voir __init__)
    car_q = pd.to_numeric(row.get("platform_quality"), errors="coerce")
    ref_q = self._platform_quality(bench.get("ref_plateforme") or {})
    platform_factor = round(float(car_q) / ref_q, 3) if pd.notnull(car_q) and ref_q else 1.0

    def estimate(ev: Optional[float]) -> Optional[float]:
      return None if ev is None else round(
          ref_price * ev * usage_factor * brand_factor * platform_factor * soh_factor, 0)

    estimated_price = estimate(ev_factor)
    estimated_price_best = estimate(ev_factor_best)

    asked_price = pd.to_numeric(row.get("price"), errors="coerce")
    has_price = pd.notnull(asked_price) and asked_price > 0
    opp_ratio = round(estimated_price / asked_price, 3) if has_price and estimated_price is not None else None
    opp_ratio_best = (round(estimated_price_best / asked_price, 3)
                      if has_price and estimated_price_best is not None else None)
    uncertain = not bool(row.get("variante_resolue_flag")) and (row.get("nb_variantes_possibles") or 0) > 1

    # --- Aptitude au road trip (barrière absolue, pas une comparaison à la référence) ---
    # Variante incertaine : statut du pire cas, mais la barrière n'exclut la voiture que si même la
    # meilleure variante possible est exclue (statut affiché avec « ? » sinon, ex. « Rédhibitoire ? »).
    roadtrip = self._roadtrip_status(
        row.get("roadtrip_realisable"), row.get("temps_perdu_min"), row.get("autonomie_autoroute_km"))
    roadtrip_best = self._roadtrip_status(
        row.get("best_roadtrip_realisable"), row.get("best_temps_perdu_min"),
        row.get("best_autonomie_autoroute_km"))
    if roadtrip in self.ROADTRIP_EXCLUDED and roadtrip_best not in self.ROADTRIP_EXCLUDED:
      roadtrip = f"{roadtrip} ?"

    # --- Fiabilité de l'estimation : variante résolue, SoH et kilométrage réellement connus ---
    flag = row.get("variante_resolue_flag")
    known = int(bool(flag) and not pd.isna(flag)) + int(pd.notnull(soh)) + int(pd.notnull(mileage))
    reliability = {3: "Haute", 2: "Moyenne"}.get(known, "Faible") if ev_factor is not None else "Aucune"

    return pd.Series({
        "roadtrip_status": roadtrip,
        "fiabilite": reliability,
        "ref_model_name": bench["ref_name"],
        "ref_new_price": ref_price,
        "ev_factor": ev_factor,
        "usage_factor": usage_factor,
        "brand_factor": brand_factor,
        "platform_factor": platform_factor,
        "soh_factor": soh_factor,
        "estimated_price": estimated_price,
        "opportunity_ratio": opp_ratio,
        "opportunity_ratio_best": opp_ratio_best if uncertain else None,
    })

  def process_and_export(self, cars: list, verbose: bool = True) -> pd.DataFrame:
    if not cars:
      if verbose:
        print("⚠️ Aucun véhicule à analyser.")
      return pd.DataFrame()

    records = [self._flatten_record(car) for car in cars]
    df = pd.DataFrame(records)

    df["price"] = pd.to_numeric(df.get("price"), errors="coerce")
    df["mileage_km"] = pd.to_numeric(df.get("mileage_km"), errors="coerce")
    df["year"] = pd.to_numeric(df.get("year"), errors="coerce")
    df["soh_percent"] = pd.to_numeric(df.get("soh_percent"), errors="coerce")

    if verbose:
      print(f"\n📋 === STATUT DE LA COLLECTE ({len(df)} véhicules analysés) ===")
      summary_grp = (
          df.groupby(["norm_brand", "norm_model"])
          .agg(
              Total=("price", "count"),
              Prix_Moyen=("price", "mean"),
              Prix_Min=("price", "min"),
              Prix_Max=("price", "max"),
          )
          .reset_index()
      )
      summary_grp["Prix_Moyen"] = summary_grp["Prix_Moyen"].round(0)
      print(summary_grp.to_string(index=False))
      print("=" * 60)

    val_df = df.apply(self._compute_valuation, axis=1)
    df = pd.concat([df, val_df], axis=1)

    # Barrière road trip : n'exclut que l'impossible, et seulement dans les segments concernés
    # (par défaut B). Les autres segments (A) sont conservés et simplement étiquetés.
    impossible = df["roadtrip_status"].isin(self.ROADTRIP_EXCLUDED)
    gated = impossible & df["segment"].isin(self.roadtrip_gate_segments)
    kept_flagged = int((impossible & ~gated).sum())
    unknown = int((df["roadtrip_status"] == "Inconnu").sum())
    df = df[~gated].copy()
    if verbose:
      if int(gated.sum()):
        print(
            f"🧹 {int(gated.sum())} véhicule(s) exclu(s) : road trip impossible"
            f" (> {self.roadtrip_max_lost_min:.0f} min perdues, trajet irréalisable ou autonomie autoroute"
            f" < {self.roadtrip_min_range_km:.0f} km) en segment {'/'.join(self.roadtrip_gate_segments)}"
            f" (Reste : {len(df)} véhicules)."
        )
      if kept_flagged:
        print(f"ℹ️ {kept_flagged} véhicule(s) 'Rédhibitoire' / 'Impossible' conservés (segment hors barrière).")
      if unknown:
        print(f"ℹ️ {unknown} véhicule(s) sans simulation road trip (statut 'Inconnu', conservés).")

    df = df.sort_values(
        by=["opportunity_ratio", "price"], ascending=[False, True], na_position="last"
    )

    key_columns = [
        "norm_brand",
        "norm_model",
        "variante_nom",
        "segment",
        "year",
        "mileage_km",
        "soh_percent",
        "norm_battery_kwh",
        "wltp_km",
        "dc_kw",
        "sim_humaine_1000km_min",
        "temps_perdu_min",
        "autonomie_autoroute_km",
        "roadtrip_status",
        "feeling_marque",
        "ref_new_price",
        "ev_factor",
        "usage_factor",
        "brand_factor",
        "plateforme_label",
        "platform_factor",
        "soh_factor",
        "estimated_price",
        "price",
        "opportunity_ratio",
        "opportunity_ratio_best",
        "fiabilite",
        "source",
        "url",
    ]
    available_cols = [c for c in key_columns if c in df.columns]
    export_df = df[available_cols].copy()

    rename_map = {
        "norm_brand": "Marque",
        "norm_model": "Modèle",
        "variante_nom": "Variante",
        "segment": "Seg.",
        "year": "Année",
        "mileage_km": "Kilométrage",
        "soh_percent": "SoH (%)",
        "norm_battery_kwh": "Batterie (kWh)",
        "wltp_km": "WLTP (km)",
        "dc_kw": "Charge DC (kW)",
        "sim_humaine_1000km_min": "Temps 1000km (min)",
        "temps_perdu_min": "Temps perdu (min)",
        "autonomie_autoroute_km": "Autonomie autoroute (km)",
        "roadtrip_status": "Road trip",
        "feeling_marque": "Confiance Marque",
        "ref_new_price": "Prix Neuf Réf. (€)",
        "ev_factor": "Coef. Électrique",
        "usage_factor": "Coef. Âge/Km",
        "brand_factor": "Coef. Marque",
        "plateforme_label": "Plateforme",
        "platform_factor": "Coef. Plateforme",
        "soh_factor": "Coef. SoH",
        "estimated_price": "Prix Estimé (€)",
        "price": "Prix Demandé (€)",
        "opportunity_ratio": "Indice Opportunité",
        "opportunity_ratio_best": "Indice si meilleure variante",
        "fiabilite": "Fiabilité",
        "source": "Source",
        "url": "Lien Annonce",
    }
    export_df = export_df.rename(columns=rename_map)

    Path(self.output_filename).parent.mkdir(parents=True, exist_ok=True)
    excel_path = f"{self.output_filename}.xlsx"
    csv_path = f"{self.output_filename}.csv"
    export_df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Analyse Opportunités EV"
    ws.views.sheetView[0].showGridLines = True

    header_fill = PatternFill(
        start_color="1F4E78", end_color="1F4E78", fill_type="solid"
    )
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    regular_font = Font(name="Calibri", size=11)
    bold_font = Font(name="Calibri", size=11, bold=True)
    link_font = Font(
        name="Calibri", size=11, color="0563C1", underline="single"
    )
    zebra_fill = PatternFill(
        start_color="F9FAFB", end_color="F9FAFB", fill_type="solid"
    )
    white_fill = PatternFill(
        start_color="FFFFFF", end_color="FFFFFF", fill_type="solid"
    )
    border_light = Border(
        left=Side(style="thin", color="D9D9D9"),
        right=Side(style="thin", color="D9D9D9"),
        top=Side(style="thin", color="D9D9D9"),
        bottom=Side(style="thin", color="D9D9D9"),
    )

    headers = list(export_df.columns)
    ws.append(headers)

    for col_num, header in enumerate(headers, 1):
      cell = ws.cell(row=1, column=col_num)
      cell.fill = header_fill
      cell.font = header_font
      cell.alignment = Alignment(
          horizontal="center", vertical="center", wrap_text=True
      )
      cell.border = border_light
    ws.row_dimensions[1].height = 30

    for row_idx, row in enumerate(export_df.itertuples(index=False), 2):
      row_data = [None if isinstance(v, float) and v != v else v for v in row]  # NaN -> cellule vide
      ws.append(row_data)
      ws.row_dimensions[row_idx].height = 22
      row_fill = zebra_fill if (row_idx % 2 == 0) else white_fill

      for col_idx, val in enumerate(row_data, 1):
        cell = ws.cell(row=row_idx, column=col_idx)
        cell.font = regular_font
        cell.fill = row_fill
        cell.border = border_light

        col_name = headers[col_idx - 1]

        if col_name in [
            "Prix Neuf Réf. (€)",
            "Prix Estimé (€)",
            "Prix Demandé (€)",
        ]:
          cell.number_format = '#,##0 "€"'
          cell.alignment = Alignment(horizontal="right", vertical="center")
          if col_name in ["Prix Estimé (€)", "Prix Demandé (€)"]:
            cell.font = bold_font
        elif col_name in [
            "Kilométrage",
            "WLTP (km)",
            "Charge DC (kW)",
            "Temps 1000km (min)",
            "Temps perdu (min)",
            "Autonomie autoroute (km)",
        ]:
          cell.number_format = "#,##0"
          cell.alignment = Alignment(horizontal="right", vertical="center")
        elif col_name in ["Année", "Seg.", "SoH (%)"]:
          cell.alignment = Alignment(horizontal="center", vertical="center")
          if col_name == "SoH (%)" and pd.notnull(val):
            cell.number_format = '#,##0"%"'
        elif col_name in ["Batterie (kWh)", "Confiance Marque"]:
          cell.number_format = "0.0"
          cell.alignment = Alignment(horizontal="right", vertical="center")
        elif col_name in [
            "Coef. Électrique",
            "Coef. Âge/Km",
            "Coef. Marque",
            "Coef. Plateforme",
            "Coef. SoH",
        ]:
          cell.number_format = "0.000"
          cell.alignment = Alignment(horizontal="right", vertical="center")
        elif col_name == "Indice si meilleure variante":
          cell.number_format = "0.00"
          cell.alignment = Alignment(horizontal="center", vertical="center")
        elif col_name == "Indice Opportunité":
          cell.number_format = "0.00"
          cell.font = bold_font
          cell.alignment = Alignment(horizontal="center", vertical="center")
          if isinstance(val, (int, float)):
            if val >= 1.00:
              cell.fill = PatternFill(
                  start_color="C6EFCE", end_color="C6EFCE", fill_type="solid"
              )
              cell.font = Font(
                  name="Calibri", size=11, bold=True, color="006100"
              )
            elif val >= 0.90:
              cell.fill = PatternFill(
                  start_color="FFEB9C", end_color="FFEB9C", fill_type="solid"
              )
              cell.font = Font(
                  name="Calibri", size=11, bold=True, color="9C6500"
              )
            else:
              cell.fill = PatternFill(
                  start_color="FFC7CE", end_color="FFC7CE", fill_type="solid"
              )
              cell.font = Font(
                  name="Calibri", size=11, bold=True, color="9C0006"
              )
        elif col_name in ("Road trip", "Fiabilité"):
          palette = {
              "OK": ("C6EFCE", "006100"), "Haute": ("C6EFCE", "006100"),
              "Long": ("FFEB9C", "9C6500"), "Moyenne": ("FFEB9C", "9C6500"),
              "Très long": ("F8CBAD", "833C0B"),
              "Rédhibitoire": ("FFC7CE", "9C0006"), "Rédhibitoire ?": ("FCE4D6", "9C0006"),
              "Impossible": ("FFC7CE", "9C0006"), "Faible": ("FFC7CE", "9C0006"),
              "Aucune": ("E7E6E6", "595959"),
              "Impossible ?": ("FCE4D6", "9C0006"),
              "Inconnu": ("E7E6E6", "595959"),
          }
          bg, fg = palette.get(str(val), ("FFFFFF", "000000"))
          cell.fill = PatternFill(start_color=bg, end_color=bg, fill_type="solid")
          cell.font = Font(name="Calibri", size=11, bold=True, color=fg)
          cell.alignment = Alignment(horizontal="center", vertical="center")
        elif col_name == "Lien Annonce":
          if pd.notnull(val):
            cell.value = "Voir l'annonce"
            cell.hyperlink = val
            cell.font = link_font
          cell.alignment = Alignment(horizontal="center", vertical="center")
        else:
          cell.alignment = Alignment(horizontal="left", vertical="center")

    for col in ws.columns:
      max_len = 0
      col_letter = get_column_letter(col[0].column)
      for cell in col:
        val_str = "Voir l'annonce" if cell.hyperlink else str(cell.value or "")
        max_len = max(max_len, len(val_str))
      ws.column_dimensions[col_letter].width = max(max_len + 4, 12)

    ws.freeze_panes = "A2"
    wb.save(excel_path)

    if verbose:
      cols_to_show = [
          c
          for c in [
              "Marque",
              "Modèle",
              "Temps perdu (min)",
              "Road trip",
              "Coef. Électrique",
              "Coef. Marque",
              "Prix Estimé (€)",
              "Prix Demandé (€)",
              "Indice Opportunité",
          ]
          if c in export_df.columns
      ]
      print("\n🏆 --- TOP 10 DES OPPORTUNITÉS ---")
      print(export_df[cols_to_show].head(10).to_string(index=False))
      print(f"\n💾 Fichier Excel généré : {os.path.abspath(excel_path)}")

    return export_df


def load_json_robust(path: str) -> Any:
  """Lit un JSON en UTF-8 strict, avec repli cp1252 puis latin-1 (évite 'Citroën' -> 'CitroÃ«n')."""
  raw = Path(path).read_bytes()
  for enc in ("utf-8-sig", "cp1252", "latin-1"):
    try:
      return json.loads(raw.decode(enc))
    except (UnicodeDecodeError, json.JSONDecodeError):
      continue
  raise ValueError(f"Impossible de lire {path} comme JSON")


if __name__ == "__main__":
  import sys
  if paths.RESOLVED_DB.exists():
    try:
      estimator = MarketEstimator()
    except MissingReferenceError as e:
      print(f"❌ {e}")
      sys.exit(2)
    print(f"📂 Chargement du cache {paths.RESOLVED_DB}...")
    raw_data = load_json_robust(str(paths.RESOLVED_DB))
    cars_data = list(raw_data.get("_default", raw_data).values())
    estimator.process_and_export(cars_data, verbose=True)
    print("Références : " + " | ".join(f"{seg} = {b['ref_name']} ({b['real_new_price']:.0f} €)"
                                        for seg, b in sorted(estimator.REFERENCE_BENCHMARKS.items())))
  else:
    print(f"❌ {paths.RESOLVED_DB} introuvable : lancer d'abord le pipeline (batch\\full.bat).")
    sys.exit(1)
