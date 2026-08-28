"""Non-régression de blink_cli.redemarrer (AUDIT-2026-08-13.md, section
28.32) : verbe « restart » qui sert le panneau de réglages de la page web
(bouton Appliquer et bouton Stop), sans jamais toucher à un vrai processus.

Le premier temps (sans --finaliser) doit rendre la main tout de suite après
avoir lancé le second : c'est cette sortie rapide qui le détache de l'arbre
que « stop » va abattre. Le second temps (--finaliser) doit arrêter puis,
sauf --sans-relance, relancer « start »."""

from __future__ import annotations

import contextlib
import unittest
from unittest import mock

import blink_cli


class TestsRedemarrerPremierTemps(unittest.TestCase):
    """Sans --finaliser : uniquement lancer le second temps, détaché."""

    def test_lance_le_second_temps_et_rend_la_main(self):
        with mock.patch.object(blink_cli.runtime, "demarrer") as demarrer, \
             mock.patch.object(blink_cli.runtime, "lancer") as lancer:
            code = blink_cli.redemarrer([])
        self.assertEqual(code, 0)
        lancer.assert_not_called()
        demarrer.assert_called_once()
        commande = demarrer.call_args[0][0]
        self.assertIn("restart", commande)
        self.assertIn("--finaliser", commande)
        self.assertNotIn("--sans-relance", commande)

    def test_relaie_sans_relance_au_second_temps(self):
        with mock.patch.object(blink_cli.runtime, "demarrer") as demarrer:
            blink_cli.redemarrer(["--sans-relance"])
        commande = demarrer.call_args[0][0]
        self.assertIn("--finaliser", commande)
        self.assertIn("--sans-relance", commande)


class TestsRedemarrerSecondTemps(unittest.TestCase):
    """Avec --finaliser : arrêter, attendre, puis relancer sauf --sans-relance."""

    def test_arrete_attend_puis_relance(self):
        with mock.patch.object(blink_cli, "_arreter_instances", return_value=0) as arreter, \
             mock.patch.object(blink_cli.runtime, "lire_instances",
                                return_value=[]) as lire_instances, \
             mock.patch.object(blink_cli.runtime, "verrou_controle",
                               return_value=contextlib.nullcontext()), \
             mock.patch.object(blink_cli.runtime, "demarrer") as demarrer, \
             mock.patch.object(blink_cli.time, "sleep") as sommeil:
            code = blink_cli.redemarrer(["--finaliser"])
        self.assertEqual(code, 0)
        arreter.assert_called_once()
        # Plus rien à attendre dès le premier passage : aucune pause.
        lire_instances.assert_called_once()
        sommeil.assert_not_called()
        # Puis relancé, avec « start ».
        demarrer.assert_called_once()
        self.assertIn("start", demarrer.call_args[0][0])

    def test_sans_relance_n_appelle_pas_start(self):
        with mock.patch.object(blink_cli, "_arreter_instances", return_value=0), \
             mock.patch.object(blink_cli.runtime, "lire_instances", return_value=[]), \
             mock.patch.object(blink_cli.runtime, "verrou_controle",
                               return_value=contextlib.nullcontext()), \
             mock.patch.object(blink_cli.runtime, "demarrer") as demarrer:
            code = blink_cli.redemarrer(["--finaliser", "--sans-relance"])
        self.assertEqual(code, 0)
        demarrer.assert_not_called()

    def test_attend_que_les_instances_disparaissent(self):
        # Encore là deux fois, plus là ensuite : deux pauses, pas vingt.
        etats = [["encore"], ["encore"], []]
        with mock.patch.object(blink_cli, "_arreter_instances", return_value=0), \
             mock.patch.object(blink_cli.runtime, "lire_instances",
                                side_effect=etats) as lire_instances, \
             mock.patch.object(blink_cli.runtime, "verrou_controle",
                               return_value=contextlib.nullcontext()), \
             mock.patch.object(blink_cli.runtime, "demarrer"), \
             mock.patch.object(blink_cli.time, "sleep") as sommeil:
            blink_cli.redemarrer(["--finaliser"])
        self.assertEqual(lire_instances.call_count, 3)
        self.assertEqual(sommeil.call_count, 2)

    def test_renonce_apres_vingt_passages_sans_empiler_une_instance(self):
        with mock.patch.object(blink_cli, "_arreter_instances", return_value=0), \
             mock.patch.object(blink_cli.runtime, "lire_instances",
                                return_value=["toujours_la"]) as lire_instances, \
             mock.patch.object(blink_cli.runtime, "verrou_controle",
                               return_value=contextlib.nullcontext()), \
             mock.patch.object(blink_cli.runtime, "demarrer") as demarrer, \
             mock.patch.object(blink_cli.time, "sleep"):
            code = blink_cli.redemarrer(["--finaliser"])
        self.assertEqual(code, 1)
        self.assertEqual(lire_instances.call_count, 20)
        demarrer.assert_not_called()

    def test_echec_de_stop_est_retourne_sans_relance(self):
        with mock.patch.object(blink_cli, "_arreter_instances", return_value=3), \
             mock.patch.object(blink_cli.runtime, "lire_instances") as lire_instances, \
             mock.patch.object(blink_cli.runtime, "verrou_controle",
                               return_value=contextlib.nullcontext()), \
             mock.patch.object(blink_cli.runtime, "demarrer") as demarrer:
            code = blink_cli.redemarrer(["--finaliser"])
        self.assertEqual(code, 3)
        lire_instances.assert_not_called()
        demarrer.assert_not_called()


