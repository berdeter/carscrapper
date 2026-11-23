"""Utilitaires partagés par les scrapers : en-têtes HTTP et extraction de valeurs dans du texte
(prix, kilométrage, année, batterie, autonomie WLTP).

Ce sont des fonctions pures : aucun accès réseau, cache ou référentiel. La persistance et le rejeu
relèvent du pipeline (raw_listings.json), pas de ce module.
"""
import re
from datetime import date
from typing import Any, Optional

# Nombre « à la française » : groupes de milliers séparés par espace / espace insécable / point / virgule,
# ou entier simple. Le lookbehind interdit de commencer au milieu d'un autre nombre (ex. « 2022 45 000 »).
_THOUSANDS = r"\d{1,3}(?:[ \xa0\u202f.,]\d{3})+"
_NUM = rf"(?<![\d.,])(?:{_THOUSANDS}(?:[.,]\d{{1,2}})?|\d+(?:[.,]\d{{1,2}})?)"
_INT = rf"(?<![\d.,])(?:{_THOUSANDS}|\d+)"

_PRICE_WITH_SYMBOL = re.compile(rf"(?:€|EUR)\s*({_NUM})|({_NUM})\s*(?:€|EUR)", re.IGNORECASE)
_PRICE_PLAIN = re.compile(rf"({_THOUSANDS}(?:[.,]\d{{1,2}})?|(?<![\d.,])\d{{4,6}}(?:[.,]\d{{1,2}})?)")
_NOT_A_PRICE_SUFFIX = re.compile(r"\s*(?:km|kwh|kw|ch|cv|hp|mois|months|ans)\b", re.IGNORECASE)

_MILEAGE = re.compile(rf"({_INT})\s*km\b", re.IGNORECASE)
_NOT_MILEAGE_AFTER = re.compile(r"\s*(?:/\s*h|wltp|d['’]autonomie|de\s+autonomie)", re.IGNORECASE)
_NOT_MILEAGE_BEFORE = re.compile(r"(?:autonomie|range|rayon|distance|jusqu['’]?\s*à|à)\s*:?\s*$", re.IGNORECASE)

_BATTERY = re.compile(r"(?<![\d.,])(\d{2,3}(?:[.,]\d{1,2})?)\s*k\s?wh\b", re.IGNORECASE)
_WLTP = re.compile(rf"({_INT})\s*km\s*wltp\b", re.IGNORECASE)
_YEAR = re.compile(r"\b(20[12]\d)\b")


class CarHelper:
  def __init__(
      self,
      cache: Optional[Any] = None,
      enrich_details: bool = True,
      headless: bool = True,
  ):
    """`cache` et `enrich_details` sont conservés pour la compatibilité des appels existants mais ne servent
    plus : le cache n'est plus créé ici (il écrivait un fichier cars_cache.json à chaque instanciation) et la
    persistance est gérée par le pipeline. `headless` reste utilisé par les scrapers Playwright."""
    self.cache = cache
    self.enrich_details = enrich_details
    self.headless = headless
    self.headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    }

  # ------------------------------------------------------------------
  @staticmethod
  def _clean_number(token: str) -> Optional[float]:
    """'24.990' -> 24990 ; '30 990,00' -> 30990.0 ; '28,950' -> 28950 ; '16999.00' -> 16999.0."""
    t = token.replace("\xa0", "").replace("\u202f", "").replace(" ", "")
    if "," in t and "." in t:
      if t.rfind(",") > t.rfind("."):
        t = t.replace(".", "").replace(",", ".")
      else:
        t = t.replace(",", "")
    elif "," in t:
      parts = t.split(",")
      t = t.replace(",", ".") if len(parts) == 2 and len(parts[1]) <= 2 else t.replace(",", "")
    elif "." in t:
      parts = t.split(".")
      if len(parts) > 2 or (len(parts) == 2 and len(parts[1]) == 3):
        t = t.replace(".", "")
    try:
      return float(t)
    except ValueError:
      return None

  def _parse_price(self, text: Optional[str]) -> Optional[float]:
    """Premier prix plausible (1 000 - 500 000 €) du texte.

    1. un nombre collé à « € » ou « EUR » (« 24 990 € », « € 28950 », « EUR 28,950 ») : c'est le prix affiché,
       et c'est le premier qui compte (« 24 990 € au lieu de 27 990 € » -> 24 990) ;
    2. à défaut, un nombre isolé, sauf s'il est suivi d'une unité (km, kWh, mois...) ou s'il ressemble à une année.
    """
    if not text:
      return None
    s = str(text).strip()

    for m in _PRICE_WITH_SYMBOL.finditer(s):
      val = self._clean_number(m.group(1) or m.group(2))
      if val is not None and 1_000 <= val <= 500_000:
        return val

    for m in _PRICE_PLAIN.finditer(s):
      token = m.group(1)
      if _NOT_A_PRICE_SUFFIX.match(s, m.end()):
        continue
      if re.fullmatch(r"(?:19[89]\d|20[0-3]\d)", token):  # année nue
        continue
      val = self._clean_number(token)
      if val is not None and 1_000 <= val <= 500_000:
        return val
    return None

  def _extract_mileage(self, text: str) -> Optional[int]:
    """Premier kilométrage plausible. Ignore l'autonomie (« 400 km WLTP », « autonomie 400 km ») et les
    distances (« à 5 km de chez vous », « 130 km/h ») ; ne colle jamais l'année ou le modèle aux chiffres."""
    if not text:
      return None
    for m in _MILEAGE.finditer(text):
      if _NOT_MILEAGE_AFTER.match(text, m.end()):
        continue
      if _NOT_MILEAGE_BEFORE.search(text[max(0, m.start() - 20):m.start()]):
        continue
      try:
        val = int(re.sub(r"[ \xa0\u202f.,]", "", m.group(1)))
      except ValueError:
        continue
      if 0 <= val <= 500_000:
        return val
    return None

  def _extract_year(self, text: str) -> Optional[int]:
    """Première année 2010-2029 qui n'est pas dans le futur (ignore « garantie jusqu'en 2029 »)."""
    if not text:
      return None
    limit = date.today().year + 1
    for m in _YEAR.finditer(text):
      year = int(m.group(1))
      if year <= limit:
        return year
    return None

  def _extract_battery(self, text: str) -> Optional[float]:
    """Première capacité plausible (10-160 kWh), quelle que soit la casse (kWh, kwh, KWH)."""
    if not text:
      return None
    for m in _BATTERY.finditer(text):
      val = float(m.group(1).replace(",", "."))
      if 10.0 <= val <= 160.0:
        return val
    return None

  def _extract_wltp(self, text: str) -> Optional[int]:
    """Première autonomie WLTP plausible (100-900 km)."""
    if not text:
      return None
    for m in _WLTP.finditer(text):
      try:
        val = int(re.sub(r"[ \xa0\u202f.,]", "", m.group(1)))
      except ValueError:
        continue
      if 100 <= val <= 900:
        return val
    return None
