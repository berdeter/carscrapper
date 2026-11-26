"""Voitures de référence par segment, choisies par l'utilisateur (data/references.json).

L'indice d'opportunité compare chaque annonce à une voiture NEUVE de référence du même segment. Le choix
n'est pas figé dans le code : il est enregistré dans data/references.json (non versionné, propre à chaque
utilisateur) par :

    batch\\set_reference_variant.bat A dacia_spring_27kwh 17890
    batch\\set_reference_variant.bat B hyundai_ioniq_3_42kwh 31495

Seuls l'identifiant de variante et le prix neuf réellement payé (remises déduites) sont enregistrés. Tout
le reste (nom, simulation 1000 km, plateforme, autonomie, score de marque) est relu à chaque estimation dans
models.json et dans la classification des marques : la référence suit donc automatiquement les mises à jour
du catalogue.

    python src/references.py                     # affiche les références et les variantes disponibles
    python src/references.py set A <variante_id> <prix_neuf> [remarque...]
"""
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import paths


class MissingReferenceError(RuntimeError):
    """Une référence de segment manque ou n'est plus valable : l'estimation est refusée."""


def load_catalog(catalog_path: Path = paths.CATALOG) -> Dict[str, Any]:
    return json.loads(Path(catalog_path).read_text(encoding="utf-8"))


def catalog_segments(catalog: Dict[str, Any]) -> List[str]:
    return sorted({m.get("segment", "").upper() for b in catalog.get("catalogue_filtrage", [])
                   for m in b.get("modeles", []) if m.get("segment")})


def find_variant(catalog: Dict[str, Any], variante_id: str) -> Optional[Tuple[Dict, Dict, Dict]]:
    """(marque, modèle, variante) du catalogue, ou None."""
    for b in catalog.get("catalogue_filtrage", []):
        for m in b.get("modeles", []):
            for v in m.get("variantes", []):
                if v.get("variante_id") == variante_id:
                    return b, m, v
    return None


def load_references(path: Path = paths.REFERENCES) -> Dict[str, Dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k.upper(): v for k, v in data.get("references", {}).items()}


def save_references(refs: Dict[str, Dict[str, Any]], path: Path = paths.REFERENCES) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "references": dict(sorted(refs.items()))}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def how_to_fix(segment: str) -> str:
    return (f"  batch\\set_reference_variant.bat {segment} <variante_id> <prix_neuf>\n"
            f"  (batch\\set_reference_variant.bat sans argument liste les variantes disponibles)")


def resolve_references(catalog: Dict[str, Any], path: Path = paths.REFERENCES) -> Dict[str, Dict[str, Any]]:
    """{segment: {"prix_neuf", "marque", "modele", "variante"}} pour TOUS les segments du catalogue.
    Lève MissingReferenceError si une référence manque, n'existe plus dans le catalogue ou n'a pas de
    simulation 1000 km réalisable."""
    refs = load_references(path)
    out, problems = {}, []
    for seg in catalog_segments(catalog):
        ref = refs.get(seg)
        if not ref:
            problems.append(f"- segment {seg} : aucune voiture de référence choisie.\n{how_to_fix(seg)}")
            continue
        found = find_variant(catalog, ref.get("variante_id", ""))
        if found is None:
            problems.append(f"- segment {seg} : la variante « {ref.get('variante_id')} » n'existe plus dans "
                            f"models.json.\n{how_to_fix(seg)}")
            continue
        b, m, v = found
        sim = v.get("simulation_humaine_1000km") or {}
        if not sim.get("realisable") or not sim.get("temps_minutes"):
            problems.append(f"- segment {seg} : « {ref['variante_id']} » n'a pas de simulation 1000 km réalisable "
                            f"(relancer batch\\simulate.bat ou choisir une autre variante).")
            continue
        out[seg] = {"prix_neuf": float(ref["prix_neuf"]), "marque": b, "modele": m, "variante": v,
                    "remarque": ref.get("remarque", "")}
    if problems:
        raise MissingReferenceError(
            f"Estimation refusée : voitures de référence à définir ({paths.REFERENCES}).\n" + "\n".join(problems))
    return out


def set_reference(segment: str, variante_id: str, prix_neuf: str, remarque: str = "",
                  path: Path = paths.REFERENCES) -> str:
    segment = segment.upper()
    catalog = load_catalog()
    segments = catalog_segments(catalog)
    if segment not in segments:
        raise ValueError(f"Segment « {segment} » inconnu (segments du catalogue : {', '.join(segments)}).")
    found = find_variant(catalog, variante_id)
    if found is None:
        raise ValueError(f"Variante « {variante_id} » introuvable dans models.json.")
    b, m, v = found
    if m.get("segment", "").upper() != segment:
        raise ValueError(f"« {variante_id} » est une voiture du segment {m.get('segment')}, pas {segment}.")
    try:
        prix = float(str(prix_neuf).replace(" ", "").replace(" ", "").replace(",", "."))
    except ValueError:
        raise ValueError(f"Prix neuf invalide : « {prix_neuf} » (nombre attendu, ex. 31495).")
    if not 3_000 <= prix <= 300_000:
        raise ValueError(f"Prix neuf peu plausible : {prix:.0f} €.")
    refs = load_references(path)
    refs[segment] = {"variante_id": variante_id, "prix_neuf": prix, "remarque": remarque,
                     "defini_le": date.today().isoformat()}
    save_references(refs, path)
    return f"Segment {segment} : {b['marque_nom']} {m['nom_canonique']} — {v['nom_variante']} à {prix:,.0f} €".replace(",", " ")


def describe() -> str:
    catalog = load_catalog()
    refs = load_references()
    lines = [f"Références actuelles ({paths.REFERENCES}) :"]
    for seg in catalog_segments(catalog):
        ref = refs.get(seg)
        found = find_variant(catalog, ref["variante_id"]) if ref else None
        if found:
            b, m, v = found
            lines.append(f"  {seg} : {v['variante_id']}  ({b['marque_nom']} {m['nom_canonique']} — {v['nom_variante']}),"
                         f" {ref['prix_neuf']:,.0f} €".replace(",", " ") + (f"  [{ref['remarque']}]" if ref.get("remarque") else ""))
        else:
            lines.append(f"  {seg} : NON DÉFINIE" + (f" (variante {ref['variante_id']} absente du catalogue)" if ref else ""))
    lines.append("\nVariantes disponibles :")
    for seg in catalog_segments(catalog):
        lines.append(f"  --- segment {seg} ---")
        for b in catalog["catalogue_filtrage"]:
            for m in b["modeles"]:
                if m.get("segment", "").upper() != seg:
                    continue
                for v in m["variantes"]:
                    sim = v.get("simulation_humaine_1000km") or {}
                    t = f"{sim['temps_minutes']:.0f} min / 1000 km" if sim.get("realisable") else "trajet irréalisable"
                    lines.append(f"  {v['variante_id']:<32} {b['marque_nom']} {m['nom_canonique']} — {v['nom_variante']} ({t})")
    return "\n".join(lines)


def main(argv: List[str]) -> int:
    if not argv or argv[0] in ("show", "list"):
        print(describe())
        return 0
    if argv[0] == "set" and len(argv) >= 4:
        try:
            print("Référence enregistrée. " + set_reference(argv[1], argv[2], argv[3], " ".join(argv[4:])))
        except ValueError as e:
            print(f"[ERREUR] {e}")
            return 1
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
