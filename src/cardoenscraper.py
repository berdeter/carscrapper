import time
import random
from cardata import CarScraperBase
import requests
from bs4 import BeautifulSoup
import json
import re
from typing import List, Optional, Any
from bs4 import BeautifulSoup
import requests
from cardata import CarScraperBase, CarListing, DetailsFetchError, DetailsGoneError
import requests
from bs4 import BeautifulSoup
from carhelper import CarHelper

class CardoenScraper(CarScraperBase):
    TIMEOUT = 15  # secondes : sans timeout, un serveur muet bloquait tout le pipeline

    def __init__(self, helper: CarHelper):
        self.helper = helper   
    def scrape_base_data(self, max_pages: int = 200)  -> List[CarListing]:
        base_url = "https://www.cardoen.be/fr/achat/occasions/citadine/electrique/"
        print(f"[Cardoen] Chargement des annonces avec pagination...")
        listings = []
        seen_urls = set()
        page_num = 1

        while page_num <= max_pages:
            url = f"{base_url}?page={page_num}" if page_num > 1 else base_url
            try:
                response = requests.get(url, headers=self.helper.headers, timeout=self.TIMEOUT)
                if response.status_code != 200:
                    url = f"{base_url}?p={page_num}"
                    response = requests.get(url, headers=self.helper.headers, timeout=self.TIMEOUT)
            except requests.RequestException as e:
                print(f"[Cardoen] Erreur réseau page {page_num} : {e} ({len(listings)} annonces conservées)")
                break
            if response.status_code != 200:
                break

            soup = BeautifulSoup(response.text, "html.parser")
            cards = soup.find_all("article", class_=lambda x: x and "card" in x.lower()) or soup.find_all("div", class_=lambda x: x and "vehicle" in x.lower())
            if not cards:
                    # Fallback de sélection large si la structure a changé
                    cards = soup.find_all("article")
            
            if not cards:
                    break

            new_cards_count = 0
            for card in cards:
                title_elem = card.find(
                    ["h2", "h3", "a"], class_=lambda x: x and any(t in x.lower() for t in ["title", "name", "model"])
                ) or card.find("a")
                
                # Récupération de l'URL de l'annonce
                link_elem = card.find("a", href=True)
                relative_url = link_elem["href"] if link_elem else (title_elem.get("href") if title_elem and title_elem.has_attr("href") else None)
                
                car_url = (
                    f"https://www.cardoen.be{relative_url}"
                    if relative_url and relative_url.startswith("/")
                    else relative_url
                )

                if not car_url or car_url in seen_urls or not ("/auto/" in car_url):
                    continue
                seen_urls.add(car_url)
                new_cards_count += 1

                title = title_elem.get_text(strip=True) if title_elem else ""

                # Extraction du prix sur la carte (recherche large de classes contenant price/amount/cost)
                price_elem = card.find(
                    class_=lambda x: x and any(c in x.lower() for c in ["price", "amount", "cost", "cash"])
                )
                price = self.helper._parse_price(
                    price_elem.get_text(strip=True) if price_elem else None
                )

                full_text = card.get_text(" ", strip=True)
                mileage = self.helper._extract_mileage(full_text)
                year = self.helper._extract_year(full_text)
                battery = self.helper._extract_battery(full_text)
                range_wltp = self.helper._extract_wltp(full_text)

                url_brand, url_model, url_batt, url_hp = (
                    self._extract_info_from_cardoen_url(car_url)
                )
                brand = url_brand
                model = url_model
                battery = battery or url_batt
                power_hp = url_hp

                if not title or title == "Inconnu":
                    title = f"{brand or ''} {model or ''}".strip()

                listing = CarListing(
                    source="cardoen",
                    title=title,
                    brand=brand,
                    model=model,
                    price=price,
                    mileage_km=mileage,
                    year=year,
                    battery_kwh=battery,
                    range_wltp_km=range_wltp,
                    power_hp=power_hp,
                    url=car_url,
                )


                listings.append(listing)

            if new_cards_count == 0:
                    break
            print(f"[Cardoen] Page {page_num} traitée ({new_cards_count} véhicules).")
            page_num += 1
            time.sleep(random.uniform(0.8, 1.6))  # politesse envers le serveur

        print(f"[Cardoen] Total : {len(listings)} véhicules extraits.")
        return listings


    def scrape_details(self, listing) -> Any:
            # Récupération sécurisée de l'objet CarListing sous-jacent
            car_obj = listing.listing if hasattr(listing, "listing") else listing
            
            # Pas de try/except global : une erreur remonte au pipeline, qui retentera la fiche plus tard.
            resp = requests.get(car_obj.url, headers=self.helper.headers, timeout=self.TIMEOUT)
            if resp.status_code in (404, 410):
                raise DetailsGoneError(f"HTTP {resp.status_code}")
            if resp.status_code != 200:
                raise DetailsFetchError(f"HTTP {resp.status_code}")

            soup = BeautifulSoup(resp.text, "html.parser")

            # 1. Recherche JSON-LD
            for ld_script in soup.find_all("script", type="application/ld+json"):
                try:
                    data = json.loads(ld_script.string)
                    items = data if isinstance(data, list) else [data]
                    for item in items:
                        offers = item.get("offers", {})
                        if isinstance(offers, list) and offers:
                            offers = offers[0]
                        if isinstance(offers, dict):
                            p = offers.get("price") or item.get("price")
                            if p and not car_obj.price:
                                parsed_p = self.helper._parse_price(str(p))
                                if parsed_p:
                                    car_obj.price = parsed_p
                except Exception:
                    pass

            # 2. OpenGraph
            if not car_obj.price:
                og_price = soup.find(
                    "meta", property=re.compile(r"price:amount|product:price:amount")
                )
                if og_price and og_price.get("content"):
                    car_obj.price = self.helper._parse_price(og_price["content"])

            page_text = soup.get_text(" ", strip=True)

            # 3. Balises HTML de prix
            if not car_obj.price:
                price_candidates = soup.find_all(
                    class_=lambda x: x and any(c in x.lower() for c in ["price", "amount", "cash", "valeur"])
                )
                for elem in price_candidates:
                    cand = self.helper._parse_price(elem.get_text())
                    if cand:
                        car_obj.price = cand
                        break

            # 4. Fallback textuel
            if not car_obj.price:
                for m in re.finditer(
                    r"\d{1,3}(?:[\s\xa0\.]\d{3})*(?:,\d{1,2})?\s*(?:€|EUR)", page_text
                ):
                    candidate = self.helper._parse_price(m.group(0))
                    if candidate:
                        car_obj.price = candidate
                        break

            # SOH, garantie, etc.
            if not car_obj.soh_percent:
                soh_match = re.search(
                    r"(?:SOH|État de la batterie)[^\d]*(\d+)\s*%",
                    page_text,
                    re.IGNORECASE,
                )
                if soh_match:
                    try:
                        car_obj.soh_percent = int(soh_match.group(1))
                    except ValueError:
                        pass

            if not car_obj.warranty_months:
                w_match = re.search(r"garantie[^\d]*(\d+)\s*mois", page_text, re.IGNORECASE)
                if w_match:
                    car_obj.warranty_months = int(w_match.group(1))

            if car_obj.has_service_history is None:
                if "carnet d'entretien" in page_text.lower() or "historique d'entretien" in page_text.lower():
                    car_obj.has_service_history = True

            if not car_obj.power_hp:
                hp_match = re.search(r"(\d{2,3})\s*(?:ch|hp|CV)", page_text, re.IGNORECASE)
                if hp_match:
                    car_obj.power_hp = int(hp_match.group(1))

            if not car_obj.battery_kwh:
                batt_match = re.search(r"([\d\.,]+)\s*(?:kWh|kwh)", page_text)
                if batt_match:
                    car_obj.battery_kwh = float(batt_match.group(1).replace(",", "."))

            return listing  
    
    # ==========================================
    # HELPERS & ENRICHISSEMENTS (Inchangés)
    # ==========================================
    def _extract_info_from_cardoen_url(
        self, url: Optional[str]
    ) -> tuple[Optional[str], Optional[str], Optional[float], Optional[int]]:
        if not url:
            return None, None, None, None
        brand, model, battery, power_hp = None, None, None, None
        match = re.search(r"/auto/([^/]+)/([^/]+)/([^/?]+)", url)
        if match:
            brand = match.group(1).capitalize()
            model = match.group(2).replace("-", " ").title()
            version_slug = match.group(3)
            batt_match = re.search(r"(\d+-\d+|\d+)-kwh", version_slug, re.IGNORECASE)
            if batt_match:
                try:
                    battery = float(batt_match.group(1).replace("-", "."))
                except ValueError:
                    pass
            hp_candidates = re.findall(
                r"-(\d{2,3})(?:-at|-mt|-bva|-bvm|/|-|$)", version_slug, re.IGNORECASE
            )
            for cand in hp_candidates:
                    val = int(cand)
                    if 50 <= val <= 500:
                        power_hp = val
                        break
        return brand, model, battery, power_hp

from carhelper import CarHelper
from market_estimator import MarketEstimator
from pipeline import CarPipeline

if __name__ == "__main__":
    max_pages = 10  # Ajuste selon tes besoins de test
    helper = CarHelper(enrich_details=True, headless=False)
    
    # Instanciation spécifique du scraper AutoScout24
    scraper = CardoenScraper(helper=helper)
    
    # Initialisation du pipeline avec ce scraper unique
    pipeline = CarPipeline(scrapers=[scraper], helper=helper, resolved_db_path="resolved_cars.json")
    
    print("=== TEST DU PIPELINE : AUTOSCOUT24 ===")
    resolved_cars = pipeline.run_single(scraper, max_pages=max_pages, verbose=True)
    
    # Analyse de marché et export spécifique
    if resolved_cars:
        estimator = MarketEstimator(output_filename="opportunities_cars")
        estimator.process_and_export(resolved_cars, verbose=True)
    else:
        print("⚠️ Aucun véhicule extrait pour Cardoen.")    

    