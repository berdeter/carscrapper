"""Scraper Ayvens Used Cars Belgique (https://usedcars.ayvens.com/fr-be).

Retours de flotte de leasing vendus aux particuliers : prix fixe TVA/garantie 12 mois/
préparation inclus, historique d'entretien et Car-Pass, parfois SoH batterie.

Architecture du site (Salesforce Commerce Cloud) :
  * HTML rendu côté serveur : pas de navigateur ni de JSON intermédiaire nécessaire,
    `requests` + BeautifulSoup suffisent.
  * Les URL de recherche filtrées/paginées (``?prefn1=fuelType&prefv1=Electric&start=..&sz=..``)
    sont INTERDITES aux robots par le robots.txt du site. On ne les utilise donc pas :
    on parcourt les pages catalogue publiques ``/fr-be/voitures/<marque>`` (et, si une page
    marque est tronquée à 24 annonces, ``/fr-be/voitures/<marque>/<modèle>``) et on filtre
    nous-mêmes les véhicules électriques.
  * Le lieu où le véhicule peut être vu (« À voir à : Liège | Aartselaar ») n'est que sur la
    fiche de détail : elle sert au filtre de rayon autour de Liège.
  * Le robots.txt est téléchargé à l'exécution et appliqué avant CHAQUE requête.
"""
import math
import os
import re
import time
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from cardata import CarListing, CarScraperBase
from carhelper import CarHelper

LIEGE_LAT, LIEGE_LNG = 50.6326, 5.5797

# Lieux de visite connus (« À voir à »). Extensible via le paramètre `store_coordinates`.
DEFAULT_STORES: Dict[str, Tuple[float, float]] = {
    "liege": (50.6326, 5.5797),        # Mobility Center Liège, Av. Olympe Gilbart 1A, 4000
    "aartselaar": (51.1347, 4.3828),   # Showroom Anvers/Aartselaar
}

# Pré-filtre optionnel par segment (opt-in via `segments`). models.json du pipeline reste la
# référence ; ceci évite seulement de télécharger des fiches inutiles. Premier motif gagnant.
_SEGMENT_PATTERNS: List[Tuple[str, str]] = [
    ("B-SUV", r"^ds.*3crossback"), ("B-SUV", r"^hyundaikona"), ("B-SUV", r"^peugeote?2008"),
    ("B-SUV", r"^volvoex30"), ("B-SUV", r"^fiat600"), ("B-SUV", r"^jeepavenger"),
    ("B-SUV", r"^opelmokka"), ("B-SUV", r"^smart1$"), ("B-SUV", r"^fordpuma"),
    ("A", r"^fiat500"), ("A", r"^abarth500"), ("A", r"^smart(?:eq|for)"),
    ("A", r"^renaulttwingo"), ("A", r"^daciaspring"), ("A", r"^volkswagen(?:e)?up"),
    ("A", r"^skodacitigo"), ("A", r"^seatmii"), ("A", r"^hyundaiinster"),
    ("A", r"^leapmotort03"), ("A", r"^mitsubishiimiev"), ("A", r"^citroenczero"),
    ("A", r"^peugeotion"),
    ("B", r"^renaultzoe"), ("B", r"^renault5"), ("B", r"^peugeote?208"),
    ("B", r"^opelcorsa"), ("B", r"^dsds3"), ("B", r"^citroene?c3"),
    ("B", r"^minicooper"), ("B", r"^minielectric"), ("B", r"^hondae$"),
    ("B", r"^mg3"), ("B", r"^lanciaypsilon"),
]

DETAIL_RE = re.compile(r"/fr-be/[^/?#]+/(\d+)-[^/?#]*\.html")
PRICE_RE = re.compile(r"(Acheter pour|Prix attendu)\s*([\d\s.,\xa0\u202f]+?)\s*€", re.IGNORECASE)
SPEC_RE = re.compile(r"(\d{2}/\d{2}/\d{4})\s+([\d.\s\xa0\u202f]+?)\s*km\b", re.IGNORECASE)
COUNT_RE = re.compile(r"(\d+)\s+R[ée]sultats?", re.IGNORECASE)


def _norm(text: Any) -> str:
    s = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _to_int(s: Any) -> Optional[int]:
    digits = re.sub(r"\D", "", str(s or ""))
    return int(digits) if digits else None


