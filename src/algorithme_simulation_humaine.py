"""Simulateur de road trip « rythme humain » (v2).

Idée : un trajet annuel de 1000 km, conduit comme avec une voiture thermique :
  * étape de 90 min au plus (≈ 180 km à 120 km/h), puis arrêt de 15 min ;
  * un arrêt « déjeuner » de 45 min, au premier arrêt à partir de midi ;
  * départ à 08:00, batterie pleine (100 %).

Pendant chaque arrêt prévu, la voiture se recharge gratuitement (le temps d'arrêt est le même
qu'en thermique). Si cela ne suffit pas à couvrir l'étape suivante :
  * l'étape est RACCOURCIE quand la batterie ne permet pas d'aller jusqu'au prochain arrêt
    (arrêt anticipé, avec la réserve de sécurité `soc_min_borne`) ;
  * la PAUSE est ALLONGÉE quand la recharge nécessaire dure plus longtemps que la pause prévue.

Le résultat est le temps total du trajet ; `temps_perdu_min` le compare à la même route en
thermique (même rythme, énergie illimitée). Le bilan d'énergie est toujours équilibré :
énergie consommée = batterie de départ + énergie rechargée - batterie restante.

Changements v2 par rapport à la v1 :
  * v1 pouvait « téléporter » de l'énergie : si la fin d'étape prévue passait sous 10 %, elle
    rechargeait 15 min au plus puis roulait quand même, avec un SoC ramené à 0 (le déficit
    disparaissait). Les voitures à courte autonomie étaient donc favorisées.
  * La recharge est calculée analytiquement (plus de pas d'une minute).
  * Le déjeuner n'est pris qu'une fois (v1 pouvait en enchaîner deux dans la fenêtre 12:00-13:30).
  * Paramètres optionnels : SoH (capacité effective), consommation autoroute mesurée par
    variante, délai de branchement.
"""
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

VERSION = "2.0"


class VehiculeSim:
    """Modèle simplifié d'un véhicule électrique pour la simulation autoroutière."""

    def __init__(
        self,
        capacite_utile_kwh: float,
        conso_wltp: float,
        temps_charge_10_80_min: float,
        segment: str = "B",
        soh: float = 1.0,
        conso_autoroute_kwh_100km: Optional[float] = None,
    ):
        self.capacite_nominale = float(capacite_utile_kwh)
        soh = float(soh)
        self.soh = soh / 100.0 if soh > 1.5 else soh          # accepte 0.92 ou 92
        self.soh = min(1.0, max(0.3, self.soh))
        # Capacité réellement disponible (batterie vieillie) ; la puissance de charge, elle, ne change pas.
        self.capacite_utile = self.capacite_nominale * self.soh

        segment = str(segment).upper().strip()
        if conso_autoroute_kwh_100km:
            self.conso_130 = float(conso_autoroute_kwh_100km)   # mesure réelle si disponible
        else:
            # Majoration de la consommation théorique WLTP pour un roulage autoroutier soutenu
            facteur_autoroute = 1.35 if segment == "B" else 1.30
            self.conso_130 = float(conso_wltp) * facteur_autoroute

        self.temps_recharge_plage = float(temps_charge_10_80_min)
        self.plage_ref = (10, 80)
        self.kwh_par_minute = self.calculer_kwh_par_minute()

    def calculer_kwh_par_minute(self) -> float:
        delta_soc = (self.plage_ref[1] - self.plage_ref[0]) / 100.0
        energie_totale_plage = self.capacite_nominale * delta_soc
        if self.temps_recharge_plage <= 0:
            return energie_totale_plage / 30.0
        return energie_totale_plage / self.temps_recharge_plage


