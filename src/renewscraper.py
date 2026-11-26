"""Scraper Renew Belgique (https://fr-be.renew.auto).

Réseau officiel d'occasions certifiées Renault Group en Belgique (Renault, Dacia,
Nissan, ainsi que reprises multimarques) : véhicules reconditionnés, garantie 12 à 36 mois.

Architecture du site :
  * Catalogue principal : ``/achat-vehicules-occasions.html?energy.groupLabel.raw=electrique``
  * Pagination via ``&page=N`` (24 annonces par page en général).
  * Fiches véhicules : ``/achat-vehicules-occasions/details.html?productId=VEH_...``
  * Les fiches embarquent fréquemment du JSON-LD (schema.org/Vehicle ou Product)
    en plus du HTML structuré.
"""
import json
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from brandfilter import BrandFilter
from cardata import CarListing, CarScraperBase, DetailsFetchError, DetailsGoneError
from carhelper import CarHelper

# Pré-filtre optionnel par segment (cohérent avec models.json <= 4m20)
_SEGMENT_PATTERNS: List[Tuple[str, str]] = [
    ("A", r"^renaulttwingo"),
    ("A", r"^daciaspring"),
    ("A", r"^fiat500"),
    ("A", r"^abarth500"),
    ("A", r"^smart(?:eq|for)"),
    ("A", r"^volkswagen(?:e)?up"),
    ("A", r"^skodacitigo"),
    ("A", r"^seatmii"),
    ("A", r"^hyundaiinster"),
    ("A", r"^citroenczero"),
    ("A", r"^peugeotion"),
    ("B", r"^renaultzoe"),
    ("B", r"^renault5\b|^renault5$|^renault5etech"),
    ("B", r"^renault4\b|^renault4$|^renault4etech"),
    ("B", r"^nissanmicra"),
    ("B", r"^peugeote?208"),
    ("B", r"^opelcorsa"),
    ("B", r"^citroene?c3(?!aircross)"),
    ("B", r"^minicooper|^minielectric"),
    ("B", r"^hondae$"),
    ("B", r"^vwidpolo|^volkswagenidpolo"),
    ("B", r"^cupraraval"),
    ("B", r"^skodaepiq"),
    ("B", r"^kiaev2"),
    ("B", r"^hyundaiioniq3"),
    ("B-SUV", r"^hyundaikona"),
    ("B-SUV", r"^peugeote?2008"),
    ("B-SUV", r"^opelmokka|^opelfrontera"),
    ("B-SUV", r"^fiat600"),
    ("B-SUV", r"^citroene?c3aircross"),
    ("B-SUV", r"^fordpuma"),
    ("B-SUV", r"^miniaceman"),
    ("B-SUV", r"^suzukievitara"),
    ("B-SUV", r"^kiasoul|^kiaesoul"),
]

DETAIL_RE = re.compile(r"/achat-vehicules-occasions/details\.html\?productId=(VEH_\w+)", re.IGNORECASE)
PRICE_RE = re.compile(r"([\d\s.\xa0\u202f]+)\s*€\s*(?:TVAc|TTC)?", re.IGNORECASE)
DATE_RE = re.compile(r"\b(\d{2})/(\d{4})\b")
KM_RE = re.compile(r"([\d\s.\xa0\u202f]+)\s*km\b", re.IGNORECASE)
WARRANTY_RE = re.compile(r"garantie\s+(\d+)\s+mois", re.IGNORECASE)
COUNT_RE = re.compile(r"(\d[\d\s\xa0]*)\s+(?:voitures?|r[ée]sultats?)", re.IGNORECASE)


def _norm(text: Any) -> str:
    s = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _to_int(s: Any) -> Optional[int]:
    digits = re.sub(r"\D", "", str(s or ""))
    return int(digits) if digits else None


