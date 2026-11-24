from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class CarListing:
  """Modèle de données unifié enrichi pour l'aide à la décision."""

  source: str
  title: str
  brand: Optional[str] = None
  model: Optional[str] = None
  price: Optional[float] = None
  mileage_km: Optional[int] = None
  year: Optional[int] = None
  battery_kwh: Optional[float] = None
  range_wltp_km: Optional[int] = None
  power_hp: Optional[int] = None
  soh_percent: Optional[int] = None
  warranty_months: Optional[int] = None
  previous_owners: Optional[int] = None
  has_service_history: Optional[bool] = None
  dealer_city: Optional[str] = None
  url: Optional[str] = None

  def to_dict(self) -> Dict[str, Any]:
    return asdict(self)

@dataclass
class EnrichedCarListing:
    """Structure de sortie enrichie avec les données du référentiel models.json."""
    listing: CarListing
    marque_id: str
    marque_nom: str
    feeling_marque: float
    modele_id: str
    nom_canonique: str
    segment: str
    type_ev: str
    variantes_candidates: List[Dict[str, Any]] = field(default_factory=list)
    variante_resolue: Optional[Dict[str, Any]] = None
    url: Optional[str] = None
    # Ids des variantes encore possibles quand la résolution échoue (sinon [id résolu]).
    variantes_possibles: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class DetailsFetchError(Exception):
    """scrape_details n'a pas pu lire la fiche (réseau, HTTP, page inattendue) : à retenter plus tard."""


class DetailsGoneError(DetailsFetchError):
    """La fiche n'existe plus (HTTP 404/410, annonce introuvable) : l'annonce a disparu."""


class CarScraperBase(ABC):
    @abstractmethod
    def scrape_base_data(self, max_pages: int = 200)  -> List[CarListing]:
        """Extrait les données de base des annonces d'une page de liste"""
        pass

    @abstractmethod
    def scrape_details(self, listing: CarListing) -> CarListing:
        """Extrait les données détaillées d'une annonce spécifique.

        En cas d'échec, LEVER une exception (DetailsGoneError si l'annonce a disparu, DetailsFetchError ou
        toute autre exception sinon) : ne jamais renvoyer l'annonce inchangée, sinon le pipeline la
        considère comme lue et ne la retentera plus."""
        pass

