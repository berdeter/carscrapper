"""Heuristiques de spécifications (batterie, WLTP) déduites d'une annonce.

Ces règles étaient dispersées dans les scrapers (table MyWay « (marque, modèle, kW) -> batterie/WLTP »,
« iv-80 -> 77 kWh » d'AutoScout...). Elles vivent ici, appliquées APRÈS le scraping par le
VariantResolver, sans jamais modifier l'annonce brute : on peut donc les corriger puis rejouer la
résolution sans re-scraper.

Pour ajouter/corriger une règle sans toucher au code : `SpecHints(rules_path="spec_hints.json")`
(les règles du fichier passent avant les règles par défaut). `SpecHints.dump_default("spec_hints.json")`
écrit le jeu par défaut comme point de départ.

Format d'une règle (champs optionnels sauf le résultat) :
  {"id": "...", "source": "autoscout24", "brand": "volkswagen", "model": "id3", "power_kw": 107,
   "url_regex": "iv-80", "text_regex": "(\\d+) ?kwh", "battery_kwh": 45.0, "wltp_km": 350}
`brand`/`model` sont comparés sans accents ni ponctuation ; `power_kw` à ±1 kW près (kW = ch / 1,35962).
Avec `text_regex`, le premier groupe capturé est la valeur ("capture": "battery_kwh" ou "wltp_km").
"""
import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional

VERSION = "2"


def norm(text: Any) -> str:
    s = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", s.lower())


# Table issue de MyWayScraper.EV_SPEC_FALLBACK : (marque, modèle, kW) -> (batterie kWh, WLTP km)
_MYWAY_TABLE = [
    ("volkswagen", "ID.Polo", 99, 37.0, 330), ("volkswagen", "ID.Polo", 155, 52.0, 450),
    ("cupra", "Raval", 99, 37.0, 330), ("cupra", "Raval", 155, 51.5, 440),
    ("volkswagen", "ID.3", 107, 45.0, 350), ("volkswagen", "ID.3", 110, 58.0, 425),
    ("volkswagen", "ID.3", 150, 58.0, 425), ("volkswagen", "ID.3", 170, 77.0, 550),
    ("volkswagen", "ID.3", 240, 79.0, 590), ("volkswagen", "ID.3 Neo", 140, 58.0, 434),
    ("volkswagen", "ID.3 Neo", 170, 79.0, 629), ("cupra", "Born", 150, 58.0, 425),
    ("cupra", "Born", 170, 77.0, 550), ("citroen", "e-C3", 83, 44.2, 320),
    ("renault", "e-R5 E-Tech", 88, 40.0, 312), ("renault", "e-R5 E-Tech", 110, 52.0, 410),
]

DEFAULT_RULES: List[Dict[str, Any]] = (
    [
        {"id": f"table-{norm(b)}-{norm(m)}-{kw}", "brand": norm(b), "model": norm(m), "power_kw": kw,
         "battery_kwh": bat, "wltp_km": wltp}
        for b, m, kw, bat, wltp in _MYWAY_TABLE
    ]
    + [
        # Règles issues d'AutoScoutScraper._extract_info_from_autoscout_url
        {"id": "autoscout-url-iv80", "source": "autoscout24", "url_regex": r"iv-80|e-80", "battery_kwh": 77.0},
        {"id": "autoscout-url-iv60", "source": "autoscout24", "url_regex": r"iv-60", "battery_kwh": 58.0},
        # Règles génériques sur le texte de l'annonce
        # « 52 kWh », « 51,5kwh » et, dans les URL, « 51-5-kwh » (le tiret remplace la virgule)
        {"id": "text-kwh", "text_regex": r"(?<![\d.,])(\d{2,3}(?:[.,]\d{1,2}|-\d(?!\d))?)[\s-]*(?:kwh|khw|kw/h)\b",
         "capture": "battery_kwh", "min": 15.0, "max": 130.0},
        {"id": "text-wltp", "text_regex": r"\b(\d{3})\s*km\s*wltp\b", "capture": "wltp_km", "min": 100, "max": 900},
    ]
)


class SpecHints:
    def __init__(self, rules: Optional[List[Dict[str, Any]]] = None, rules_path: Optional[str] = None):
        user_rules: List[Dict[str, Any]] = []
        if rules_path and Path(rules_path).exists():
            user_rules = json.loads(Path(rules_path).read_text(encoding="utf-8"))
        self.rules = user_rules + (rules if rules is not None else DEFAULT_RULES)
        blob = json.dumps(self.rules, sort_keys=True, ensure_ascii=False)
        self.signature = f"{VERSION}.{hashlib.sha1(blob.encode()).hexdigest()[:8]}"

    @staticmethod
    def dump_default(path: str) -> None:
        Path(path).write_text(json.dumps(DEFAULT_RULES, ensure_ascii=False, indent=1), encoding="utf-8")

    def infer(self, listing: Any) -> Dict[str, Any]:
        """Renvoie {"battery_kwh", "range_wltp_km", "sources": [ids de règles]} (clés présentes si déduites)."""
        brand, model = norm(getattr(listing, "brand", None)), norm(getattr(listing, "model", None))
        source = str(getattr(listing, "source", "") or "").lower()
        url = str(getattr(listing, "url", "") or "")
        text = " ".join(str(getattr(listing, a, "") or "") for a in ("title", "model")) + " " + url
        hp = getattr(listing, "power_hp", None)
        kw = hp / 1.35962 if hp else None

        out: Dict[str, Any] = {"sources": []}

        def put(field: str, value: Any, rule_id: str) -> None:
            if field not in out and value is not None:
                out[field] = value
                out["sources"].append(rule_id)

        for r in self.rules:
            if r.get("source") and r["source"].lower() != source:
                continue
            if r.get("brand") and r["brand"] != brand:
                continue
            if r.get("model") and r["model"] != model:
                continue
            if r.get("power_kw") is not None and (kw is None or abs(kw - r["power_kw"]) > 1.0):
                continue
            if r.get("url_regex") and not re.search(r["url_regex"], url, re.IGNORECASE):
                continue
            if r.get("text_regex"):
                m = re.search(r["text_regex"], text, re.IGNORECASE)
                if not m:
                    continue
                try:
                    val = float(m.group(1).replace(",", ".").replace("-", "."))
                except (ValueError, IndexError):
                    continue
                if not (r.get("min", float("-inf")) <= val <= r.get("max", float("inf"))):
                    continue
                field = {"battery_kwh": "battery_kwh", "wltp_km": "range_wltp_km"}[r.get("capture", "battery_kwh")]
                put(field, int(val) if field == "range_wltp_km" else val, r["id"])
                continue
            put("battery_kwh", r.get("battery_kwh"), r["id"])
            put("range_wltp_km", r.get("wltp_km"), r["id"])
        return out
