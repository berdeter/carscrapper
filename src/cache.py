from typing import Any, Dict, Iterable, Optional, Union
from tinydb import Query, TinyDB


class CarCache:

  def __init__(self, db_path: str = "cars_cache.json"):
    # TinyDB stocke tout dans un fichier JSON local léger
    # encoding explicite : sans lui, TinyDB écrit dans l'encodage du système (cp1252 sous Windows).
    self.db = TinyDB(db_path, encoding="utf-8", ensure_ascii=False, indent=2)
    self.query = Query()

  def get(self, url: str) -> Optional[Dict[str, Any]]:
    """Récupère une annonce en cache par son URL."""
    result = self.db.search(self.query.url == url)
    return result[0] if result else None

  def exists(self, url: str) -> bool:
    """Vérifie si une annonce est déjà en cache."""
    return self.db.contains(self.query.url == url)

  def upsert(self, listing_data: Dict[str, Any]):
    """Insère ou met à jour une annonce dans le cache."""
    url = listing_data.get("url")
    if not url:
      return
    if self.exists(url):
      self.db.update(listing_data, self.query.url == url)
    else:
      self.db.insert(listing_data)

  def update(
      self, car_or_url: Union[str, Any], fields: Optional[Dict[str, Any]] = None
  ) -> bool:
    """Met à jour un enregistrement existant en cache.

    Accepte soit (url: str, fields: dict), soit un objet (CarListing / dict).
    Retourne True si la mise à jour a réussi, False si l'URL n'existe pas.
    """
    if isinstance(car_or_url, str):
      url = car_or_url
      data = fields or {}
    else:
      data = (
          car_or_url.to_dict()
          if hasattr(car_or_url, "to_dict")
          else dict(car_or_url)
      )
      url = data.get("url")

    if not url or not self.exists(url):
      return False

    self.db.update(data, self.query.url == url)
    return True

  def ensure_present(self, car: Any) -> bool:
    """S'assure qu'un objet CarListing est présent en base.

    Retourne True s'il a été inséré, False s'il était déjà présent ou sans URL.
    """
    data = car.to_dict() if hasattr(car, "to_dict") else dict(car)
    url = data.get("url")

    if not url or self.exists(url):
      return False

    self.db.insert(data)
    return True

  def ensure_all_present(self, cars: Iterable[Any]) -> int:
    """S'assure qu'une liste de CarListing est présente en base.

    Insère les éléments manquants et retourne le nombre de nouvelles annonces ajoutées.
    """
    added_count = 0
    for car in cars:
      data = car.to_dict() if hasattr(car, "to_dict") else dict(car)
      url = data.get("url")
      if url and not self.exists(url):
        self.db.insert(data)
        added_count += 1
    return added_count

  def all_records(self) -> list:
    """Retourne tous les enregistrements du cache."""
    return self.db.all()

  def count(self) -> int:
    return len(self.db)