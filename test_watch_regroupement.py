"""Regroupement du registre sans modifier les règles d'identité des caméras."""

import copy
import itertools
import os
import tempfile
import unittest
from unittest import mock

os.environ["BLINK_BOOTSTRAP"] = "none"
_TEST_HOME = tempfile.TemporaryDirectory(prefix="blink-groupes-import-")
os.environ["BLINK_HOME"] = _TEST_HOME.name

import watch


class TestsRegroupementCameras(unittest.TestCase):
    def setUp(self):
        self.cameras = {
            "Jardin A": {"name": "Jardin", "network_id": "111", "device_id": "1"},
            "Jardin B": {"name": "Jardin", "network_id": "111", "device_id": "2"},
            "Jardin C": {"name": "Jardin", "network_id": "222", "device_id": "1"},
            "Salon": {"name": "Salon", "network_id": "333", "device_id": "3"},
            "Garage": {},  # État d'une ancienne version sans métadonnées.
        }

    def groupes(self, entrees):
        return watch.regrouper_entrees_cameras(self.cameras, entrees)

    def test_homonymes_et_identifiants_partiels_restent_prudents(self):
        entrees = {
            "renomme": {"camera": "Ancien jardin", "network_id": "111", "device_id": "1"},
            "usb": {"camera": " Jardin ", "network_id": 222},
            "appareil": {"camera": "JARDIN", "device_id": 2},
            "ancien": {"camera": " garage "},
            "nom_ambigu": {"camera": "Jardin"},
            "reseau_ambigu": {"camera": "Jardin", "network_id": "111"},
            "appareil_ambigu": {"camera": "Jardin", "device_id": "1"},
            "autre_reseau": {"camera": "Jardin", "network_id": "999", "device_id": "1"},
            "autre_appareil": {"camera": "Jardin", "network_id": "111", "device_id": "999"},
            "invalide": None,
        }
        avant = copy.deepcopy((self.cameras, entrees))
        groupes = self.groupes(entrees)
        self.assertEqual({nom: list(clips) for nom, clips in groupes.items()}, {
            "Jardin A": ["renomme"], "Jardin B": ["appareil"], "Jardin C": ["usb"],
            "Salon": [], "Garage": ["ancien"],
        })
        self.assertIs(groupes["Jardin A"]["renomme"], entrees["renomme"])
        self.assertEqual((self.cameras, entrees), avant)

    def test_equivalence_avec_la_selection_historique_sur_identites_incompletes(self):
        # Caractérise les rapprochements existants, y compris leurs replis
        # par nom. L'optimisation ne doit pas durcir ou élargir ces règles.
        entrees = {str(i): {"camera": nom, "network_id": reseau, "device_id": appareil}
                   for i, (nom, reseau, appareil) in enumerate(itertools.product(
                       ("Jardin", " JARDIN ", "Renommée", "Garage", "Salon", ""),
                       ("", "111", "222", "999"), ("", "1", "2", "999")))}
        attendu = {
            nom: {cle: entree for cle, entree in entrees.items()
                  if watch._cameras_correspondantes(entree, self.cameras) == [nom]}
            for nom in self.cameras
        }
        self.assertEqual(self.groupes(entrees), attendu)

    def test_ordre_du_registre_et_cameras_sans_clip_conserves(self):
        entrees = {cle: {"camera": "Salon"} for cle in ("z", "b", "a")}
        groupes = self.groupes(entrees)
        self.assertEqual(list(groupes), list(self.cameras))
        self.assertEqual(list(groupes["Salon"]), ["z", "b", "a"])
        self.assertEqual(groupes["Garage"], {})
        self.assertEqual(watch.camera_entries("inconnue", self.cameras, entrees), {})

    def test_dictionnaires_de_groupes_independants_et_entrees_invalides_ignorees(self):
        groupes = self.groupes({"nul": None, "liste": [], "nombre": 1, "texte": "x"})
        groupes["Jardin A"]["local"] = {}
        self.assertEqual(groupes["Jardin B"], {})
        self.assertEqual(self.groupes({})["Jardin A"], {})

    def test_identifiants_partiels_n_autorisent_pas_un_renommage(self):
        entrees = {
            "nom_ancien": {"camera": "Ancien nom", "network_id": "333"},
            "appareil_seul": {"camera": "Ancien nom", "device_id": "3"},
            "identite_complete": {"camera": "Ancien nom", "network_id": "333", "device_id": "3"},
        }
        self.assertEqual(list(self.groupes(entrees)["Salon"]), ["identite_complete"])

    def test_identifiants_complets_dupliques_restent_ambigus_malgre_le_nom(self):
        self.cameras["Salon bis"] = {**self.cameras["Salon"], "name": "Autre nom"}
        groupes = self.groupes({
            "ambigu": {"camera": "Salon", "network_id": "333", "device_id": "3"},
        })
        self.assertTrue(all(not entrees for entrees in groupes.values()))

    def test_chaque_entree_valide_est_rapprochee_une_seule_fois(self):
        entrees = {str(i): {"camera": "Salon"} for i in range(40)}
        entrees["invalide"] = None
        with mock.patch.object(watch, "_cameras_correspondantes",
                               wraps=watch._cameras_correspondantes) as rapprocher:
            groupes = self.groupes(entrees)
        self.assertEqual(rapprocher.call_count, 40)
        self.assertEqual(len(groupes["Salon"]), 40)

    def test_aucun_cache_ne_survit_a_un_changement_du_registre_ou_des_cameras(self):
        entree = {"camera": "Jardin"}
        self.assertTrue(all(not groupe for groupe in self.groupes({"clip": entree}).values()))
        entree.update(network_id="111", device_id="1")
        self.assertEqual(list(self.groupes({"clip": entree})["Jardin A"]), ["clip"])
        self.cameras["Doublon"] = dict(self.cameras["Jardin A"])
        self.assertTrue(all(not groupe for groupe in self.groupes({"clip": entree}).values()))


if __name__ == "__main__":
    unittest.main()
