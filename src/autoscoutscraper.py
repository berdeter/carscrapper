"""Scraper AutoScout24.be : extrait des FAITS BRUTS depuis le JSON `__NEXT_DATA__` des pages de liste.

Ce scraper ne fait ni cache, ni filtrage par référentiel, ni déduction de variante :
  * le cache et le rejeu relèvent du pipeline (raw_listings.json) ;
  * les déductions de spécifications (ex. « iv-80 -> 77 kWh ») sont dans `spec_hints.py`.
Il ne remplit que ce que le site affirme (y compris dans l'URL : marque, modèle, « 52-kwh »).
"""
import json
import random
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

from cardata import CarListing, CarScraperBase
from carhelper import CarHelper


def _to_float(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = re.sub(r"[^\d.,]", "", value).replace(",", ".")
        try:
            return float(cleaned) if cleaned else None
        except ValueError:
            return None
    return None


def _to_int(value: Any) -> Optional[int]:
    number = _to_float(value)
    return int(number) if number is not None else None


def _year_of(value: Any) -> Optional[int]:
    """'07-2022' / '2022-07' / 2022 -> 2022."""
    if value is None:
        return None
    match = re.search(r"\b(19\d{2}|20\d{2})\b", str(value))
    return int(match.group(1)) if match else None


class AutoscoutScraper(CarScraperBase):
    BASE_URL = (
        "https://www.autoscout24.be/fr/lst/cit_liege-belgium/bt_citadine/ft_electrique?cy=B&damaged_listing=exclude&desc=0&powerfrom=20&powertype=kw&pricefrom=5000&sort=standard&ustate=N%2CU&zip=Li%C3%A8ge+%28Belgium%29&zipr=50&lat=50.64396&lon=5.57074&atype=C&search_id=m9mmcxptw6&source=listpage_pagination"
    )
    SITE = "https://www.autoscout24.be"

    def __init__(self, helper: CarHelper):
        self.helper = helper

    # ------------------------------------------------------------------
    def scrape_base_data(self, max_pages: int = 200) -> List[CarListing]:
        print(f"[AutoScout24] Chargement des annonces (jusqu'à {max_pages} pages)...")
        listings: List[CarListing] = []
        seen_urls = set()

        for page_num in range(1, max_pages + 1):
            sep = "&" if "?" in self.BASE_URL else "?"
            url = f"{self.BASE_URL}{sep}page={page_num}" if page_num > 1 else self.BASE_URL

            try:
                response = requests.get(url, headers=self.helper.headers, timeout=15)
            except requests.RequestException as e:
                print(f"[AutoScout24] Erreur réseau page {page_num} : {e}")
                break
            if response.status_code != 200:
                print(f"[AutoScout24] Arrêt sur page {page_num} (HTTP {response.status_code}).")
                break

            try:
                items = self._extract_items(response.text)
            except Exception as e:
                print(f"[AutoScout24] Erreur parsing page {page_num} : {e}")
                break
            if items is None:
                print(f"[AutoScout24] Page {page_num} sans __NEXT_DATA__ : arrêt.")
                break
            if not items:
                print(f"[AutoScout24] Fin des résultats atteinte à la page {page_num}.")
                break

            page_added = 0
            for item in items:
                try:
                    listing = self._item_to_listing(item)
                except Exception as e:  # une annonce mal formée ne doit pas faire perdre la page
                    print(f"[AutoScout24] Annonce ignorée (page {page_num}) : {e}")
                    continue
                if listing is None or not listing.url or listing.url in seen_urls:
                    continue
                seen_urls.add(listing.url)
                listings.append(listing)
                page_added += 1

            print(
                f"[AutoScout24] Page {page_num} traitée ({page_added} nouveaux"
                f" véhicules | Total cumulé : {len(listings)})."
            )
            if page_added == 0:
                break

            # Pause courtoise entre deux PAGES pour éviter le bannissement IP
            time.sleep(random.uniform(0.8, 1.6))

        print(f"[AutoScout24] Total final : {len(listings)} véhicules extraits.")
        return listings

    @staticmethod
    def _extract_items(html: str) -> Optional[List[Dict[str, Any]]]:
        """Liste des annonces du JSON `__NEXT_DATA__` ; None si la page n'en contient pas."""
        soup = BeautifulSoup(html, "html.parser")
        script_tag = soup.find("script", id="__NEXT_DATA__")
        if not script_tag or not script_tag.string:
            return None
        data = json.loads(script_tag.string)
        page_props = data.get("props", {}).get("pageProps", {})
        search_result = page_props.get("searchResult", {})
        return search_result.get("listings", []) or page_props.get("listings", [])

    def _item_to_listing(self, item: Dict[str, Any]) -> Optional[CarListing]:
        vehicle = item.get("vehicle", item)
        prices = item.get("prices") or {}
        tracking = item.get("tracking") or {}
        price_obj = item.get("price")

        price_val = (
            (prices.get("tracking") or {}).get("price")
            or vehicle.get("price")
            or (price_obj.get("priceRaw") if isinstance(price_obj, dict) else None)
        )

        make = vehicle.get("make")
        model = vehicle.get("model")
        title = f"{make or ''} {model or ''}".strip() or item.get("title", "Inconnu")

        mileage = tracking.get("mileage") or vehicle.get("mileageInKm") or item.get("mileage")
        year = _year_of(tracking.get("firstRegistration"))
        hp = vehicle.get("hp") or vehicle.get("powerInHp")

        path = item.get("url") or vehicle.get("url")
        car_url = f"{self.SITE}{path}" if path and path.startswith("/") else path
        if not car_url:
            return None

        url_brand, url_model, url_batt = self._extract_info_from_autoscout_url(car_url)
        final_brand = make or url_brand
        final_model = model or url_model
        if not title or title == "Inconnu":
            title = f"{final_brand or ''} {final_model or ''}".strip()

        return CarListing(
            source="autoscout24",
            title=title,
            brand=final_brand,
            model=final_model,
            price=_to_float(price_val),
            mileage_km=_to_int(mileage),
            year=year,
            battery_kwh=url_batt,
            power_hp=_to_int(hp),
            url=car_url,
        )

    # ------------------------------------------------------------------
    def scrape_details(self, listing: CarListing) -> CarListing:
        car = listing.listing if hasattr(listing, "listing") else listing
        try:
            resp = requests.get(car.url, headers=self.helper.headers, timeout=12)
            if resp.status_code != 200:
                return listing
            soup = BeautifulSoup(resp.text, "html.parser")
            page_text = soup.get_text(" ", strip=True)
            if not car.price:
                og_price = soup.find("meta", property=re.compile(r"price:amount|product:price:amount"))
                if og_price and og_price.get("content"):
                    car.price = self.helper._parse_price(og_price["content"])
            if not car.price:
                price_match = re.search(r"([\d\s\xa0\.,]+)\s*(?:€|EUR)", page_text)
                if price_match:
                    car.price = self.helper._parse_price(price_match.group(0))
            if not car.mileage_km:
                car.mileage_km = self.helper._extract_mileage(page_text)
            if not car.year:
                car.year = self.helper._extract_year(page_text)
            if not car.battery_kwh:
                car.battery_kwh = self.helper._extract_battery(page_text)
            if not car.warranty_months:
                w_match = re.search(r"garantie[^\d]*(\d+)\s*mois", page_text, re.IGNORECASE)
                if w_match:
                    car.warranty_months = int(w_match.group(1))
            if car.has_service_history is None:
                if "carnet d'entretien" in page_text.lower():
                    car.has_service_history = True
        except Exception as e:
            print(f"[AutoScout24] Erreur d'enrichissement pour {car.url} : {e}")
        return listing

    @staticmethod
    def _extract_info_from_autoscout_url(
        url: Optional[str],
    ) -> Tuple[Optional[str], Optional[str], Optional[float]]:
        """Marque, modèle et capacité affichés dans l'URL (faits du site). Aucune déduction."""
        if not url:
            return None, None, None
        match = re.search(r"/offres/([^-_/]+)-([^-_/]+)(?:-([^_/]+))?", url)
        brand, model, battery = None, None, None
        if match:
            brand = match.group(1).capitalize()
            model = match.group(2).upper()
        batt_match = re.search(r"(\d+[\.,]?\d*)-kwh", url, re.IGNORECASE)
        if batt_match:
            try:
                battery = float(batt_match.group(1).replace(",", "."))
            except ValueError:
                pass
        return brand, model, battery


if __name__ == "__main__":
    from market_estimator import MarketEstimator
    from pipeline import CarPipeline

    max_pages = 10  # Ajuste selon tes besoins de test
    helper = CarHelper(enrich_details=True, headless=False)
    scraper = AutoscoutScraper(helper=helper)
    pipeline = CarPipeline(scrapers=[scraper], helper=helper, resolved_db_path="resolved_cars.json")

    print("=== TEST DU PIPELINE : AUTOSCOUT24 ===")
    resolved_cars = pipeline.run_single(scraper, max_pages=max_pages, verbose=True)

    if resolved_cars:
        estimator = MarketEstimator(output_filename="opportunities_cars")
        estimator.process_and_export(resolved_cars, verbose=True)
    else:
        print("⚠️ Aucun véhicule extrait pour AutoScout24.")
