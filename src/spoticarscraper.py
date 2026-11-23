"""Scraper Spoticar.be (réseau d'occasion Stellantis) : extrait des FAITS BRUTS des cartes du catalogue.

Le catalogue se charge par clics successifs sur « Afficher plus » : on pilote Chromium (Playwright), puis
on analyse le HTML final (`_parse_cards`, fonction pure).

Ce scraper ne fait ni cache, ni filtrage par référentiel, ni résolution de variante : ces étapes
appartiennent au pipeline (raw_listings.json, match, resolve).
"""
import json
import re
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from cardata import CarListing, CarScraperBase
from carhelper import CarHelper


class SpoticarScraper(CarScraperBase):
    URL = "https://www.spoticar.be/fr/voitures-occasion?page=1&filters[0][energy]=electrique&gps[lat]=50.647693&gps[lon]=5.55916&filters[3][location][lat]=50.647693&filters[3][location][lon]=5.55916&filters[3][location][place]=4000%2C%20Li%C3%A8ge%2C%20Belgique&filters[3][location][radius]=50&filters[4][category]=citadine"
    SITE = "https://www.spoticar.be"
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        " (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    def __init__(self, helper: CarHelper):
        self.helper = helper
        self.headless = helper.headless

    # ------------------------------------------------------------------
    def scrape_base_data(self, max_pages: int = 200) -> List[CarListing]:
        """`max_pages` = nombre de lots de cartes chargés (1 lot initial + max_pages-1 clics)."""
        print("[Spoticar] Chargement du catalogue complet (clics sur 'Afficher plus')...")
        html = self._load_catalog_html(max_pages)
        listings = self._parse_cards(html)
        print(f"[Spoticar] Total : {len(listings)} véhicules extraits.")
        return listings

    # ------------------------------------------------------------------
    # Navigateur
    # ------------------------------------------------------------------
    def _load_catalog_html(self, max_pages: int) -> str:
        """HTML de la page après chargement. En cas d'erreur de navigation, renvoie ce qui est déjà chargé
        (des résultats partiels valent mieux que rien : le pipeline les fusionne dans raw_listings.json)."""
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            try:
                context = browser.new_context(
                    user_agent=self.USER_AGENT,
                    viewport={"width": 1366, "height": 768},
                    locale="fr-BE",
                )
                context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                page = context.new_page()
                try:
                    page.goto(self.URL, wait_until="domcontentloaded", timeout=60000)
                    self._dismiss_cookies(page)
                    self._wait_for_cards(page)
                    self._load_more(page, max(0, max_pages - 1))
                except Exception as e:
                    print(f"[Spoticar] Erreur lors de la navigation : {e}")
                    try:
                        page.screenshot(path="spoticar_debug.png")
                    except Exception:
                        pass
                return page.content()
            finally:
                browser.close()

    @staticmethod
    def _dismiss_cookies(page) -> None:
        try:
            page.wait_for_selector("#_psaihm_id_accept_all_btn", timeout=30000)
        except Exception:
            print("[Spoticar] Avertissement : bandeau de cookies non apparu, on continue.")
        for selector in ("#_psaihm_id_accept_all_btn", "button.cta-banner"):
            try:
                btn = page.locator(selector).first
                if btn.is_visible(timeout=2500):
                    btn.click()
                    page.wait_for_timeout(1000)
            except Exception:
                print(f"[Spoticar] Impossible de fermer le bandeau ({selector}).")

    @staticmethod
    def _wait_for_cards(page) -> None:
        try:
            page.wait_for_selector("div.vehicle-card", timeout=30000)
        except Exception:
            print("[Spoticar] Avertissement : délai d'attente initial dépassé, tentative de lecture...")

    @staticmethod
    def _load_more(page, clicks: int) -> int:
        """Clique sur « Afficher plus » jusqu'à `clicks` fois. S'arrête proprement quand le bouton disparaît
        (fin du catalogue). Renvoie le nombre de clics effectués."""
        done = 0
        for _ in range(clicks):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            btn = page.locator("button#see-more-results").first
            try:
                btn.wait_for(state="visible", timeout=8000)
            except Exception:
                print("[Spoticar] Bouton 'Afficher plus' absent : fin du catalogue atteinte.")
                break
            try:
                btn.scroll_into_view_if_needed()
                btn.click()
            except Exception as e:
                print(f"[Spoticar] Clic impossible ({e.__class__.__name__}) : arrêt du chargement.")
                break
            done += 1
            print(f"[Spoticar] Clic sur 'Afficher plus' ({done}/{clicks})...")
            page.wait_for_timeout(2500)
        return done

    # ------------------------------------------------------------------
    # Analyse du HTML (pure, testable hors navigateur)
    # ------------------------------------------------------------------
    def _parse_cards(self, html: str) -> List[CarListing]:
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.find_all("div", class_="vehicle-card")
        print(f"[Spoticar] {len(cards)} cartes de véhicules trouvées.")

        listings: List[CarListing] = []
        seen_urls = set()
        skipped = 0
        for card in cards:
            try:
                listing = self._parse_card(card)
            except Exception as e:  # une carte mal formée ne doit pas faire perdre les autres
                print(f"[Spoticar] Carte ignorée : {e}")
                skipped += 1
                continue
            if listing is None:
                skipped += 1
                continue
            if listing.url in seen_urls:
                continue
            seen_urls.add(listing.url)
            listings.append(listing)
        if skipped:
            print(f"[Spoticar] {skipped} carte(s) ignorée(s) (sans lien d'annonce ou illisibles).")
        return listings

    @staticmethod
    def _vehicle_data(raw_data: Any) -> Dict[str, Any]:
        """Bloc `vehicles` du JSON embarqué : clé racine connue (AP, OV, OC) ou la première qui en contient un."""
        if not isinstance(raw_data, dict):
            return {}
        for key in ("AP", "OV", "OC"):
            block = raw_data.get(key)
            if isinstance(block, dict) and "vehicles" in block:
                return block["vehicles"]
        for val in raw_data.values():
            if isinstance(val, dict) and "vehicles" in val:
                return val["vehicles"]
        return {}

    def _parse_card(self, card) -> Optional[CarListing]:
        link_elem = card.find("a", class_="vehicle-card-link", href=True)
        if not link_elem:
            return None
        href = link_elem["href"]
        car_url = f"{self.SITE}{href}" if href.startswith("/") else href

        title_block = card.find("div", class_="vehicle-card-title")
        title = (
            " ".join(title_block.get_text(" ", strip=True).split()).rstrip(" -") if title_block else "Inconnu"
        )

        price = mileage = year = power_hp = brand = model = None

        hidden_input = card.find("input", class_="multiple_teaser_data")
        if hidden_input and hidden_input.has_attr("value"):
            try:
                veh_data = self._vehicle_data(json.loads(hidden_input["value"]))
                if veh_data:
                    if veh_data.get("pricing", {}).get("netPriceInclTax") is not None:
                        price = float(veh_data["pricing"]["netPriceInclTax"])
                    if veh_data.get("features", {}).get("maximumPowerHP") is not None:
                        power_hp = int(veh_data["features"]["maximumPowerHP"])
                    state_data = veh_data.get("state", {})
                    if state_data.get("mileage"):
                        mileage = int(state_data["mileage"])
                    if state_data.get("firstRegistrationDate"):
                        year = int(state_data["firstRegistrationDate"].split("-")[0])
                    long_label = veh_data.get("presentation", {}).get("longLabel", "")
                    if long_label:
                        model = long_label.split(" ")[0].capitalize()
            except Exception as e:
                print(f"[Spoticar] Données JSON illisibles ({car_url}) : {e}")

        warranty_months = None
        warranty_block = card.find("div", class_="vehicle-card-warranty")
        if warranty_block:
            w_match = re.search(r"(\d+)\s*mois", warranty_block.get_text(), re.IGNORECASE)
            if w_match:
                warranty_months = int(w_match.group(1))

        h3 = card.find("h3")
        if h3:
            main_name = "".join(t for t in h3.contents if isinstance(t, str)).strip()
            if main_name:
                parts = main_name.split(" ", 1)
                brand = parts[0].capitalize()
                if len(parts) > 1:
                    model = parts[1]

        batt_match = re.search(r"(\d+(?:[\.,]\d+)?)\s*kwh", title, re.IGNORECASE)
        battery_kwh = float(batt_match.group(1).replace(",", ".")) if batt_match else None

        soh_percent = None
        soh_tag = card.find("span", class_="soh-tooltip")
        if soh_tag:
            soh_match = re.search(r"(\d+)\s*%", soh_tag.get_text())
            if soh_match:
                soh_percent = int(soh_match.group(1))

        dealer_city = None
        dealer_addr = card.find("div", class_="dealer-address")
        if dealer_addr:
            for s in dealer_addr.find_all("span", recursive=False):
                txt = s.get_text(strip=True)
                if txt and not txt.startswith("("):
                    dealer_city = txt
                    break

        if price is None:
            price_span = card.find("div", class_="cash")
            if price_span:
                m = re.search(r"([\d\s\xa0\.,]+)\s*€", price_span.get_text())
                if m:
                    price = self.helper._parse_price(m.group(0))
        if price is None:
            print(f"[Spoticar] Annonce sans prix : {car_url}")

        return CarListing(
            source="spoticar",
            title=title,
            brand=brand,
            model=model,
            price=price,
            mileage_km=mileage,
            year=year,
            battery_kwh=battery_kwh,
            power_hp=power_hp,
            soh_percent=soh_percent,
            warranty_months=warranty_months,
            dealer_city=dealer_city,
            url=car_url,
        )

    def scrape_details(self, listing: CarListing) -> CarListing:
        """Les cartes contiennent déjà toutes les données disponibles : rien à compléter."""
        return listing


if __name__ == "__main__":
    from market_estimator import MarketEstimator
    from pipeline import CarPipeline

    max_pages = 10  # Ajuste selon tes besoins de test
    helper = CarHelper(enrich_details=True, headless=False)
    scraper = SpoticarScraper(helper=helper)
    pipeline = CarPipeline(scrapers=[scraper], helper=helper, resolved_db_path="resolved_cars.json")

    print("=== TEST DU PIPELINE : Spoticar ===")
    resolved_cars = pipeline.run_single(scraper, max_pages=max_pages, verbose=True)

    if resolved_cars:
        estimator = MarketEstimator(output_filename="opportunities")
        estimator.process_and_export(resolved_cars, verbose=True)
    else:
        print("⚠️ Aucun véhicule extrait pour Spoticar.")
