import json
from typing import Any, Dict, List, Optional
from langchain_core.prompts import ChatPromptTemplate
from langchain_ollama import ChatOllama
import pandas as pd
from pydantic import BaseModel, Field

model="qwen2.5-coder:14b"  # Modèle LLM à utiliser pour la normalisation

# ==========================================
# 1. SCHÉMA DE NORMALISATION STRICT (PYDANTIC)
# ==========================================
class NormalizedEV(BaseModel):
  """Schéma normalisé pour permettre un regroupement fiable."""

  brand: str = Field(
      description=(
          "Marque standardisée avec majuscule initiale (ex: BMW, Opel, Peugeot,"
          " Volkswagen, Skoda)."
      )
  )
  model: str = Field(
      description=(
          "Modèle normalisé sans suffixe de finition ni code interne (ex: i3,"
          " Corsa-e, e-208, ID.3, Enyaq)."
      )
  )
  battery_kwh: Optional[float] = Field(
      default=None,
      description=(
          "Capacité de la batterie en kWh (ex: 50.0, 42.2, 77.0). Déduis-la de"
          " la version si connue (ex: Enyaq 80 -> 77.0, 120Ah -> 42.2)."
      ),
  )
  trim: str = Field(
      description=(
          "Déclinaison ou finition principale normalisée (ex: Edition, M Sport,"
          " Pure Performance, GT, Standard). Mets 'Standard' si non précisé."
      )
  )


# ==========================================
# 2. CONSTRUCTION DE LA CHAÎNE LANGCHAIN
# ==========================================
def build_normalization_chain(
    base_url: str = "http://192.168.0.23:11434",
    model_name: str = model,
):
  # temperature=0 pour un comportement déterministe et reproductible
  llm = ChatOllama(
      base_url=base_url,
      model=model_name,
      temperature=0,
  )

  # Force le LLM à retourner un objet respectant exactement le schéma NormalizedEV
  structured_llm = llm.with_structured_output(NormalizedEV)

  prompt = ChatPromptTemplate.from_messages([
      (
          "system",
          "Tu es un expert du marché automobile électrique européen. "
          "Ta mission est d'extraire et de normaliser la marque, le modèle, la capacité batterie (kWh) "
          "et la déclinaison (finition) à partir d'une annonce brute.\n"
          "Règles de normalisation :\n"
          "- Unifie les variantes d'écriture (ex: 'E Corsa', 'e-Corsa', 'Corsa Electric' -> brand='Opel', model='Corsa-e').\n"
          "- Unifie 'i3 I01 Lci 120ah' -> brand='BMW', model='i3', battery_kwh=42.2.\n"
          "- Nettoie les codes techniques (ex: 'U10', 'I01', 'AT', 'BVA') du nom du modèle et de la finition.\n"
          "- Garde uniquement le nom commercial de la finition dans 'trim' (ex: 'M Sport', 'Edition', 'Atelier', 'Business').",
      ),
      (
          "human",
          "Analyse cette annonce :\n"
          "Titre brut : {title}\n"
          "Marque détectée : {brand}\n"
          "Modèle détecté : {model}\n"
          "Batterie détectée : {battery_kwh} kWh\n"
          "Puissance : {power_hp} ch\n"
          "URL : {url}",
      ),
  ])

  return prompt | structured_llm


