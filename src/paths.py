"""Emplacements des fichiers du projet, indépendants du dossier courant.

Racine du projet : sources et référentiels versionnés (models.json, classification des marques).
  data/   : état du pipeline, régénérable (annonces brutes, appariements, journal de résolution, cache final).
  output/ : résultats à consulter (export Excel / CSV de l'estimation).
Les deux dossiers sont ignorés par Git et créés à la demande.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / "src"
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"

CATALOG = ROOT / "models.json"
BRANDS = ROOT / "classification_marques_belgique-v3.json"
RESOLVED_DB = DATA_DIR / "resolved_cars.json"
REFERENCES = DATA_DIR / "references.json"   # voitures de référence choisies (batch\set_reference_variant.bat)
EXPORT_BASENAME = "best_ev_opportunities"