class RobotsRules:
    """Mini-interpréteur robots.txt avec jokers `*` et `$` (que urllib.robotparser ignore)."""

    def __init__(self, text: str, user_agent: str):
        ua = user_agent.lower()
        groups: List[Tuple[List[str], List[Tuple[bool, str]]]] = []
        agents: Optional[List[str]] = None
        rules: List[Tuple[bool, str]] = []
        prev_agent = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, val = (x.strip() for x in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if not prev_agent or agents is None:
                    agents, rules = [], []
                    groups.append((agents, rules))
                agents.append(val.lower())
                prev_agent = True
            elif key in ("allow", "disallow") and agents is not None:
                prev_agent = False
                rules.append((key == "allow", val))
            else:
                prev_agent = False
        specific = [g for g in groups if any(a != "*" and a in ua for a in g[0])]
        chosen = specific or [g for g in groups if "*" in g[0]]
        self._rules: List[Tuple[bool, int, "re.Pattern[str]"]] = []
        for _, rs in chosen:
            for allow, pat in rs:
                if not pat:  # « Disallow: » vide = tout autorisé
                    continue
                rx = re.escape(pat).replace(r"\*", ".*")
                if rx.endswith(r"\$"):
                    rx = rx[:-2] + "$"
                self._rules.append((allow, len(pat), re.compile(rx)))

    def can_fetch(self, url: str) -> bool:
        p = urlparse(url)
        target = p.path + (f"?{p.query}" if p.query else "")
        best: Optional[Tuple[int, bool]] = None
        for allow, length, rx in self._rules:
            if rx.match(target):
                if best is None or length > best[0] or (length == best[0] and allow):
                    best = (length, allow)
        return True if best is None else best[1]


class AyvensScraper(CarScraperBase):
    BASE = "https://usedcars.ayvens.com"
    ROOT = "/fr-be/voitures?prefn1=fuelType&prefn2=vehicleType&prefv1=Electric&prefv2=Hatchback&start=0&sz=24"
    PAGE_SIZE = 24

    def __init__(
        self,
        helper: CarHelper,
        radius_km: Optional[float] = 50.0,
        center_lat: float = LIEGE_LAT,
        center_lng: float = LIEGE_LNG,
        segments: Optional[Set[str]] = None,
        statuses: Optional[Set[str]] = None,
        keep_unknown_location: bool = False,
        store_coordinates: Optional[Dict[str, Tuple[float, float]]] = None,
        request_delay: float = 1.0,
        timeout: int = 20,
        save_html_dir: Optional[str] = None,
    ):
        """
        radius_km             : rayon autour du centre (4000 Liège par défaut) ; None = pas de filtre.
        segments              : ex. {"A", "B"} ou {"A", "B", "B-SUV"} ; None = pas de filtre (opt-in).
        statuses              : {"available"} par défaut ; autres : "reserved", "soon", "sold".
        keep_unknown_location : garder les voitures dont le lieu de visite est inconnu.
        store_coordinates     : lieux supplémentaires {"nom sans accent en minuscules": (lat, lng)}.
        request_delay         : pause (s) entre deux requêtes, par politesse envers le site.
        save_html_dir         : si renseigné, sauvegarde le HTML brut de chaque page (diagnostic).
        """
        self.helper = helper
        self.radius_km = radius_km
        self.center = (center_lat, center_lng)
        self.segments = {s.upper() for s in segments} if segments else None
        self.statuses = set(statuses) if statuses else {"available"}
        self.keep_unknown_location = keep_unknown_location
        self.stores = {**DEFAULT_STORES, **{_norm(k): v for k, v in (store_coordinates or {}).items()}}
        self.request_delay = request_delay
        self.timeout = timeout
        self.save_html_dir = save_html_dir

        self.session = requests.Session()
        self.session.headers.update(self.helper.headers)
        self._robots: Optional[RobotsRules] = None
        self._last_request = 0.0
        self._saved = 0
        self._detail_cache: Dict[str, Dict[str, Any]] = {}
        self.stats = {"pages": 0, "cards": 0, "not_electric": 0, "status": 0, "segment": 0,
                      "too_far": 0, "unknown_location": 0, "kept": 0, "truncated_pages": 0}

    # ------------------------------------------------------------------
    # Interface CarScraperBase
    # ------------------------------------------------------------------
    def scrape_base_data(self, max_pages: int = 200) -> List[CarListing]:
        """`max_pages` = nombre maximal de pages catalogue (marque/modèle) parcourues.
        Les fiches de détail des véhicules électriques retenus sont ensuite lues (lieu de
        visite pour le rayon, batterie, autonomie...) ; elles ne comptent pas dans `max_pages`."""
        print("[Ayvens] Chargement du catalogue (pages marque/modèle, HTML serveur)...")
        self.stats = {k: 0 for k in self.stats}
        self._load_robots()
        budget = {"pages": max_pages}

        root = self._get(urljoin(self.BASE, self.ROOT), budget)
        if root is None:
            print("[Ayvens] Page catalogue inaccessible ou interdite : arrêt.")
            return []
        brands = self._parse_brands(root)
        print(f"[Ayvens] {len(brands)} marques au catalogue.")

        cards: Dict[str, Dict[str, Any]] = {}
        for slug, info in brands.items():
            if budget["pages"] <= 0:
                break
            self._crawl_brand(slug, info, budget, cards)

        listings: List[CarListing] = []
        for card in cards.values():
            listing = self._card_to_listing(card)
            if listing is not None:
                listings.append(listing)
        self.stats["kept"] = len(listings)
        print(f"[Ayvens] Total : {len(listings)} véhicules retenus ({self.stats}).")
        return listings

    def scrape_details(self, listing) -> Any:
        """Complète l'annonce (batterie, autonomie, puissance, SoH, garantie...) depuis la fiche."""
        car = listing.listing if hasattr(listing, "listing") else listing
        try:
            detail = self._detail_cache.get(car.url)
            if detail is None and car.url:
                soup = self._get(car.url, {"pages": 1})
                if soup is not None:
                    detail = self._parse_detail(soup)
                    self._detail_cache[car.url] = detail
            if detail:
                self._apply_detail(car, detail)
        except Exception as e:  # ne jamais casser le pipeline pour un détail
            print(f"[Ayvens] Erreur d'enrichissement pour {car.url} : {e}")
        return listing

    # ------------------------------------------------------------------
    # Réseau, robots.txt
    # ------------------------------------------------------------------
    def _load_robots(self) -> None:
        url = urljoin(self.BASE, "/robots.txt")
        try:
            r = self.session.get(url, timeout=self.timeout)
            if r.status_code == 200:
                self._robots = RobotsRules(r.text, self.helper.headers.get("User-Agent", "*"))
                print(f"[Ayvens] robots.txt chargé ({len(self._robots._rules)} règles).")
            elif r.status_code in (401, 403):
                self._robots = RobotsRules("User-agent: *\nDisallow: /", "*")
                print("[Ayvens] robots.txt inaccessible (401/403) : tout est considéré interdit.")
            else:
                self._robots = RobotsRules("", "*")  # 404 etc. : pas de restriction publiée
        except requests.RequestException as e:
            self._robots = RobotsRules("", "*")
            print(f"[Ayvens] robots.txt non lu ({e.__class__.__name__}) : "
                  "seules les URL sans paramètres de requête seront utilisées.")

    def _allowed(self, url: str) -> bool:
        if "?" in url:  # les URL à paramètres (filtres, pagination) ne sont jamais utilisées
            return False
        return self._robots.can_fetch(url) if self._robots else True

    def _get(self, url: str, budget: Dict[str, int]) -> Optional[BeautifulSoup]:
        if budget["pages"] <= 0:
            return None
        if not self._allowed(url):
            print(f"[Ayvens] robots.txt interdit {url} : ignoré.")
            return None
        for attempt in range(3):
            wait = self.request_delay - (time.time() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.time()
            try:
                resp = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as e:
                print(f"[Ayvens] Erreur réseau {url} ({e.__class__.__name__}), essai {attempt + 1}/3")
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code == 200:
                budget["pages"] -= 1
                self.stats["pages"] += 1
                self._save_html(url, resp.text)
                return BeautifulSoup(resp.text, "html.parser")
            if resp.status_code in (429, 503):
                time.sleep(min(float(resp.headers.get("Retry-After", 5 * (attempt + 1))), 60))
                continue
            print(f"[Ayvens] HTTP {resp.status_code} pour {url}")
            return None
        return None

    def _save_html(self, url: str, html: str) -> None:
        if not self.save_html_dir:
            return
        os.makedirs(self.save_html_dir, exist_ok=True)
        self._saved += 1
        name = re.sub(r"[^a-zA-Z0-9]+", "_", urlparse(url).path.strip("/"))[:80] or "root"
        with open(os.path.join(self.save_html_dir, f"{self._saved:03d}_{name}.html"), "w", encoding="utf-8") as f:
            f.write(html)

    # ------------------------------------------------------------------
    # Parcours du catalogue
    # ------------------------------------------------------------------
    def _parse_brands(self, soup: BeautifulSoup) -> Dict[str, Dict[str, Any]]:
        """slug -> {name, url, models:[url]} depuis les liens de filtres de la page catalogue."""
        brands: Dict[str, Dict[str, Any]] = {}
        models: Dict[str, List[str]] = {}
        for a in soup.find_all("a", href=True):
            path = unquote(urlparse(urljoin(self.BASE, a["href"])).path).rstrip("/")
            if not path.startswith(self.ROOT + "/"):
                continue
            parts = path[len(self.ROOT) + 1:].split("/")
            url = urljoin(self.BASE, a["href"]).split("?")[0]
            name = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
            name = re.sub(r"\s*label\.refinement\S*|\s*msg\.assistive\S*", "", name).strip()
            if len(parts) == 1 and parts[0]:
                brands.setdefault(parts[0], {"name": name or parts[0], "url": url, "models": []})
            elif len(parts) == 2:
                models.setdefault(parts[0], [])
                if url not in models[parts[0]]:
                    models[parts[0]].append(url)
        for slug, urls in models.items():
            if slug in brands:
                brands[slug]["models"] = urls
        return brands

    def _crawl_brand(self, slug: str, info: Dict[str, Any], budget: Dict[str, int],
                     cards: Dict[str, Dict[str, Any]]) -> None:
        soup = self._get(info["url"], budget)
        if soup is None:
            return
        found = self._parse_cards(soup, info["name"])
        total = self._result_count(soup)
        self._merge_cards(cards, found)
        print(f"[Ayvens] {info['name']}: {len(found)} annonces lues / {total if total is not None else '?'}")

        if total is not None and len(found) < total and info["models"]:
            for url in info["models"]:  # page marque tronquée -> une page par modèle
                if budget["pages"] <= 0:
                    return
                msoup = self._get(url, budget)
                if msoup is None:
                    continue
                mfound = self._parse_cards(msoup, info["name"])
                mtotal = self._result_count(msoup)
                self._merge_cards(cards, mfound)
                if mtotal is not None and len(mfound) < mtotal:
                    self.stats["truncated_pages"] += 1
                    print(f"[Ayvens] ⚠ {url} tronquée ({len(mfound)}/{mtotal}) : la pagination "
                          "(« Afficher plus ») est interdite par robots.txt, annonces restantes ignorées.")
        elif total is not None and len(found) < total:
            self.stats["truncated_pages"] += 1
            print(f"[Ayvens] ⚠ {info['name']} tronquée ({len(found)}/{total}) sans pages modèle.")

    @staticmethod
    def _merge_cards(store: Dict[str, Dict[str, Any]], cards: Iterable[Dict[str, Any]]) -> None:
        for c in cards:
            store.setdefault(c["id"], c)

    @staticmethod
    def _result_count(soup: BeautifulSoup) -> Optional[int]:
        m = COUNT_RE.search(soup.get_text(" ", strip=True))
        return int(m.group(1)) if m else None

    # ------------------------------------------------------------------
    # Parsing d'une page de liste
    # ------------------------------------------------------------------
    def _ids_under(self, node) -> Set[str]:
        return {m.group(1) for x in node.find_all("a", href=True) if (m := DETAIL_RE.search(x["href"]))}

    def _parse_cards(self, soup: BeautifulSoup, brand_name: str) -> List[Dict[str, Any]]:
        cards: Dict[str, Dict[str, Any]] = {}
        for a in soup.find_all("a", href=True):
            m = DETAIL_RE.search(a["href"])
            title = a.get_text(" ", strip=True)
            if not m or not title:
                continue
            vid = m.group(1)
            if vid in cards:
                continue
            node = a
            for _ in range(8):  # remonte jusqu'à la tuile complète (une seule annonce, texte court)
                parent = node.parent
                if parent is None or len(self._ids_under(parent)) > 1:
                    break
                if len(parent.get_text(" ", strip=True)) > 700:
                    break
                node = parent
            card = self._parse_card_text(node.get_text(" ", strip=True), title)
            if card is None:
                continue
            card.update(id=vid, url=urljoin(self.BASE, a["href"]).split("?")[0], brand=brand_name)
            cards[vid] = card
            self.stats["cards"] += 1
        return list(cards.values())

    def _parse_card_text(self, text: str, title: str) -> Optional[Dict[str, Any]]:
        spec = SPEC_RE.search(text)
        if not spec:
            return None
        pre = text[:spec.start()]
        tail = pre[-60:]
        electric = bool(re.search(r"[ée]lectrique", tail, re.IGNORECASE)) and "hybride" not in tail.lower()
        i = pre.find(title)
        version = pre[i + len(title):] if i >= 0 else pre
        version = re.sub(r"\s*(?:Plug-in hybride|Hybride)?\s*(?:[ÉE]lectrique|Essence|Diesel)[\w\s/\-]*$",
                         "", version.strip(), flags=re.IGNORECASE)
        version = re.sub(r"\s*(?:Other|Autre|Euro\s*\S+)$", "", version.strip(), flags=re.IGNORECASE).strip()
        pm = PRICE_RE.search(text)
        # Le badge (Réservé / Bientôt disponible / Vendu) précède le titre dans la tuile : on ne lit que
        # les quelques caractères qui le précèdent, pour ne pas être trompé par les listes de filtres.
        idx = text.find(title)
        head = text[max(0, idx - 45):idx].lower() if idx >= 0 else ""
        status = ("sold" if re.search(r"\bvendu\b", head) else
                  "reserved" if re.search(r"\br[ée]serv[é]\b", head) else
                  "soon" if re.search(r"bient[ôo]t disponible", head) else "available")
        return {
            "title": title, "version": version, "electric": electric, "status": status,
            "date": spec.group(1), "km": _to_int(spec.group(2)),
            "price_label": pm.group(1).lower() if pm else None,
            "price_text": f"{pm.group(2)} €" if pm else None,
        }

    # ------------------------------------------------------------------
    # Fiche de détail
    # ------------------------------------------------------------------
    def _parse_detail(self, soup: BeautifulSoup) -> Dict[str, Any]:
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
        d: Dict[str, Any] = {"text": text}

        def grab(pattern: str, flags: int = re.IGNORECASE):
            m = re.search(pattern, text, flags)
            return m.group(1).strip() if m else None

        d["year"] = _to_int(grab(r"Date de premi[èe]re immatriculation\s+\d{2}/\d{2}/(\d{4})"))
        d["fuel"] = grab(r"Carburant\s+(.+?)\s+Kilom[ée]trage")
        d["km"] = _to_int(grab(r"Kilom[ée]trage\s+([\d.\s\xa0\u202f]+?)\s*km"))
        d["body"] = grab(r"Cat[ée]gorie\s+(\S+)")
        d["store"] = grab(r"[ÀA] voir [àa]\s+(.+?)(?=\s+(?:Espace du coffre|Plaque|Puissance|Couleur|Capacit|"
                          r"Autonomie|Norme|Nombre|Roue)|$)")
        d["kw"] = _to_int(grab(r"Puissance\s+(\d+)\s*kW\b"))
        d["cv"] = _to_int(grab(r"Puissance CV\s+(\d+)\s*CV"))
        batt = grab(r"Capacit[ée] de la batterie\s+([\d.,]+)\s*kWh")
        d["battery_kwh"] = float(batt.replace(",", ".")) if batt else None
        d["range_km"] = _to_int(grab(r"Autonomie th[ée]orique\s+(\d+)\s*km"))
        d["warranty_months"] = _to_int(grab(r"Garantie de (\d+) mois"))
        d["service_history"] = bool(re.search(r"Historique d.entretien", text, re.IGNORECASE)
                                    and re.search(r"entretenu|checks?|Afficher l.historique", text, re.IGNORECASE))
        return d

    def _apply_detail(self, car: CarListing, d: Dict[str, Any]) -> None:
        if car.battery_kwh is None and d.get("battery_kwh") and 10 <= d["battery_kwh"] <= 160:
            car.battery_kwh = d["battery_kwh"]
        if car.range_wltp_km is None and d.get("range_km"):
            car.range_wltp_km = d["range_km"]
        if car.power_hp is None:
            car.power_hp = d.get("cv") or (round(d["kw"] * 1.35962) if d.get("kw") else None)
        if car.warranty_months is None and d.get("warranty_months"):
            car.warranty_months = d["warranty_months"]
        if car.has_service_history is None and d.get("service_history"):
            car.has_service_history = True
        if car.mileage_km is None and d.get("km"):
            car.mileage_km = d["km"]
        if car.year is None and d.get("year"):
            car.year = d["year"]
        if not car.dealer_city and d.get("store"):
            car.dealer_city = d["store"]

    # ------------------------------------------------------------------
    # Fiche -> CarListing (avec tous les filtres)
    # ------------------------------------------------------------------
    def _segment_of(self, brand: str, model: str) -> Optional[str]:
        key = _norm(f"{brand}{model}")
        for seg, pattern in _SEGMENT_PATTERNS:
            if re.search(pattern, key):
                return seg
        return None

    @staticmethod
    def _split_model(brand: str, title: str) -> str:
        t = title.strip()
        return t[len(brand):].strip() if t.lower().startswith(brand.lower()) else t

    def _card_to_listing(self, card: Dict[str, Any]) -> Optional[CarListing]:
        if not card["electric"]:
            self.stats["not_electric"] += 1
            return None
        if card["status"] not in self.statuses:
            self.stats["status"] += 1
            return None
        brand = card["brand"]
        model = self._split_model(brand, card["title"]) or None
        if self.segments and self._segment_of(brand, model or "") not in self.segments:
            self.stats["segment"] += 1
            return None

        detail = self._detail_cache.get(card["url"])
        if detail is None:
            soup = self._get(card["url"], {"pages": 1})
            detail = self._parse_detail(soup) if soup is not None else {}
            self._detail_cache[card["url"]] = detail

        if detail.get("fuel") and "lectrique" not in detail["fuel"].lower():
            self.stats["not_electric"] += 1
            return None
        if not self._within_radius(detail.get("store")):
            return None

        year = None
        m = re.search(r"/(\d{4})$", card["date"])
        if m:
            year = int(m.group(1))
        listing = CarListing(
            source="ayvens",
            title=" ".join(x for x in (card["title"], card["version"]) if x),
            brand=brand, model=model,
            price=self.helper._parse_price(card["price_text"]) if card["price_text"] else None,
            mileage_km=card["km"], year=year,
            dealer_city=detail.get("store"),
            url=card["url"],
        )
        self._apply_detail(listing, detail)
        self._enrich_from_text(listing, f"{card['version']} {detail.get('text', '')}")
        return listing

    def _within_radius(self, store: Optional[str]) -> bool:
        if self.radius_km is None:
            return True
        coords = self.stores.get(_norm(store)) if store else None
        if coords is None:
            self.stats["unknown_location"] += 1
            return self.keep_unknown_location
        if _haversine_km(*self.center, *coords) <= self.radius_km:
            return True
        self.stats["too_far"] += 1
        return False

    def _enrich_from_text(self, car: CarListing, text: str) -> None:
        if not text:
            return
        if car.battery_kwh is None:
            for m in re.finditer(r"(\d{2,3}(?:[.,]\d{1,2})?)\s*kwh", text, re.IGNORECASE):
                v = float(m.group(1).replace(",", "."))
                if 10 <= v <= 160:
                    car.battery_kwh = v
                    break
        if car.soh_percent is None:
            m = re.search(r"(?:SOH|state of health|[ée]tat de (?:la )?batterie)\s*:?\s*(\d{2,3})(?:[.,]\d+)?\s*%",
                          text, re.IGNORECASE)
            if m and 30 <= int(m.group(1)) <= 100:
                car.soh_percent = int(m.group(1))
        if car.previous_owners is None:
            m = re.search(r"(\d)\s*(?:propri[ée]taires?|owners?|eigenaars?)", text, re.IGNORECASE)
            if m:
                car.previous_owners = int(m.group(1))


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Test du scraper Ayvens Used Cars (scraping seul)")
    ap.add_argument("--max-pages", type=int, default=40)
    ap.add_argument("--segments", help="ex. A,B ou A,B,B-SUV")
    ap.add_argument("--radius", type=float, default=50.0, help="rayon km autour de 4000 Liège (0 = désactivé)")
    ap.add_argument("--save-html", help="dossier où sauvegarder le HTML brut (diagnostic)")
    ap.add_argument("--dump", help="écrit les annonces JSON dans ce fichier")
    args = ap.parse_args()

    scraper = AyvensScraper(
        helper=CarHelper(enrich_details=True),
        radius_km=args.radius or None,
        segments=set(args.segments.split(",")) if args.segments else None,
        save_html_dir=args.save_html,
    )
    cars = scraper.scrape_base_data(max_pages=args.max_pages)
    for c in cars[:10]:
        scraper.scrape_details(c)
        print(c.to_dict())
    if args.dump:
        with open(args.dump, "w", encoding="utf-8") as f:
            json.dump([c.to_dict() for c in cars], f, ensure_ascii=False, indent=2)
