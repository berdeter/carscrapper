# carscrapper

Outil personnel d'aide au choix d'une voiture électrique d'occasion (Belgique, segments A et B).
Langue du projet : français (code commenté, messages, documentation).

## Ce que calcule l'outil

`MarketEstimator` ne donne PAS une valeur de marché : il compare chaque annonce à une voiture de
**référence neuve** par segment, choisie par l'utilisateur (`data/references.json`, via `batch/set_reference_variant.bat` ;
seuls `variante_id` et prix neuf y sont stockés, le reste est relu dans le catalogue par `references.py`).
Aucune référence en dur dans le code : l'estimateur refuse de tourner (`MissingReferenceError`) tant qu'un segment n'en a pas.
`Indice Opportunité` = prix estimé / prix demandé ; **< 1 : mieux vaut acheter la référence neuve.**
Prix estimé = prix neuf de la référence × coef. électrique (simulation 1000 km) × coef. âge/km × coef. marque × coef. SoH.

- Coef. Plateforme : champ `plateforme` de chaque variante (dediee 1,00 / multi_energie 0,95 / convertie 0,90, ×0,95 si batterie « air passif »),
  relatif à la plateforme de la variante de référence du segment. Chaque `plateforme` indique sa `source` et sa `confiance`
  (vérifiées par Bernard le 26/11/2026) : documenter la source de toute nouvelle valeur.
- Coef. SoH : SoH mesuré / SoH attendu pour l'âge. L'usure normale est déjà dans la décote d'âge : ne pas la compter deux fois.
- Variante incertaine : valorisée au **pire cas** parmi `variantes_possibles`.
- Road trip (temps perdu vs thermique) : OK < 30 min, Long < 60, Très long ≤ 90, Rédhibitoire > 90 ; exclusion en segment B seulement.
- Segment A = voiture urbaine pour un usage différent (sorties occasionnelles, recharge sur bornes pendant les courses) : le road trip y est indicatif.

## Pipeline (`src/pipeline.py`)

`scrape → match → details → resolve`, puis `src/market_estimator.py`. Chaque étape persiste son résultat
et se rejoue seule, sans refaire les précédentes :

| Fichier | Écrit par | Contenu |
|---|---|---|
| `data/raw_listings.json` | scrape, details | faits bruts, historique de prix, `active`, `details_at` / `details_failures` |
| `data/catalog_matches.json` | match | (marque_id, modele_id) ou raison du rejet |
| `data/resolution_log.json` | resolve | variante choisie, méthode, `pool_ids`, empreinte d'entrée |
| `data/resolved_cars.json` | resolve | cache final (TinyDB) lu par l'estimateur |
| `data/references.json` | `set_reference_variant.bat` | voitures de référence choisies (conservé par `reset-all`) |
| `output/best_ev_opportunities.xlsx` / `.csv` | estimateur | résultat à consulter |
| `models.json` | à la main + simulateur | référentiel modèles/variantes et simulations 1000 km |
| `classification_marques_belgique-v3.json` | à la main | scores de marque : filtre ≥ 55 (`BrandFilter`) et coef. marque ; axe `maturite_ev` issu des études USCALE |

Tous les chemins viennent de `src/paths.py` (indépendants du dossier courant) : sources et référentiels à la racine,
état du pipeline dans `data/`, résultats dans `output/` (tous deux non versionnés). Les cas d'usage
courants sont dans `batch/` : voir `batch/LISEZMOI.txt` avant d'en ajouter un.

## Responsabilités (une seule place par règle)

- **Scrapers** (`*scraper.py`, héritent de `CarScraperBase`) : extraient uniquement ce que le site affirme. Aucune déduction.
  `scrape_details` **lève une exception** en cas d'échec (`DetailsGoneError` si 404/410, sinon `DetailsFetchError`) :
  renvoyer l'annonce inchangée la ferait passer pour lue.
- **`SpecHints`** : heuristiques batterie / WLTP déduites (tables, regex sur titre et URL).
- **`VariantResolver`** : seul endroit qui choisit une variante, par règles déterministes
  (année ±1, batterie nominale puis utile, mots-clés, puissance, année stricte, WLTP). Le LLM est désactivé par défaut (`--llm`).
- Toute modification d'une règle de résolution : incrémenter `RULES_VERSION` (ou `VERSION` dans `spec_hints.py`),
  sinon `resolve` réutilise les anciennes décisions via l'empreinte.
- Une variante mal résolue se corrige d'abord **dans les données** de `models.json` :
  `mots_cles` (variante) et `donnees_techniques.puissance_moteur_kw` (nombre ou liste), puis `batch/replay.bat`.

## Pièges connus

- Encodage : tout fichier JSON s'écrit en **UTF-8** (TinyDB reçoit `encoding="utf-8"`, sinon Windows écrit en cp1252).
  `models.json` est en CRLF, indentation 2, `ensure_ascii=False` : préserver ce format en le réécrivant.
- `StageStore` refuse un fichier illisible : ne jamais contourner en repartant d'un magasin vide (perte de l'historique).
- `--mark-missing` n'est valable que si `--max-pages` couvre tout le catalogue de la source.
- Spoticar : le champ `maximumPowerHP` est en **kW**, converti en ch par le scraper.
- Les `simulation_humaine_1000km` de `models.json` sont relues à chaque `resolve` (annonces) et à chaque estimation (références).
- Données techniques ajoutées par Claude (puissances, mots-clés, futures puissances AC) : signalées « à vérifier », à confirmer par Bernard.

## Vérifier un changement

Pas de tests automatisés. Avant de modifier les fichiers de données réels, travailler sur une copie des JSON
et comparer `data/resolution_log.json` avant/après (méthode, variante) : toute résolution qui change doit être justifiée.

## Git

Dépôt **public** (licence MIT), travaillé depuis deux PC Windows. Versionnés : code, `batch/`, référentiels
(`models.json`, `classification_marques_*.json`), documentation. Non versionnés (`.gitignore`) : annonces scrapées,
`data/`, `output/`, `archives/`, `.venv`.
- L'historique Git remplace les copies de sauvegarde : plus de `bkp/` ni de fichiers `.before-*`.
- Rien de personnel dans le dépôt : identifiants, coordonnées précises, adresses e-mail, données d'annonces.
- `.gitattributes` désactive la conversion des fins de ligne : préserver CRLF dans `models.json` et les `.bat`.