class SimulateurRoadTripHumain:
    """Simule un trajet (1000 km par défaut) au rythme habituel du conducteur, voiture pleine au départ."""

    def __init__(
        self,
        vehicule: VehiculeSim,
        longueur_totale_km: float = 1000.0,
        vitesse_moyenne_kmh: float = 120.0,
        duree_etape_min: float = 90.0,
        duree_courte_pause_min: float = 15.0,
        duree_longue_pause_min: float = 45.0,
        heure_depart: str = "08:00",
        soc_min_borne: float = 0.10,
        arret_auto_80_pct: bool = True,
        soc_depart: float = 1.0,
        delai_branchement_min: float = 3.0,
        debut_dejeuner_h: float = 12.0,
        fin_dejeuner_h: float = 14.5,
    ):
        self.vehic = vehicule
        self.longueur_totale = float(longueur_totale_km)
        self.vitesse_moyenne = float(vitesse_moyenne_kmh)
        self.duree_etape = duree_etape_min / 60.0
        self.duree_courte_pause = duree_courte_pause_min / 60.0
        self.longue_pause = duree_longue_pause_min / 60.0

        h, m = map(int, heure_depart.split(":"))
        self.heure_depart_h = h + m / 60.0
        self.heure_actuelle = self.heure_depart_h

        self.soc_min_borne = soc_min_borne
        self.arret_auto_80 = arret_auto_80_pct
        self.soc_depart = soc_depart
        self.delai_branchement_min = delai_branchement_min   # temps perdu à brancher/débrancher/payer
        self.debut_dejeuner_h = debut_dejeuner_h
        self.fin_dejeuner_h = fin_dejeuner_h

        # Résultats détaillés de la dernière simulation
        self.distance_parcourue = 0.0
        self.soc_actuel = soc_depart
        self.ajustements_requis: List[int] = []   # arrêts où l'étape a été raccourcie ou la pause allongée
        self.journal: List[Dict[str, Any]] = []
        self.energie_consommee_kwh = 0.0
        self.energie_rechargee_kwh = 0.0

    # ------------------------------------------------------------------
    def simuler_recharge(self, duree_max_h: float, soc_depart: float) -> tuple[float, float]:
        """Recharge au plus `duree_max_h` heures (arrêt à 80 % si `arret_auto_80`). Renvoie (soc, heures)."""
        cible = 0.80 if self.arret_auto_80 else 1.0
        if soc_depart >= cible or duree_max_h <= 0:
            return soc_depart, 0.0
        kwh_min, cap = self.vehic.kwh_par_minute, self.vehic.capacite_utile
        minutes_pour_cible = (cible - soc_depart) * cap / kwh_min
        minutes = min(duree_max_h * 60.0, minutes_pour_cible)
        return min(cible, soc_depart + minutes * kwh_min / cap), minutes / 60.0

    def _simuler(self, energie_illimitee: bool = False) -> Dict[str, Any]:
        v = self.vehic
        cap, cons, kwh_min = v.capacite_utile, v.conso_130, v.kwh_par_minute
        vit = self.vitesse_moyenne
        etape_km = vit * self.duree_etape
        reserve = self.soc_min_borne
        cible_max = 0.80 if self.arret_auto_80 else 1.0
        delai = self.delai_branchement_min

        soc = 1.0 if energie_illimitee else self.soc_depart
        horloge = self.heure_depart_h
        dist = 0.0
        arrets = etapes_raccourcies = 0
        pause_supp_min = consommee = rechargee = 0.0
        dejeuner_pris = False
        journal: List[Dict[str, Any]] = []
        ajustements: List[int] = []

        if not energie_illimitee and (cap <= 0 or cons <= 0 or (cible_max - reserve) * cap / cons * 100.0 < 20.0):   # étape max < 20 km : irréaliste
            return {"realisable": False}

        for _ in range(100000):
            if dist >= self.longueur_totale - 1e-9:
                break
            restant = self.longueur_totale - dist
            prevue = min(etape_km, restant)

            # Étape : celle prévue, ou raccourcie si la batterie ne permet pas d'aller jusqu'à l'arrêt prévu.
            raccourcie = False
            if energie_illimitee:
                leg = prevue
            else:
                portee = max(0.0, (soc - reserve) * cap / cons * 100.0)
                if portee >= prevue - 1e-9:
                    leg = prevue
                else:
                    leg, raccourcie = portee, True
                if leg <= 1e-6:
                    return {"realisable": False}

            dist += leg
            horloge += leg / vit
            if not energie_illimitee:
                e = leg / 100.0 * cons
                soc -= e / cap
                consommee += e
            if dist >= self.longueur_totale - 1e-9:
                break

            # Arrêt : durée prévue (15 min, ou déjeuner au premier arrêt à partir de midi).
            arrets += 1
            est_dejeuner = (not dejeuner_pris) and self.debut_dejeuner_h <= horloge <= self.fin_dejeuner_h
            if est_dejeuner:
                dejeuner_pris = True
            pause_min = (self.longue_pause if est_dejeuner else self.duree_courte_pause) * 60.0
            duree = pause_min
            soc_avant, recharge_kwh = soc, 0.0

            if not energie_illimitee:
                prochaine = min(etape_km, self.longueur_totale - dist)
                besoin = reserve + prochaine / 100.0 * cons / cap     # SoC requis pour la prochaine étape
                cible = min(besoin, cible_max)
                dispo = max(0.0, pause_min - delai)                    # recharge gratuite pendant la pause
                soc_libre = soc if soc >= cible_max else min(cible_max, soc + dispo * kwh_min / cap)
                if soc_libre >= cible - 1e-9:
                    soc_new = soc_libre
                else:                                                  # pause allongée : on recharge le nécessaire
                    soc_new = cible
                    duree = max(pause_min, delai + (cible - soc) * cap / kwh_min)
                recharge_kwh = max(0.0, (soc_new - soc) * cap)
                rechargee += recharge_kwh
                soc = soc_new

            supp = duree - pause_min
            pause_supp_min += supp
            horloge += duree / 60.0
            if raccourcie:
                etapes_raccourcies += 1
            if raccourcie or supp > 1e-9:
                ajustements.append(arrets)
            journal.append({
                "arret": arrets, "km": round(dist, 1), "heure": round(horloge - duree / 60.0, 2),
                "soc_arrivee": round(soc_avant, 3), "soc_depart": round(soc, 3),
                "pause_min": round(duree, 1), "pause_supp_min": round(supp, 1),
                "recharge_kwh": round(recharge_kwh, 1), "etape_raccourcie": raccourcie,
                "dejeuner": est_dejeuner,
            })
        else:
            return {"realisable": False}

        return {
            "realisable": True, "minutes": (horloge - self.heure_depart_h) * 60.0, "arrets": arrets,
            "etapes_raccourcies": etapes_raccourcies, "pause_supp_min": pause_supp_min,
            "consommee": consommee, "rechargee": rechargee, "soc_fin": soc,
            "journal": journal, "ajustements": ajustements,
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _formater(minutes: float) -> str:
        total_hours = minutes / 60.0
        h = int(total_hours)
        m = int(round((total_hours - h) * 60))
        if m == 60:
            h, m = h + 1, 0
        return f"{h}h{m:02d}"

    def executer_voyage(self) -> dict:
        ref = self._simuler(energie_illimitee=True)
        res = self._simuler(energie_illimitee=False)
        if not res["realisable"]:
            return {
                "realisable": False, "temps_formate": None, "temps_minutes": None,
                "temps_heures_decimales": None,
            }

        self.journal = res["journal"]
        self.ajustements_requis = res["ajustements"]
        self.energie_consommee_kwh = res["consommee"]
        self.energie_rechargee_kwh = res["rechargee"]
        self.soc_actuel = res["soc_fin"]
        self.distance_parcourue = self.longueur_totale

        v = self.vehic
        total_min = res["minutes"]
        return {
            "realisable": True,
            "temps_formate": self._formater(total_min),
            "temps_minutes": round(total_min, 1),
            "temps_heures_decimales": round(total_min / 60.0, 2),
            "temps_reference_thermique_min": round(ref["minutes"], 1),
            "temps_perdu_min": round(total_min - ref["minutes"], 1),
            "nb_arrets": res["arrets"],
            "nb_etapes_raccourcies": res["etapes_raccourcies"],
            "pause_supplementaire_min": round(res["pause_supp_min"], 1),
            "autonomie_autoroute_km": round((1.0 - self.soc_min_borne) * v.capacite_utile / v.conso_130 * 100.0, 0),
        }


def simuler_variante(
    capacite_utile_kwh: float,
    conso_wltp: float,
    temps_charge_10_80_min: float,
    segment: str = "B",
    soh: float = 1.0,
    conso_autoroute_kwh_100km: Optional[float] = None,
    **params: Any,
) -> dict:
    """Raccourci pour simuler une variante (ex. une annonce avec son SoH réel) sans passer par le JSON."""
    vehicule = VehiculeSim(
        capacite_utile_kwh, conso_wltp, temps_charge_10_80_min, segment, soh, conso_autoroute_kwh_100km
    )
    return SimulateurRoadTripHumain(vehicule, **params).executer_voyage()


def _libelle(marque: dict, modele: dict, variante: dict) -> str:
    parts = [
        marque.get("marque_nom") or marque.get("nom") or marque.get("marque_id") or marque.get("id"),
        modele.get("nom_canonique") or modele.get("nom") or modele.get("id"),
        variante.get("nom_variante") or variante.get("nom") or variante.get("id"),
    ]
    return " ".join(str(p) for p in parts if p)


def enrichir_referentiel(data: dict, imprimer: Optional[List[str]] = None) -> dict:
    metadata = data.setdefault("metadata", {})
    metadata["algorithme_simulation_humaine"] = {
        "nom": "SimulateurRoadTripHumain",
        "version": VERSION,
        "cible": "Temps total 1000 km au rythme habituel (étape 90 min, pause 15 min, déjeuner 45 min), "
                 "départ batterie pleine ; étapes raccourcies / pauses allongées si l'énergie manque",
        "hypotheses": {
            "longueur_km": 1000, "vitesse_kmh": 120, "etape_min": 90, "pause_min": 15, "dejeuner_min": 45,
            "reserve_soc": 0.10, "recharge_max_soc": 0.80, "delai_branchement_min": 3,
        },
    }

    ignorees = 0
    for marque in data.get("catalogue_filtrage", []):
        for modele in marque.get("modeles", []):
            segment = modele.get("segment", "B")
            for variante in modele.get("variantes", []):
                tech = variante.get("donnees_techniques", {})
                try:
                    vehicule = VehiculeSim(
                        capacite_utile_kwh=tech["batterie_utile_kwh"],
                        conso_wltp=tech["conso_wltp_theorique_kwh_100km"],
                        temps_charge_10_80_min=tech["temps_charge_10_80_min"],
                        segment=segment,
                        conso_autoroute_kwh_100km=tech.get("conso_autoroute_kwh_100km"),
                    )
                except (KeyError, TypeError, ValueError):
                    ignorees += 1
                    continue
                resultat = SimulateurRoadTripHumain(vehicule).executer_voyage()
                variante["simulation_humaine_1000km"] = resultat
                if imprimer and any(p.lower() in _libelle(marque, modele, variante).lower() for p in imprimer):
                    print(f"  {_libelle(marque, modele, variante)} (segment {segment}) -> "
                          f"{resultat['temps_minutes']} min, perdu {resultat.get('temps_perdu_min')} min")
    if ignorees:
        print(f"⚠️ {ignorees} variante(s) ignorée(s) : données techniques incomplètes.")
    return data


def main(
    input_path: str = "model.json",
    output_path: str = "referentiel_ev_simule.json",
    imprimer_references: Optional[List[str]] = None,
) -> None:
    raw_data = json.loads(Path(input_path).read_text(encoding="utf-8"))
    enriched = enrichir_referentiel(raw_data, imprimer_references)
    Path(output_path).write_text(
        json.dumps(enriched, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Fichier généré avec succès : {output_path}")


if __name__ == "__main__":
    import sys

    # Usage : python algorithme_simulation_humaine.py [entrée.json] [sortie.json] [filtre1 filtre2 ...]
    # Les filtres (sous-chaînes) impriment le temps simulé des variantes correspondantes, utile pour
    # mettre à jour ref_sim_min dans market_estimator.py (ex. "spring" "ioniq 3").
    args = sys.argv[1:]
    main(*(args[:2]), imprimer_references=args[2:] or None)