class TestsArretIncomplet(unittest.TestCase):
    def test_conserve_la_demande_arret_si_un_processus_survit(self):
        instance = {
            "pid": 123, "depuis": "maintenant", "verbes": [["serve"]],
            "enfants": [], "identites": {"123": "creation-attendue"},
            "fiche": "inutilisee.json",
        }
        horloge = {"t": 0.0}
        with mock.patch.object(blink_cli.runtime, "lire_instances",
                               return_value=[instance]), \
             mock.patch.object(blink_cli.runtime, "demander_arret") as demander, \
             mock.patch.object(blink_cli.runtime, "effacer_arret_demande") as effacer, \
             mock.patch.object(blink_cli.runtime, "processus_vivant", return_value=True), \
             mock.patch.object(blink_cli.runtime, "identite_processus", return_value=None), \
             mock.patch.object(blink_cli.runtime, "arreter_processus"), \
             mock.patch.object(blink_cli.time, "time", side_effect=lambda: horloge["t"]), \
             mock.patch.object(blink_cli.time, "sleep",
                               side_effect=lambda d: horloge.__setitem__("t", horloge["t"] + d)):
            code = blink_cli._arreter_instances()
        self.assertEqual(code, 1)
        demander.assert_called_once()
        effacer.assert_not_called()


class TestsSerialisationArret(unittest.TestCase):
    def test_stop_execute_son_corps_sous_verrou(self):
        with mock.patch.object(blink_cli.runtime, "verrou_controle",
                               return_value=contextlib.nullcontext()) as verrou, \
             mock.patch.object(blink_cli, "_arreter_instances", return_value=0) as corps:
            self.assertEqual(blink_cli.arreter([]), 0)
        verrou.assert_called_once_with("stop")
        corps.assert_called_once()

    def test_stop_concurrent_echoue_sans_toucher_aux_instances(self):
        with mock.patch.object(blink_cli.runtime, "verrou_controle",
                               side_effect=blink_cli.runtime.BusyError("occupé")), \
             mock.patch.object(blink_cli, "_arreter_instances") as corps:
            self.assertEqual(blink_cli.arreter([]), 1)
        corps.assert_not_called()


class TestsRedemarrerDansVerbes(unittest.TestCase):
    def test_restart_route_vers_un_fichier_existant(self):
        commande = blink_cli.runtime.self_command("restart")
        from pathlib import Path
        self.assertTrue(Path(commande[2]).is_file())


if __name__ == "__main__":
    unittest.main()
