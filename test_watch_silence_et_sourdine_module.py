"""Deux points laissés à la décision de l'utilisateur (backlog) :

#9, une caméra qui n'a jamais rien enregistré n'entrait pas dans `last_clip`,
donc jamais dans le contrôle de silence : on garde maintenant la date de son
premier relevé et on alerte au franchissement du seuil.

Sourdine d'un Sync Module : la fenêtre « Module hors ligne » ne proposait que
--ignore pour une caméra. --ignore-module / --unignore-module, par identifiant
(les noms par défaut de deux modules sont identiques)."""

import contextlib
import datetime as dt
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_IMPORT_HOME = tempfile.TemporaryDirectory(prefix="blink-watch-silence-import-")
with mock.patch.dict(os.environ, {"BLINK_BOOTSTRAP": "none", "BLINK_HOME": _IMPORT_HOME.name}):
    import merge_daily as md
    import runtime
    import watch

NOM = "My Blink Sync Module"
DEBUT = dt.datetime(2026, 10, 1, 12, tzinfo=dt.timezone.utc)


def camera(**c):
    return {"online": True, "armed": True, "battery": "ok", "system_armed": True, **c}


def module(identifiant, en_ligne, reseau="", nom=NOM):
    return {"name": nom, "id": identifiant, "network_id": reseau, "online": en_ligne}


class Horloge(dt.datetime):
    maintenant = DEBUT

    @classmethod
    def now(cls, tz=None):
        return cls.maintenant.astimezone(tz) if tz else cls.maintenant.replace(tzinfo=None)


