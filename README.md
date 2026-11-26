# carscrapper

Outil d'aide au choix d'une **voiture électrique d'occasion** en Belgique (segments A et B).

Il collecte les annonces de plusieurs sites (AutoScout24, MyWay, Spoticar, Renew, Ayvens, Cardoen),
identifie la variante technique exacte de chaque voiture, puis la compare à une **voiture de référence
neuve** du même segment, que vous choisissez vous-même (voir ci-dessous).

L'**indice d'opportunité** = prix estimé / prix demandé. Sous 1, mieux vaut acheter la référence neuve.
Le prix estimé tient compte :

- du temps perdu sur un trajet de 1 000 km par rapport à une thermique (simulation « rythme humain ») ;
- de l'âge et du kilométrage ;
- de la marque : fiabilité, réseau en Belgique, géopolitique et maturité électrique
  (étude [USCALE EV Satisfaction](https://uscale.digital/en/2026/10/20/ev-satisfaction-study-2026/)) ;
- de la plateforme : dédiée à l'électrique, partagée avec le thermique ou convertie ;
- de l'état de la batterie (SoH) quand l'annonce le donne.

Résultat : `output/best_ev_opportunities.xlsx`, trié par indice, avec le statut road trip
(OK < 30 min perdues, Long < 60, Très long ≤ 90, Rédhibitoire au-delà).

## Installation (Windows)

```bat
git clone <url-du-dépôt> carscrapper
cd carscrapper
py -3.14 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium
```

Puis choisir les voitures de référence (section suivante) et lancer `batch\full.bat`.

## Choisir les voitures de référence (obligatoire)

Chaque annonce est comparée à la voiture **neuve** que vous achèteriez à la place, dans le même segment.
L'estimation est refusée tant qu'une référence n'a pas été choisie pour chaque segment (A et B) :

```bat
batch\set_reference_variant.bat A dacia_spring_27kwh 17890
batch\set_reference_variant.bat B hyundai_ioniq_3_42kwh 31495 "finition Select, remise 2000 € déduite"
```

- 1er argument : le segment (`A` ou `B`).
- 2e argument : l'identifiant de la variante (`variante_id` dans `models.json`).
- 3e argument : le prix neuf réellement payé, remises déduites.
- 4e argument (facultatif) : une remarque.

`batch\set_reference_variant.bat` sans argument affiche les références actuelles et toutes les variantes
disponibles par segment. Le choix est enregistré dans `data/references.json` (propre à chaque PC, non versionné).
Le reste (simulation 1 000 km, plateforme, score de marque) est relu dans le catalogue à chaque estimation.
Relancez ensuite `batch\estimate.bat`.

## Utilisation

Les cas courants sont dans `batch\` (détails dans [`batch/LISEZMOI.txt`](batch/LISEZMOI.txt)) :

| Batch | Usage |
|---|---|
| `full.bat` | scraping complet → estimation |
| `update.bat` | mise à jour rapide (premières pages) |
| `replay.bat` | rejeu hors ligne après modification de `models.json` ou des règles |
| `estimate.bat` | estimation seule |
| `set_reference_variant.bat` | choix des voitures de référence (voir plus haut) |
| `reset-all.bat` | archive les annonces et repart de zéro |

Les annonces collectées (`data/`) et les résultats (`output/`) ne sont **pas** versionnés : un nouveau clone
commence par `batch\full.bat`.

## Organisation

- `src/paths.py` : emplacements des fichiers (`data/` pour l'état du pipeline, `output/` pour les résultats).
- `src/pipeline.py` : étapes rejouables `scrape → match → details → resolve`.
- `src/*scraper.py` : un scraper par site.
- `src/variant_resolver.py`, `src/spec_hints.py` : identification de la variante.
- `src/market_estimator.py` : valorisation et export Excel.
- `src/references.py` : voitures de référence par segment (`data/references.json`).
- `models.json` : référentiel des modèles et variantes (données techniques, plateforme, simulations 1 000 km).
- `classification_marques_belgique-v3.json` : scores de marque.
- `CLAUDE.md` : conventions et pièges, pour les assistants de code comme pour les humains.

## Avertissements

- Outil personnel, partagé tel quel. Les estimations reposent sur des hypothèses et des données en partie
  estimées (signalées « à vérifier » dans les fichiers). Ce n'est pas un conseil d'achat.
- Respectez les conditions d'utilisation des sites consultés. Les scrapers limitent leur rythme de requêtes,
  et le scraper Ayvens respecte le robots.txt.

## Licence

[MIT](LICENSE) : libre à chacun de s'en servir.
