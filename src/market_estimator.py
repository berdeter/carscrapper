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


class MarketEstimator:
  REFERENCE_BENCHMARKS = {
      "A": {
          "ref_name": "Dacia Spring Expression (80 ch)",
          "real_new_price": 17890.0,
          "wltp_km": 246.0,
          "dc_kw": 50.0,
          "ref_feeling": 92.0,  # Dacia dans classification_marques_belgique-v3_recalibrated.json
          "ref_sim_min": 834.5,  # Dacia Spring, simulation v2 (rythme humain) : temps total 1000 km en minutes
      },
      "B": {
          "ref_name": "Hyundai IONIQ 3 Select (42.2 kWh)",
          "real_new_price": 31495.0,
          "wltp_km": 344.0,
          "dc_kw": 110.0,
          "ref_feeling": 87.8,  # Hyundai dans classification_marques_belgique-v3_recalibrated.json
          "ref_sim_min": 639.6,  # Ioniq 3 petite batterie, simulation v2 : temps total 1000 km en minutes
      },
  }
  def __init__(
      self,
      output_filename: str = "best_ev_opportunities",
      current_year: Optional[int] = None,
      brands_json_path: str | Path = "classification_marques_belgique-v3.json",
      roadtrip_max_lost_min: float = 240.0,
      roadtrip_ok_lost_min: float = 60.0,
      roadtrip_min_range_km: float = 120.0,
      roadtrip_gate_segments: tuple = ("B",),
      ev_exponent: float = 0.65,
  ):
    """
    Barrière road trip (1000 km, rythme habituel) : ne retire que les voitures pour lesquelles
    c'est impraticable, pas celles qui sont simplement moins bonnes que la référence.
      roadtrip_max_lost_min  : au-delà de ce temps perdu vs thermique -> "Impossible".
      roadtrip_ok_lost_min   : en dessous -> "OK" ; entre les deux -> "Long".
      roadtrip_min_range_km  : autonomie autoroute (100 % -> réserve) minimale ; en dessous -> "Impossible".
      roadtrip_gate_segments : segments où "Impossible" exclut la voiture. Les autres (ex. A) sont
                               conservés et seulement étiquetés.
      ev_exponent            : poids du temps de trajet dans la valorisation ((ref/voiture) ** exposant).
    """
    self.output_filename = output_filename
    self.current_year = current_year or date.today().year
    self.REFERENCE_BENCHMARKS = copy.deepcopy(MarketEstimator.REFERENCE_BENCHMARKS)  # pas d'état partagé
    self.roadtrip_max_lost_min = roadtrip_max_lost_min
    self.roadtrip_ok_lost_min = roadtrip_ok_lost_min
    self.roadtrip_min_range_km = roadtrip_min_range_km
    self.roadtrip_gate_segments = tuple(str(s).upper() for s in roadtrip_gate_segments)
    self.ev_exponent = ev_exponent
    self.brands_json_path = Path(brands_json_path)
    self.brand_feelings: Dict[str, float] = self._load_brand_feelings()
    self._sync_benchmark_feelings()

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

  def _sync_benchmark_feelings(self) -> None:
    """Synchronise automatiquement les scores de référence des segments A (Dacia) et B (Hyundai) avec le JSON."""
    if "dacia" in self.brand_feelings:
      self.REFERENCE_BENCHMARKS["A"]["ref_feeling"] = self.brand_feelings["dacia"]
    if "hyundai" in self.brand_feelings:
      self.REFERENCE_BENCHMARKS["B"]["ref_feeling"] = self.brand_feelings["hyundai"]

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

    var_obj = d.get("variante_resolue")
    flat["variante_resolue_flag"] = isinstance(var_obj, dict)
    if not isinstance(var_obj, dict):
      candidates = d.get("variantes_candidates", [])
      if candidates and isinstance(candidates, list):
        var_obj = candidates[0]

    sim = {}
    if isinstance(var_obj, dict):
      tech = var_obj.get("donnees_techniques", {})
      flat["norm_battery_kwh"] = (
          tech.get("batterie_utile_kwh") or flat["listing_batt"]
      )
      flat["wltp_km"] = tech.get("autonomie_wltp_km") or flat["listing_wltp"]
      flat["dc_kw"] = tech.get("puissance_dc_max_kw")
      flat["variante_nom"] = var_obj.get("nom_variante")
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

    return flat

  def _compute_valuation(self, row: pd.Series) -> pd.Series:
    seg = row.get("segment", "B")
    bench = self.REFERENCE_BENCHMARKS.get(seg, self.REFERENCE_BENCHMARKS["B"])

    ref_price = bench["real_new_price"]
    ref_feeling = bench["ref_feeling"]
    ref_sim_min = bench["ref_sim_min"]

    # Calcul du coefficient électrique basé sur la simulation humaine 1000km
    car_sim_min = pd.to_numeric(row.get("sim_humaine_1000km_min"), errors="coerce")
    if pd.notnull(car_sim_min) and car_sim_min > 0:
      ev_ratio = ref_sim_min / car_sim_min
      ev_factor = round(ev_ratio**self.ev_exponent, 3)
    else:
      ev_factor = 1.0

    year = pd.to_numeric(row.get("year"), errors="coerce")
    mileage = pd.to_numeric(row.get("mileage_km"), errors="coerce")

    age = max(0, self.current_year - int(year)) if pd.notnull(year) else 1
    km = float(mileage) if pd.notnull(mileage) else (age * 15000.0)

    # 1. Décote liée à l'âge (15% la 1ère année, 8% jusqu'à 5 ans, puis 5% au-delà)
    if age == 0:
      age_depreciation = 0.15
    elif age <= 5:
      age_depreciation = 0.15 + (age - 1) * 0.08
    else:
      age_depreciation = 0.47 + (age - 5) * 0.05

    # 2. Ajustement kilométrique basé sur l'écart par rapport à la normale (15 000 km/an)
    expected_km = age * 15000.0 if age > 0 else 1500.0
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

    # Facteur SoH (State of Health) de la batterie
    soh = pd.to_numeric(row.get("soh_percent"), errors="coerce")
    if pd.notnull(soh):
      soh_factor = soh * 0.01
    else:
      ref_yr = int(year) if pd.notnull(year) else self.current_year
      soh_factor = (100 - 2.0 * (self.current_year - ref_yr)) * 0.01

    estimated_price = round(
        ref_price * ev_factor * usage_factor * brand_factor * soh_factor, 0
    )

    asked_price = pd.to_numeric(row.get("price"), errors="coerce")
    if pd.notnull(asked_price) and asked_price > 0:
      opp_ratio = round(estimated_price / asked_price, 3)
    else:
      opp_ratio = 0.0

    # --- Aptitude au road trip (barrière absolue, pas une comparaison à la référence) ---
    lost = pd.to_numeric(row.get("temps_perdu_min"), errors="coerce")
    rng = pd.to_numeric(row.get("autonomie_autoroute_km"), errors="coerce")
    realisable = row.get("roadtrip_realisable")
    not_feasible = realisable is not None and not pd.isna(realisable) and not bool(realisable)
    if not_feasible:
      roadtrip = "Impossible"
    elif pd.isnull(lost):
      roadtrip = "Inconnu"
    elif lost > self.roadtrip_max_lost_min or (
        pd.notnull(rng) and rng < self.roadtrip_min_range_km
    ):
      roadtrip = "Impossible"
    elif lost <= self.roadtrip_ok_lost_min:
      roadtrip = "OK"
    else:
      roadtrip = "Long"

    # --- Fiabilité de l'estimation : variante résolue, SoH et kilométrage réellement connus ---
    flag = row.get("variante_resolue_flag")
    known = int(bool(flag) and not pd.isna(flag)) + int(pd.notnull(soh)) + int(pd.notnull(mileage))
    reliability = {3: "Haute", 2: "Moyenne"}.get(known, "Faible")

    return pd.Series({
        "roadtrip_status": roadtrip,
        "fiabilite": reliability,
        "ref_model_name": bench["ref_name"],
        "ref_new_price": ref_price,
        "ev_factor": ev_factor,
        "usage_factor": usage_factor,
        "brand_factor": brand_factor,
        "soh_factor": soh_factor,
        "estimated_price": estimated_price,
        "opportunity_ratio": opp_ratio,
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
    impossible = df["roadtrip_status"] == "Impossible"
    gated = impossible & df["segment"].isin(self.roadtrip_gate_segments)
    kept_flagged = int((impossible & ~gated).sum())
    unknown = int((df["roadtrip_status"] == "Inconnu").sum())
    df = df[~gated].copy()
    if verbose:
      if int(gated.sum()):
        print(
            f"🧹 {int(gated.sum())} véhicule(s) exclu(s) : road trip impossible"
            f" (> {self.roadtrip_max_lost_min:.0f} min perdues ou autonomie autoroute"
            f" < {self.roadtrip_min_range_km:.0f} km) en segment {'/'.join(self.roadtrip_gate_segments)}"
            f" (Reste : {len(df)} véhicules)."
        )
      if kept_flagged:
        print(f"ℹ️ {kept_flagged} véhicule(s) 'Impossible' conservés (segment hors barrière).")
      if unknown:
        print(f"ℹ️ {unknown} véhicule(s) sans simulation road trip (statut 'Inconnu', conservés).")

    df = df.sort_values(
        by=["opportunity_ratio", "price"], ascending=[False, True]
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
        "soh_factor",
        "estimated_price",
        "price",
        "opportunity_ratio",
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
        "soh_factor": "Coef. SoH",
        "estimated_price": "Prix Estimé (€)",
        "price": "Prix Demandé (€)",
        "opportunity_ratio": "Indice Opportunité",
        "fiabilite": "Fiabilité",
        "source": "Source",
        "url": "Lien Annonce",
    }
    export_df = export_df.rename(columns=rename_map)

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
      row_data = list(row)
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
            "Coef. SoH",
        ]:
          cell.number_format = "0.000"
          cell.alignment = Alignment(horizontal="right", vertical="center")
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
              "Impossible": ("FFC7CE", "9C0006"), "Faible": ("FFC7CE", "9C0006"),
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
  cache_files = ["resolved_cars.json"]
  loaded = False
  for cache_file in cache_files:
    if os.path.exists(cache_file):
      print(f"📂 Chargement automatique du cache depuis {cache_file}...")
      raw_data = load_json_robust(cache_file)
      cars_data = list(raw_data.get("_default", raw_data).values())
      estimator = MarketEstimator(
          output_filename="best_ev_opportunities",
          brands_json_path="classification_marques_belgique-v3.json",
      )
      estimator.process_and_export(cars_data, verbose=True)
      loaded = True
      break
  if not loaded:
    print(
        "❌ Aucun fichier de cache (resolved_cars.json) n'a été trouvé dans le"
        " répertoire courant."
    )