class Base(unittest.TestCase):
    def setUp(self):
        for patcher in (
            mock.patch.object(runtime, "lire_langue", return_value="fr"),
            mock.patch.object(watch.dt, "datetime", Horloge),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def aller_a(**delai):
        Horloge.maintenant = DEBUT + dt.timedelta(**delai)


class CameraJamaisEnregistree(Base):
    def tour(self, previous, jours, cameras=None, last_clip=None):
        """Un tour à `jours` jours du début ; rend (alertes, état à écrire)."""
        self.aller_a(days=jours)
        current = {"at": Horloge.maintenant.isoformat(), "modules": [],
                   "cameras": cameras if cameras is not None else {"Cave": camera()},
                   "last_clip": last_clip or {}}
        current["first_seen"] = watch.suivre_premiers_releves(previous, current)
        alertes, _ = watch.compare(previous, current, dt.timezone.utc, set())
        return alertes, current

    def test_premier_releve_n_alerte_pas(self):
        alertes, etat = self.tour({}, 0)
        self.assertEqual(alertes, [])
        self.assertIn("Cave", etat["first_seen"])

    def test_alerte_une_seule_fois_au_franchissement_du_seuil(self):
        _, etat = self.tour({}, 0)
        alertes, etat = self.tour(etat, 1)
        self.assertEqual(alertes, [])
        alertes, etat = self.tour(etat, 2)
        self.assertEqual(len(alertes), 1)
        self.assertIn("Cave", alertes[0])
        self.assertIn("2 jour", alertes[0])
        for jours in (3, 4, 10):
            alertes, etat = self.tour(etat, jours)
            self.assertEqual(alertes, [], jours)

    def test_la_date_du_premier_releve_est_conservee_d_un_tour_a_l_autre(self):
        _, etat = self.tour({}, 0)
        premiere = etat["first_seen"]["Cave"]
        _, etat = self.tour(etat, 1)
        self.assertEqual(etat["first_seen"]["Cave"], premiere)

    def test_camera_hors_ligne_ou_desarmee_n_alerte_pas(self):
        _, etat = self.tour({}, 0)
        for etat_camera in (camera(online=False), camera(armed=False)):
            alertes, _ = self.tour(etat, 3, cameras={"Cave": etat_camera})
            # La chute en elle-même reste signalée par l'alerte ordinaire.
            self.assertEqual([a for a in alertes if "surveillance" in a], [])

    def test_une_camera_qui_enregistre_sort_du_suivi(self):
        _, etat = self.tour({}, 0)
        iso = (DEBUT + dt.timedelta(days=3)).isoformat()
        alertes, etat = self.tour(etat, 3, last_clip={"Cave": iso})
        self.assertEqual(alertes, [])
        self.assertNotIn("Cave", etat["first_seen"])

    def test_camera_disparue_de_l_installation_sort_du_suivi(self):
        _, etat = self.tour({}, 0)
        _, etat = self.tour(etat, 1, cameras={})
        self.assertEqual(etat["first_seen"], {})

    def test_camera_en_sourdine_n_alerte_pas(self):
        _, etat = self.tour({}, 0)
        self.aller_a(days=3)
        current = {"at": Horloge.maintenant.isoformat(), "modules": [],
                   "cameras": {"Cave": camera()}, "last_clip": {}}
        current["first_seen"] = watch.suivre_premiers_releves(etat, current)
        alertes, _ = watch.compare(etat, current, dt.timezone.utc, {"Cave"})
        self.assertEqual(alertes, [])

    def test_camera_qui_a_deja_enregistre_garde_le_controle_existant(self):
        iso = (DEBUT - dt.timedelta(days=5)).isoformat()
        alertes, _ = self.tour({}, 0, last_clip={"Cave": iso})
        self.assertEqual(len(alertes), 1)
        self.assertIn("aucun clip depuis", alertes[0])


class SourdineDeModule(Base):
    def comparer(self, avant, courant, muets=()):
        previous = {"modules": avant, "cameras": {}, "ignored_modules": list(muets)}
        return watch.compare(previous, {"modules": courant, "cameras": {}},
                             dt.timezone.utc, set())

    def test_module_en_sourdine_ne_produit_ni_alerte_ni_retour(self):
        en_ligne = [module("1", True, "436363"), module("2", True, "808060")]
        tombe = [module("1", True, "436363"), module("2", False, "808060")]
        self.assertEqual(self.comparer(en_ligne, tombe, muets=["2"]), ([], []))
        self.assertEqual(self.comparer(tombe, en_ligne, muets=["2"]), ([], []))

    def test_la_sourdine_d_un_module_laisse_l_autre_alerter(self):
        en_ligne = [module("1", True, "436363"), module("2", True, "808060")]
        tombe = [module("1", False, "436363"), module("2", True, "808060")]
        alertes, _ = self.comparer(en_ligne, tombe, muets=["2"])
        self.assertEqual(len(alertes), 1)
        self.assertIn("436363", alertes[0])

    def test_module_sans_id_se_met_en_sourdine_par_son_nom(self):
        avant = [{"name": "Maison", "online": True}]
        apres = [{"name": "Maison", "online": False}]
        self.assertEqual(self.comparer(avant, apres, muets=["Maison"]), ([], []))


class CommandeSourdineModule(Base):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory(prefix="blink-watch-sourdine-")
        self.addCleanup(self.tmp.cleanup)
        etat = Path(self.tmp.name) / ".blink_watch_state.json"
        patcher = mock.patch.object(watch, "WATCH_STATE", etat)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.etat = etat
        md.save_json(etat, {"modules": [module("1", True, "436363"),
                                        module("2", False, "808060")], "cameras": {}})
        verrou = mock.patch.object(runtime, "verrou", side_effect=lambda *a, **k: contextlib.nullcontext())
        verrou.start()
        self.addCleanup(verrou.stop)

    def lancer(self, ignorer=(), lever=()):
        args = SimpleNamespace(ignore_module=list(ignorer), unignore_module=list(lever))
        sortie = io.StringIO()
        with contextlib.redirect_stdout(sortie):
            code = watch._sourdine_modules(args)
        return code, sortie.getvalue(), md.load_json(self.etat, {})

    def test_par_identifiant(self):
        code, _, etat = self.lancer(["2"])
        self.assertEqual((code, etat["ignored_modules"]), (0, ["2"]))

    def test_par_reseau(self):
        _, _, etat = self.lancer(["808060"])
        self.assertEqual(etat["ignored_modules"], ["2"])

    def test_nom_partage_par_deux_modules_est_refuse_et_rien_n_est_ecrit(self):
        code, sortie, etat = self.lancer([NOM])
        self.assertEqual(code, 2)
        self.assertIn("plusieurs modules", sortie)
        self.assertNotIn("ignored_modules", etat)

    def test_libelle_avec_reseau_designe_un_seul_module(self):
        _, _, etat = self.lancer([f"{NOM} (808060)"])
        self.assertEqual(etat["ignored_modules"], ["2"])

    def test_module_inconnu_liste_les_modules_connus(self):
        code, sortie, etat = self.lancer(["nimporte"])
        self.assertEqual(code, 2)
        self.assertIn("Module inconnu", sortie)
        self.assertIn("[2]", sortie)
        self.assertNotIn("ignored_modules", etat)

    def test_lever_la_sourdine(self):
        self.lancer(["2"])
        _, _, etat = self.lancer(lever=["2"])
        self.assertEqual(etat["ignored_modules"], [])

    def test_lever_la_sourdine_d_une_cle_dont_le_module_a_disparu(self):
        etat = md.load_json(self.etat, {})
        etat["ignored_modules"] = ["99"]
        md.save_json(self.etat, etat)
        _, _, etat = self.lancer(lever=["99"])
        self.assertEqual(etat["ignored_modules"], [])

    def test_ajouter_deux_fois_ne_double_pas(self):
        self.lancer(["2"])
        _, _, etat = self.lancer(["2"])
        self.assertEqual(etat["ignored_modules"], ["2"])


class MessagesEtAide(unittest.TestCase):
    def test_les_deux_langues_ont_les_memes_cles(self):
        self.assertEqual(set(watch.MESSAGES["fr"]), set(watch.MESSAGES["en"]))

    def test_les_nouvelles_options_existent(self):
        with mock.patch("sys.argv", ["watch", "--ignore-module", "2", "--unignore-module", "3"]):
            args = watch.parse_args()
        self.assertEqual((args.ignore_module, args.unignore_module), (["2"], ["3"]))


if __name__ == "__main__":
    unittest.main()
