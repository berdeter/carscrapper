import json
import unicodedata
from pathlib import Path
from typing import List, Set
from cardata import CarListing
import paths
from typing import List

class BrandFilter:
  def __init__(self,
        json_path: str | Path = paths.BRANDS,
        min_score: float = 55.0):
      # 55 = seuil « Correct ». Depuis l'ajout de la maturité EV (v3.1, 24/11/2026), les marques Stellantis,
      # Fiat, Ford et Honda descendent entre 65 et 69 : le seuil historique de 70 les aurait exclues du
      # catalogue. Elles restent analysées, la pénalité passe par le coefficient marque de l'estimateur.

      self.allowed_brands = self.load_allowed_brands(json_path=json_path, min_score=min_score)

  def is_brand_allowed(self, brand:str) -> bool:
      return self._normalize_brand(brand) in self.allowed_brands
 

  def brand_filter(
      self,
      database: List[CarListing],
  ) -> List[CarListing]:
      """Filters out the database removing all cars of an unwanted brand based on an external JSON."""
      
      
      return [
          car for car in database
          if self.is_brand_allowed(car.brand)
      ]



  def _normalize_brand(self, name: str) -> str:
      """Normalise le nom de la marque (minuscules, sans accents, espaces nettoyés)."""
      if not name:
          return ""
      nfkd = unicodedata.normalize("NFKD", name)
      without_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
      return without_accents.strip().lower()

  def load_allowed_brands(
      self,
      json_path: str | Path = paths.BRANDS,
      min_score: float = 55.0
  ) -> Set[str]:
      """Charge dynamiquement les marques autorisées depuis le fichier JSON selon le seuil de score."""
      path = Path(json_path)
      with path.open("r", encoding="utf-8") as f:
          data = json.load(f)

      allowed_brands: Set[str] = set()
      for marque in data.get("marques", []):
          score = marque.get("feeling_global", {}).get("score", 0.0)
          if score is not None and score >= min_score:
              # On indexe à la fois le nom officiel ("Škoda") et l'identifiant ("skoda")
              allowed_brands.add(self._normalize_brand(marque.get("nom", "")))
              allowed_brands.add(self._normalize_brand(marque.get("id", "")))

      return allowed_brands

