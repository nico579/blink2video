"""Deux Sync Modules qui portent le même nom (« My Blink Sync Module », le nom
par défaut) : le module hors ligne était comparé au module en ligne du même nom
et sa chute signalée à chaque tour, par une fenêtre toutes les dix minutes
(2026-10-05, le module du Portail éteint). Un module se retrouve par son
identifiant Blink, pas par son nom."""

import datetime as dt
import os
import tempfile
import unittest
from unittest import mock

_IMPORT_HOME = tempfile.TemporaryDirectory(prefix="blink-watch-modules-import-")
with mock.patch.dict(os.environ, {"BLINK_BOOTSTRAP": "none", "BLINK_HOME": _IMPORT_HOME.name}):
    import runtime
    import watch

NOM = "My Blink Sync Module"


def module(identifiant, en_ligne, reseau="", nom=NOM):
    return {"name": nom, "id": identifiant, "network_id": reseau, "online": en_ligne}


class ModulesHomonymes(unittest.TestCase):
    def setUp(self):
        langue = mock.patch.object(runtime, "lire_langue", return_value="fr")
        langue.start()
        self.addCleanup(langue.stop)

    @staticmethod
    def comparer(avant, courant):
        return watch.compare(
            {"modules": avant, "cameras": {}}, {"modules": courant, "cameras": {}},
            dt.timezone.utc, set())

    def test_module_hors_ligne_depuis_un_tour_n_est_pas_resignale(self):
        etat = [module("1", True, "436363"), module("2", False, "808060")]
        alertes, retours = self.comparer(etat, etat)
        self.assertEqual((alertes, retours), ([], []))

    def test_chute_d_un_des_deux_modules_signalee_une_fois_avec_son_reseau(self):
        avant = [module("1", True, "436363"), module("2", True, "808060")]
        apres = [module("1", True, "436363"), module("2", False, "808060")]
        alertes, retours = self.comparer(avant, apres)
        self.assertEqual(len(alertes), 1)
        self.assertIn("808060", alertes[0])
        self.assertNotIn("436363", alertes[0])
        self.assertEqual(retours, [])

    def test_retour_en_ligne_du_bon_module(self):
        avant = [module("1", True, "436363"), module("2", False, "808060")]
        apres = [module("1", True, "436363"), module("2", True, "808060")]
        alertes, retours = self.comparer(avant, apres)
        self.assertEqual(alertes, [])
        self.assertEqual(len(retours), 1)
        self.assertIn("808060", retours[0])

    def test_ordre_different_dans_la_reponse_de_blink(self):
        avant = [module("1", True, "436363"), module("2", False, "808060")]
        apres = [module("2", False, "808060"), module("1", True, "436363")]
        self.assertEqual(self.comparer(avant, apres), ([], []))

    def test_ancien_etat_sans_identifiants_et_noms_identiques_signale_une_fois(self):
        # État écrit avant cette version : pas d'id, deux entrées de même nom.
        avant = [{"name": NOM, "online": True}, {"name": NOM, "online": False}]
        apres = [module("1", True, "436363"), module("2", False, "808060")]
        alertes, retours = self.comparer(avant, apres)
        self.assertEqual(len(alertes), 1)
        self.assertEqual(retours, [])
        # Et le tour suivant, état désormais avec identifiants : plus rien.
        self.assertEqual(self.comparer(apres, apres), ([], []))

    def test_noms_distincts_et_sans_identifiant_inchanges(self):
        avant = [{"name": "Maison", "online": True}]
        apres = [{"name": "Maison", "online": False}]
        alertes, _ = self.comparer(avant, apres)
        self.assertEqual(len(alertes), 1)
        self.assertIn("Maison", alertes[0])
        self.assertNotIn("(", alertes[0])

    def test_module_nouveau_hors_ligne_est_signale(self):
        alertes, _ = self.comparer([module("1", True)], [module("1", True), module("3", False)])
        self.assertEqual(len(alertes), 1)

    def test_le_nom_seul_ne_designe_pas_un_module_d_un_autre_identifiant(self):
        # Même nom, autre identifiant : c'est un autre module, pas l'ancien.
        avant = [module("1", False)]
        apres = [module("9", False)]
        alertes, _ = self.comparer(avant, apres)
        self.assertEqual(len(alertes), 1)


if __name__ == "__main__":
    unittest.main()