# ==========================================
# 3. TRAITEMENT PAR LOT & REGROUPEMENT
# ==========================================
def process_and_group_vehicles(
    raw_database: List[Dict[str, Any]],
    cache: Optional[Any] = None,
    ollama_url: str = "http://192.168.0.23:11434",
    model_name: str = "qwen2.5-coder:14b",
) -> pd.DataFrame:
  if not raw_database:
    return pd.DataFrame()

  already_normalized = []
  to_normalize = []

  for car in raw_database:
    # Vérifie si l'enregistrement (ou sa version en cache) possède déjà les champs normalisés
    cached_car = cache.get(car["url"]) if cache and car.get("url") else car
    if cached_car and cached_car.get("norm_brand") and cached_car.get("norm_trim"):
      already_normalized.append(cached_car)
    else:
      to_normalize.append(car)

  print(
      f"[LangChain] {len(already_normalized)} véhicules déjà normalisés en cache"
      f" | {len(to_normalize)} à traiter via {model_name}."
  )

  newly_enriched = []
  if to_normalize:
    chain = build_normalization_chain(base_url=ollama_url, model_name=model_name)
    inputs = [
        {
            "title": car.get("title") or "",
            "brand": car.get("brand") or "Inconnu",
            "model": car.get("model") or "Inconnu",
            "battery_kwh": car.get("battery_kwh") or "Inconnue",
            "power_hp": car.get("power_hp") or "Inconnue",
            "url": car.get("url") or "",
        }
        for car in to_normalize
    ]

    normalized_results: List[NormalizedEV] = chain.batch(
        inputs, config={"max_concurrency": 2}
    )

    for raw, norm in zip(to_normalize, normalized_results):
      record = raw.copy()
      record["norm_brand"] = norm.brand
      record["norm_model"] = norm.model
      record["norm_battery_kwh"] = raw.get("battery_kwh") or norm.battery_kwh
      record["norm_trim"] = norm.trim
      newly_enriched.append(record)

      # Sauvegarde dans le cache avec les champs normalisés inclus
      if cache:
        cache.upsert(record)

  all_records = already_normalized + newly_enriched
  df = pd.DataFrame(all_records)

  df["norm_battery_kwh_str"] = df["norm_battery_kwh"].apply(
      lambda x: f"{x} kWh" if pd.notnull(x) else "Non spécifiée"
  )

  grouped = (
      df.groupby(
          ["norm_brand", "norm_model", "norm_battery_kwh_str", "norm_trim"],
          as_index=False,
      )
      .agg(
          nb_offres=("url", "count"),
          prix_min=("price", "min"),
          prix_moyen=("price", "mean"),
          prix_max=("price", "max"),
          km_moyen=("mileage_km", "mean"),
          annee_moyenne=("year", "mean"),
          soh_max=("soh_percent", "max"),
          garantie_max=("warranty_months", "max"),
          sources=("source", lambda s: ", ".join(sorted(set(s)))),
          urls=("url", list),
      )
      .sort_values(by=["norm_brand", "norm_model", "prix_min"])
  )

  grouped["prix_moyen"] = grouped["prix_moyen"].round(0)
  grouped["km_moyen"] = grouped["km_moyen"].round(0)
  grouped["annee_moyenne"] = grouped["annee_moyenne"].round(1)

  return grouped

# ==========================================
# EXEMPLE D'EXÉCUTION
# ==========================================
if __name__ == "__main__":
  # Exemple avec tes données extraites précédemment
  sample_data = [
      {
          "source": "cardoen",
          "title": "Opel E Corsa",
          "brand": "Opel",
          "model": "E Corsa",
          "price": 16999.0,
          "mileage_km": 39462,
          "year": 2021,
          "battery_kwh": 50.0,
          "power_hp": 136,
          "soh_percent": None,
          "url": "https://www.cardoen.be/fr/auto/opel/e-corsa/50-kwh-edition-136-at/337614",
      },
      {
          "source": "spoticar",
          "title": "Opel Corsa-e 50kWh Edition",
          "brand": "Opel",
          "model": "Corsa-e",
          "price": 16490.0,
          "mileage_km": 41200,
          "year": 2021,
          "battery_kwh": 50.0,
          "power_hp": 136,
          "soh_percent": 94,
          "url": "https://www.spoticar.be/fr/voitures-occasion/opel-corsa-e-edition",
      },
      {
          "source": "cardoen",
          "title": "Bmw I3 I01 Lci",
          "brand": "Bmw",
          "model": "I3 I01 Lci",
          "price": 19499.0,
          "mileage_km": 76577,
          "year": 2022,
          "battery_kwh": 42.2,
          "power_hp": 170,
          "soh_percent": 97,
          "url": "https://www.cardoen.be/fr/auto/bmw/i3-i01-lci/120ah-42-2-kwh-atelier-170/339201",
      },
  ]

  summary_df = process_and_group_vehicles(
      sample_data,
      ollama_url="http://192.168.0.23:11434",
      model_name=model,
  )

  print("\n--- Tableau récapitulatif regroupé ---")
  print(summary_df.drop(columns=["urls"]).to_string(index=False))