class RenewScraper(CarScraperBase):
    BASE = "https://fr-be.renew.auto"
    SEARCH_PATH = "/achat-vehicules-occasions.html?battery.leasingMode.raw=B&bodyType.groupLabel.raw=citadine&energy.groupLabel.raw=electrique&location=50.63507569999999%2C5.5628874%2C50%2C4000%2CLi%C3%A8ge%2C4000%20Li%C3%A8ge"

    def __init__(
        self,
        helper: CarHelper,
        segments: Optional[Set[str]] = None,
        request_delay: float = 1.0,
        timeout: int = 20,
        brand_filter: Optional[BrandFilter] = None,
        brand_json_path: str | Path = "classification_marques_belgique-v3.json",
        min_brand_score: float = 70.0,
        exclude_future_availability: bool = False,
    ):
        """
        segments                    : ex. {"A", "B"} ou {"A", "B", "B-SUV"} ; None = pas de filtre segment.
        request_delay               : pause (s) entre deux requêtes HTTP.
        brand_filter                : instance optionnelle de BrandFilter.
        exclude_future_availability : si True, écarte les véhicules de démonstration marqués "DISPO JJ/MM/AA".
        """
        self.helper = helper
        self.segments = {s.upper() for s in segments} if segments else None
        self.request_delay = request_delay
        self.timeout = timeout
        self.exclude_future_availability = exclude_future_availability

        self.brand_filter = brand_filter or BrandFilter()

        self.session = requests.Session()
        self.session.headers.update(self.helper.headers)
        self._last_request = 0.0
        self._detail_cache: Dict[str, Dict[str, Any]] = {}

        self.stats = {
            "pages": 0,
            "cards": 0,
            "filtered_brands": 0,
            "segment": 0,
            "future_dispo": 0,
            "kept": 0,
        }

    # ------------------------------------------------------------------
    # Interface CarScraperBase
    # ------------------------------------------------------------------
    def scrape_base_data(self, max_pages: int = 10) -> List[CarListing]:
        print("[Renew] Chargement du catalogue électrique Belgique...")
        self.stats = {k: 0 for k in self.stats}
        seen_ids: Set[str] = set()
        listings: List[CarListing] = []

        for page in range(1, max_pages + 1):
            params = {"energy.groupLabel.raw": "electrique"}
            if page > 1:
                params["page"] = str(page)
            url = f"{urljoin(self.BASE, self.SEARCH_PATH)}?{urlencode(params)}"

            soup = self._get(url)
            if soup is None:
                print(f"[Renew] Échec de récupération page {page}, arrêt pagination.")
                break

            if page == 1:
                total = self._result_count(soup)
                if total is not None:
                    print(f"[Renew] {total} véhicules électriques annoncés sur le site.")

            page_cards = self._parse_search_page(soup)
            if not page_cards:
                print(f"[Renew] Plus aucune annonce trouvée sur la page {page}.")
                break

            new_on_page = 0
            for card in page_cards:
                vid = card["id"]
                if vid in seen_ids:
                    continue
                seen_ids.add(vid)
                new_on_page += 1

                listing = self._card_to_listing(card)
                if listing is not None:
                    listings.append(listing)

            print(f"[Renew] Page {page} : {new_on_page} nouvelles annonces analysées.")
            # Si aucune nouvelle annonce n'a été découverte (boucle sur dernière page), on arrête
            if new_on_page == 0:
                break

        self.stats["kept"] = len(listings)
        print(f"[Renew] Total : {len(listings)} véhicules retenus ({self.stats}).")
        return listings

    def scrape_details(self, listing) -> Any:
        car = listing.listing if hasattr(listing, "listing") else listing
        # Une erreur remonte au pipeline (qui retentera plus tard) au lieu d'être avalée ici.
        detail = self._detail_cache.get(car.url)
        if detail is None and car.url:
            soup = self._get(car.url)
            if soup is None:
                if self._last_status in (404, 410):
                    raise DetailsGoneError(f"HTTP {self._last_status}")
                raise DetailsFetchError(f"fiche inaccessible (HTTP {self._last_status})")
            detail = self._parse_detail_page(soup)
            self._detail_cache[car.url] = detail
        if detail:
            self._apply_detail(car, detail)
        return listing

    # ------------------------------------------------------------------
    # Réseau
    # ------------------------------------------------------------------
    def _get(self, url: str) -> Optional[BeautifulSoup]:
        self._last_status = None  # dernier code HTTP (None : erreur réseau)
        for attempt in range(3):
            wait = self.request_delay - (time.time() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.time()
            try:
                resp = self.session.get(url, timeout=self.timeout)
            except requests.RequestException:
                time.sleep(2 * (attempt + 1))
                continue
            self._last_status = resp.status_code

            if resp.status_code == 200:
                self.stats["pages"] += 1
                return BeautifulSoup(resp.text, "html.parser")
            if resp.status_code in (429, 503):
                time.sleep(min(float(resp.headers.get("Retry-After", 5 * (attempt + 1))), 60))
                continue
            return None
        return None

    # ------------------------------------------------------------------
    # Parsing de la page de résultats
    # ------------------------------------------------------------------
    @staticmethod
    def _result_count(soup: BeautifulSoup) -> Optional[int]:
        text = soup.get_text(" ", strip=True)
        m = COUNT_RE.search(text)
        return _to_int(m.group(1)) if m else None

    def _ids_under(self, node) -> Set[str]:
        ids = set()
        for a in node.find_all("a", href=True):
            m = DETAIL_RE.search(a["href"])
            if m:
                ids.add(m.group(1))
        return ids

    def _parse_search_page(self, soup: BeautifulSoup) -> List[Dict[str, Any]]:
        cards: Dict[str, Dict[str, Any]] = {}

        for a in soup.find_all("a", href=True):
            m = DETAIL_RE.search(a["href"])
            if not m:
                continue
            vid = m.group(1)
            if vid in cards:
                continue

            # Remonter jusqu'au conteneur englobant uniquement cette carte véhicule
            node = a
            for _ in range(8):
                parent = node.parent
                if parent is None or len(self._ids_under(parent)) > 1:
                    break
                if len(parent.get_text(" ", strip=True)) > 900:
                    break
                node = parent

            raw_text = re.sub(r"\s+", " ", node.get_text(" | ", strip=True))
            full_url = urljoin(self.BASE, a["href"])
            parsed = self._parse_card_container(node, raw_text, full_url, vid)
            if parsed:
                cards[vid] = parsed
                self.stats["cards"] += 1

        return list(cards.values())

    def _parse_card_container(
        self, node, raw_text: str, url: str, vid: str
    ) -> Optional[Dict[str, Any]]:
        # Sur Renew, le titre principal suit généralement le format "Marque - Modèle" (ex: "Renault - 5")
        brand: Optional[str] = None
        model: Optional[str] = None

        # Recherche dans les balises de titre (h2, h3, strong) en priorité
        for tag in node.find_all(["h2", "h3", "h4", "strong", "a"]):
            t = re.sub(r"\s+", " ", tag.get_text(" ", strip=True)).strip()
            if " - " in t and len(t) < 60:
                parts = [p.strip() for p in t.split(" - ", 1)]
                if parts[0] and parts[1]:
                    brand, model = parts[0], parts[1]
                    break

        # Fallback regex sur le texte brut
        if not brand:
            m_bm = re.search(
                r"\b(Renault|Dacia|Nissan|Peugeot|Citro[ëe]n|Opel|Fiat|Volkswagen|VW|Škoda|Skoda|"
                r"Seat|Cupra|Audi|MINI|Hyundai|Kia|Suzuki|Honda|Ford|Alpine|BYD|MG|Smart|Volvo)\s*-\s*([A-Za-z0-9ëéèê\-\s]+?)(?=\s*\||$)",
                raw_text,
                re.IGNORECASE,
            )
            if m_bm:
                brand = m_bm.group(1).strip()
                model = m_bm.group(2).strip()

        if not brand:
            return None

        # Prix TVAc
        pm = PRICE_RE.search(raw_text)
        price_val = _to_int(pm.group(1)) if pm else None

        # Date de mise en circulation (MM/YYYY)
        dm = DATE_RE.search(raw_text)
        year_val = int(dm.group(2)) if dm else None

        # Kilométrage
        km_m = KM_RE.search(raw_text)
        km_val = _to_int(km_m.group(1)) if km_m else None

        # Garantie
        wm = WARRANTY_RE.search(raw_text)
        warranty_months = int(wm.group(1)) if wm else None

        # Concessionnaire / localisation (ex: "CENTRE-AUTOMOBILES SA (Mons)", "GROUPE AUTOSPHERE - BRUSSELS EAST")
        dealer = None
        dealer_m = re.search(
            r"(?:TVAc|TTC)\s*\|\s*([^|]+?)(?=\s*\||$)", raw_text, re.IGNORECASE
        )
        if dealer_m:
            dealer = dealer_m.group(1).strip()

        return {
            "id": vid,
            "url": url,
            "brand": brand,
            "model": model or "",
            "raw_text": raw_text,
            "price": price_val,
            "year": year_val,
            "km": km_val,
            "warranty_months": warranty_months,
            "dealer": dealer,
        }

    # ------------------------------------------------------------------
    # Filtrage & Conversion en CarListing
    # ------------------------------------------------------------------
    def _segment_of(self, brand: str, model: str) -> Optional[str]:
        key = _norm(f"{brand}{model}")
        for seg, pattern in _SEGMENT_PATTERNS:
            if re.search(pattern, key):
                return seg
        return None

    def _card_to_listing(self, card: Dict[str, Any]) -> Optional[CarListing]:
        brand = card["brand"]
        model = card["model"]
        raw_text = card["raw_text"]

        # 1. Filtre marque via BrandFilter
        if not self.brand_filter.is_brand_allowed(brand):
            self.stats["filtered_brands"] += 1
            return None

        # 2. Filtre segment (A, B, B-SUV) si activé
        if self.segments and self._segment_of(brand, model) not in self.segments:
            self.stats["segment"] += 1
            return None

        # 3. Exclusion optionnelle des véhicules de démo indisponibles immédiatement ("DISPO 10/02/27")
        if self.exclude_future_availability and re.search(r"\bdispo\s+\d{2}/\d{2}/\d{2,4}", raw_text, re.IGNORECASE):
            self.stats["future_dispo"] += 1
            return None

        # Extraction d'un sous-titre / version propre
        title = f"{brand} {model}".strip()
        version_m = re.search(
            r"\bKM\s*\|\s*([^|]+?)(?=\s*\|\s*[\d\s.]+\s*€|$)", raw_text, re.IGNORECASE
        )
        if version_m:
            version_str = version_m.group(1).strip()
            if version_str and "garantie" not in version_str.lower():
                title = f"{title} {version_str}"

        listing = CarListing(
            source="renew",
            title=title,
            brand=brand,
            model=model or None,
            price=card["price"],
            mileage_km=card["km"],
            year=card["year"],
            dealer_city=card["dealer"],
            url=card["url"],
            warranty_months=card["warranty_months"],
            has_service_history=True,  # Engagement standard réseau Renew (150 points + historique)
        )

        # Enrichissement immédiat depuis le texte de la carte (ex: "52 KWH", "40kWh", "SOH 98%")
        self._enrich_from_text(listing, raw_text)
        return listing

    # ------------------------------------------------------------------
    # Parsing de la fiche détaillée (details.html?productId=VEH_...)
    # ------------------------------------------------------------------
    def _parse_detail_page(self, soup: BeautifulSoup) -> Dict[str, Any]:
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
        d: Dict[str, Any] = {"text": text}

        # 1. Lecture éventuelle du JSON-LD structuré
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                payload = json.loads(script.string or "")
                items = payload if isinstance(payload, list) else [payload]
                for obj in items:
                    if not isinstance(obj, dict):
                        continue
                    if "offers" in obj and isinstance(obj["offers"], dict):
                        p = obj["offers"].get("price")
                        if p:
                            d["price"] = int(float(p))
                    if "mileageFromOdometer" in obj and isinstance(obj["mileageFromOdometer"], dict):
                        v = obj["mileageFromOdometer"].get("value")
                        if v:
                            d["km"] = _to_int(v)
                    if "vehicleModelDate" in obj:
                        d["year"] = _to_int(str(obj["vehicleModelDate"])[:4])
            except Exception:
                pass

        # 2. Extraction par expressions régulières sur le texte de la fiche
        def grab(pattern: str) -> Optional[str]:
            m = re.search(pattern, text, re.IGNORECASE)
            return m.group(1).strip() if m else None

        kw = _to_int(grab(r"Puissance\s*(?:fiscale|r[ée]elle|moteur)?\s*:?\s*(\d+)\s*kW\b"))
        cv = _to_int(grab(r"(\d+)\s*(?:ch|cv|hp)\b"))
        d["kw"] = kw
        d["cv"] = cv

        batt = grab(r"(?:Capacit[ée]\s*(?:de la )?batterie|Batterie)\s*:?\s*(\d{2}(?:[.,]\d{1,2})?)\s*kWh")
        if batt:
            d["battery_kwh"] = float(batt.replace(",", "."))

        wltp = _to_int(grab(r"Autonomie\s*(?:WLTP|th[ée]orique|mixte)?\s*:?\s*(\d{3})\s*km"))
        if wltp:
            d["range_km"] = wltp

        city = grab(r"(?:en stock chez|concessionnaire|point de vente)\s+[^|]+?\s+[àa]\s+([A-ZÀ-Ÿ][A-Za-zÀ-ÿ\-\s]+?)(?=\s*[\.|,]|$)")
        if city:
            d["city"] = city

        return d

    def _apply_detail(self, car: CarListing, d: Dict[str, Any]) -> None:
        if car.price is None and d.get("price"):
            car.price = d["price"]
        if car.mileage_km is None and d.get("km"):
            car.mileage_km = d["km"]
        if car.year is None and d.get("year"):
            car.year = d["year"]
        if car.battery_kwh is None and d.get("battery_kwh") and 10 <= d["battery_kwh"] <= 160:
            car.battery_kwh = d["battery_kwh"]
        if car.range_wltp_km is None and d.get("range_km"):
            car.range_wltp_km = d["range_km"]
        if car.power_hp is None:
            car.power_hp = d.get("cv") or (round(d["kw"] * 1.35962) if d.get("kw") else None)
        if d.get("city"):
            car.dealer_city = d["city"]

        self._enrich_from_text(car, d.get("text", ""))

    def _enrich_from_text(self, car: CarListing, text: str) -> None:
        if not text:
            return
        if car.battery_kwh is None:
            for m in re.finditer(r"(\d{2}(?:[.,]\d{1,2})?)\s*kwh\b", text, re.IGNORECASE):
                v = float(m.group(1).replace(",", "."))
                if 15 <= v <= 100:
                    car.battery_kwh = v
                    break
        if car.range_wltp_km is None:
            m = re.search(r"(\d{3})\s*km\s*wltp\b", text, re.IGNORECASE)
            if m and 100 <= int(m.group(1)) <= 700:
                car.range_wltp_km = int(m.group(1))
        if car.soh_percent is None:
            m = re.search(
                r"(?:SOH|state of health|[ée]tat de (?:la )?batterie)\s*:?\s*(\d{2,3})(?:[.,]\d+)?\s*%",
                text,
                re.IGNORECASE,
            )
            if m and 50 <= int(m.group(1)) <= 100:
                car.soh_percent = int(m.group(1))


if __name__ == "__main__":
    from market_estimator import MarketEstimator
    from pipeline import CarPipeline

    max_pages = 5  # ~88 véhicules électriques au total sur Renew BE = 4 à 5 pages suffisent
    helper = CarHelper(enrich_details=True, headless=False)

    # On filtre directement sur les segments A, B et B-SUV (<= 4m20)
    scraper = RenewScraper(
        helper=helper,
        segments={"A", "B", "B-SUV"},
        exclude_future_availability=False,
    )

    pipeline = CarPipeline(scrapers=[scraper], helper=helper)

    print("=== TEST DU PIPELINE : Renew Belgique ===")
    resolved_cars = pipeline.run_single(scraper, max_pages=max_pages, verbose=True)

    if resolved_cars:
        estimator = MarketEstimator(output_filename="opportunities_cars_renew")
        estimator.process_and_export(resolved_cars, verbose=True)
    else:
        print("⚠️ Aucun véhicule extrait pour Renew.")