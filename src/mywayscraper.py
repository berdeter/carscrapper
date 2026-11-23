from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from cardata import CarListing, CarScraperBase
from carhelper import CarHelper


class MyWayScraper(CarScraperBase):
    """Scraper GraphQL pour myway.be conforme à l'interface CarScraperBase.

    N'extrait que des FAITS BRUTS (ce que l'API affirme). Ni table de résolution de variante, ni
    normalisation de marque, ni filtre par modèle : ces éléments relèvent du pipeline
    (`spec_hints.py` pour les déductions batterie/WLTP, BrandFilter/ModelCatalogMatcher pour le référentiel).
    """

    SOURCE_NAME = "myway.be"
    BASE_URL = "https://www.myway.be"
    GRAPHQL_URL = "https://www.myway.be/graphql"

    VEHICLES_QUERY = """
    query Vehicles(
      $first: Int,
      $after: String,
      $orderBy: [String],
      $makeFamilies: String,
      $modelFamilies: String,
      $bodyTypes: String,
      $fuels: String,
      $priceMin: Float,
      $priceMax: Float,
      $mileageMax: Float,
      $firstRegistrationYearMin: Float,
      $sold: Boolean,
      $reserved: Boolean,
      $search: String,
      $settings: VehicleSearchSettings
    ) {
      vehiclesV3(
        first: $first,
        after: $after,
        orderBy: $orderBy,
        makeFamilies: $makeFamilies,
        modelFamilies: $modelFamilies,
        bodyTypes: $bodyTypes,
        fuels: $fuels,
        priceMin: $priceMin,
        priceMax: $priceMax,
        mileageMax: $mileageMax,
        firstRegistrationYearMin: $firstRegistrationYearMin,
        sold: $sold,
        reserved: $reserved,
        search: $search,
        settings: $settings
      ) {
        totalCount
        pageInfo {
          hasNextPage
          endCursor
        }
        edges {
          node {
            pk
            identification
            reference
            vin
            url
            make
            model
            modelFamily
            version
            firstRegistrationMonth
            firstRegistrationYear
            mileage
            bodyKey
            fuelKey
            price
            wltpKmRange
            batteryCapacity
            sold
            reserved
            kw
            hp
            warranty
            equipmentKeys
            dealer {
              name
              postal
              city
            }
          }
        }
      }
    }
    """

    def __init__(
        self,
        helper: CarHelper,
        fuels: Optional[str] = "electric",
        body_types: Optional[str] = None,
        make_families: Optional[str] = None,
        models_whitelist: Optional[List[str]] = None,
        price_max: Optional[float] = None,
        mileage_max: Optional[float] = None,
        year_min: Optional[int] = None,
        include_reserved: bool = False,
        page_size: int = 50,
        delay_sec: float = 0.4,
        timeout_sec: int = 20,
    ) -> None:
        self.fuels = fuels
        self.body_types = body_types
        self.make_families = make_families
        self.models_whitelist = set(models_whitelist) if models_whitelist else None
        self.price_max = price_max
        self.mileage_max = mileage_max
        self.year_min = year_min
        self.include_reserved = include_reserved
        self.page_size = page_size
        self.delay_sec = delay_sec
        self.timeout_sec = timeout_sec

        self._raw_cache_by_url: Dict[str, Dict[str, Any]] = {}
        self.session = self._build_session()

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=4,
            backoff_factor=0.8,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("POST",),
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update(
            {
                "accept": "*/*",
                "accept-language": "fr",
                "content-type": "application/json",
                "origin": self.BASE_URL,
                "referer": f"{self.BASE_URL}/fr/offre-voitures-occasion/",
                "user-agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            }
        )
        return session

    def _build_variables(
        self,
        after_cursor: Optional[str] = None,
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        variables: Dict[str, Any] = {
            "first": self.page_size,
            "after": after_cursor,
            "orderBy": ["-publicationDate"],
            "fuels": self.fuels,
            "bodyTypes": self.body_types,
            "makeFamilies": self.make_families,
            "priceMax": self.price_max,
            "mileageMax": self.mileage_max,
            "firstRegistrationYearMin": float(self.year_min) if self.year_min else None,
            "sold": False,
            "reserved": None if self.include_reserved else False,
            "search": search,
            "settings": {
                "vehicleDetailPageTypeName": "vehicleDetail",
                "initialFilters": [{"type": "not_labels", "value": "only_lease"}],
            },
        }
        return {k: v for k, v in variables.items() if v is not None}

    def _post_graphql(self, variables: Dict[str, Any]) -> Dict[str, Any]:
        response = self.session.post(
            self.GRAPHQL_URL,
            json={"query": self.VEHICLES_QUERY, "variables": variables},
            timeout=self.timeout_sec,
        )
        response.raise_for_status()
        payload = response.json()
        if "errors" in payload:
            raise RuntimeError(f"Erreur GraphQL MyWay : {payload['errors']}")
        return payload["data"]["vehiclesV3"]

    def scrape_base_data(self, max_pages: int = 200) -> List[CarListing]:
        """Extrait les annonces depuis l'API GraphQL MyWay en paginant via endCursor.

        Si une page échoue après plusieurs tentatives, on conserve les annonces déjà lues : le pipeline
        les fusionne dans raw_listings.json et la suite sera récupérée au prochain passage."""
        listings: List[CarListing] = []
        after_cursor: Optional[str] = None

        for page_idx in range(max_pages):
            variables = self._build_variables(after_cursor=after_cursor)
            try:
                v3_data = self._post_graphql(variables)
            except Exception as e:
                print(f"[{self.SOURCE_NAME}] Erreur page {page_idx + 1} : {e} ({len(listings)} annonces conservées)")
                break

            if page_idx == 0:
                print(f"[{self.SOURCE_NAME}] {v3_data.get('totalCount', 0)} annonces disponibles sur l'API.")

            edges = v3_data.get("edges") or []
            for edge in edges:
                node = edge.get("node") or {}
                raw_model = (node.get("model") or "").strip()
                if self.models_whitelist and raw_model not in self.models_whitelist:
                    continue

                listing = self._node_to_car_listing(node)
                if listing.url:
                    self._raw_cache_by_url[listing.url] = node
                listings.append(listing)

            page_info = v3_data.get("pageInfo") or {}
            if not page_info.get("hasNextPage") or not page_info.get("endCursor"):
                break

            after_cursor = page_info["endCursor"]
            if self.delay_sec > 0:
                time.sleep(self.delay_sec)

        print(f"[{self.SOURCE_NAME}] Total : {len(listings)} véhicules extraits.")
        return listings

    def scrape_details(self, listing: CarListing) -> CarListing:
        """
        Complète les données détaillées d'un CarListing MyWay.
        Utilise le cache de session ou réinterroge GraphQL par référence d'URL si nécessaire.
        """
        node = self._raw_cache_by_url.get(listing.url or "")

        if node is None and listing.url:
            reference = self._extract_reference_from_url(listing.url)
            if reference:
                v3_data = self._post_graphql(
                    {
                        "first": 5,
                        "search": reference,
                        "settings": {
                            "vehicleDetailPageTypeName": "vehicleDetail",
                            "initialFilters": [{"type": "not_labels", "value": "only_lease"}],
                        },
                    }
                )
                for edge in v3_data.get("edges") or []:
                    candidate = edge.get("node") or {}
                    if candidate.get("reference") == reference or (
                        candidate.get("url") and listing.url.endswith(candidate["url"])
                    ):
                        node = candidate
                        self._raw_cache_by_url[listing.url] = node
                        break

        if node is None:
            # Fallback : relire au minimum les spécifications annoncées dans le titre existant
            bat_kwh, wltp_km = self._extract_stated_specs(
                version=listing.title,
                api_battery=listing.battery_kwh,
                api_wltp=listing.range_wltp_km,
            )
            listing.battery_kwh = listing.battery_kwh or bat_kwh
            listing.range_wltp_km = listing.range_wltp_km or wltp_km
            return listing

        enriched = self._node_to_car_listing(node)
        listing.title = enriched.title
        listing.brand = enriched.brand
        listing.model = enriched.model
        listing.price = enriched.price
        listing.mileage_km = enriched.mileage_km
        listing.year = enriched.year
        listing.battery_kwh = enriched.battery_kwh
        listing.range_wltp_km = enriched.range_wltp_km
        listing.power_hp = enriched.power_hp
        listing.warranty_months = enriched.warranty_months
        listing.dealer_city = enriched.dealer_city
        listing.url = enriched.url
        return listing

    def _node_to_car_listing(self, node: Dict[str, Any]) -> CarListing:
        raw_make = (node.get("make") or "").strip().upper()
        brand = raw_make.title() if raw_make else None  # casse/accents sont normalisés par le référentiel
        model = (node.get("model") or "").strip() or None
        version = (node.get("version") or "").strip()

        title_parts = [p for p in (brand, model, version) if p]
        title = " | ".join(title_parts) if title_parts else "Véhicule MyWay"

        power_kw = node.get("kw")
        power_hp = node.get("hp")
        if power_hp is None and power_kw is not None:
            power_hp = round(float(power_kw) * 1.35962)

        bat_kwh, wltp_km = self._extract_stated_specs(
            version=version,
            api_battery=node.get("batteryCapacity"),
            api_wltp=node.get("wltpKmRange"),
        )

        warranty_years = node.get("warranty")
        warranty_months = int(warranty_years) * 12 if warranty_years is not None else None

        dealer = node.get("dealer") or {}
        dealer_city = (dealer.get("city") or "").strip() or None

        rel_url = (node.get("url") or "").strip()
        full_url = f"{self.BASE_URL}{rel_url}" if rel_url.startswith("/") else (rel_url or None)

        price_val = node.get("price")
        mileage_val = node.get("mileage")
        year_val = node.get("firstRegistrationYear")

        return CarListing(
            source=self.SOURCE_NAME,
            title=title,
            brand=brand,
            model=model,
            price=float(price_val) if price_val is not None else None,
            mileage_km=int(mileage_val) if mileage_val is not None else None,
            year=int(year_val) if year_val is not None else None,
            battery_kwh=bat_kwh,
            range_wltp_km=wltp_km,
            power_hp=int(power_hp) if power_hp is not None else None,
            soh_percent=None,
            warranty_months=warranty_months,
            previous_owners=None,
            has_service_history=True,  # Réseau certifié MyWay / D'Ieteren
            dealer_city=dealer_city,
            url=full_url,
        )

    @staticmethod
    def _extract_stated_specs(
        version: str,
        api_battery: Optional[float],
        api_wltp: Optional[int],
    ) -> Tuple[Optional[float], Optional[int]]:
        """Batterie / WLTP ANNONCÉS : champs de l'API, sinon valeurs écrites dans le libellé de version.
        Aucune déduction par table (marque, modèle, kW) : c'est le rôle de spec_hints.py, après le scraping."""
        bat_kwh: Optional[float] = float(api_battery) if api_battery is not None else None
        wltp_km: Optional[int] = int(api_wltp) if api_wltp is not None else None

        if bat_kwh is None and version:
            m_bat = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:kwh|khw|kw/h)\b", version, flags=re.IGNORECASE)
            if m_bat:
                val = float(m_bat.group(1).replace(",", "."))
                if 15.0 <= val <= 130.0:
                    bat_kwh = val

        if wltp_km is None and version:
            m_wltp = re.search(r"\b(\d{3})\s*km\s*wltp\b", version, flags=re.IGNORECASE)
            if m_wltp:
                wltp_km = int(m_wltp.group(1))

        return bat_kwh, wltp_km

    @staticmethod
    def _extract_reference_from_url(url: str) -> Optional[str]:
        path = urlparse(url).path.strip("/")
        parts = path.split("/")
        if parts:
            last = parts[-1]
            if re.fullmatch(r"[0-9a-fA-F-]{32,36}", last):
                return last
        return None

if __name__ == "__main__":
    from market_estimator import MarketEstimator
    from pipeline import CarPipeline

    max_pages = 10  # Ajuste selon tes besoins de test
    helper = CarHelper(enrich_details=True, headless=False)
    scraper = MyWayScraper(helper=helper)
    pipeline = CarPipeline(scrapers=[scraper], helper=helper, resolved_db_path="resolved_cars.json")

    print("=== TEST DU PIPELINE : MYWAY ===")
    resolved_cars = pipeline.run_single(scraper, max_pages=max_pages, verbose=True)

    if resolved_cars:
        estimator = MarketEstimator(output_filename="opportunities_cars")
        estimator.process_and_export(resolved_cars, verbose=True)
    else:
        print("⚠️ Aucun véhicule extrait pour MyWay.")
