"""blink2video sur l'installateur commun (0.23, phase 2 du plan de nico579-commons
PLAN-INSTALLATEUR-COMMUN-2026-10-08.md).

Ce qui est propre à blink2video : l'aiguillage entre l'installateur commun et l'ancienne
permutation (gardée pour une permutation interrompue et pour une installation que le
commun refuse), le second temps détaché, les processus à attendre et à relancer, les
conclusions publiées pour la page, et --self-test-version. Le protocole lui-même
(assistant, échange, retour arrière) est testé dans nico579-commons, avec l'assistant
réel des trois systèmes."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["BLINK_BOOTSTRAP"] = "none"
_TEST_HOME = tempfile.TemporaryDirectory(prefix="blink-maj-commun-")
os.environ["BLINK_HOME"] = _TEST_HOME.name

import maj  # noqa: E402
import runtime  # noqa: E402

RACINE = Path(__file__).resolve().parent


class Aiguillage(unittest.TestCase):
    def installer(self, *, interrompue=False, possible=(True, "")):
        with mock.patch.object(runtime, "build_windows7", return_value=False), \
                mock.patch.object(runtime, "frozen", return_value=True), \
                mock.patch.object(maj, "_nettoyer", return_value=interrompue), \
                mock.patch.object(maj, "_possible_commun", return_value=possible) as possible_, \
                mock.patch.object(maj, "_lancer_installateur_commun", return_value=0) as commun, \
                mock.patch.object(maj, "disponible", return_value={}) as ancien, \
                mock.patch.object(maj, "_conclure_sans_relance"), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as sortie:
            code = maj.installer()
        return code, commun, ancien, possible_, sortie.getvalue()

    def test_par_defaut_l_installateur_commun(self):
        code, commun, ancien, _, _ = self.installer()
        self.assertEqual(code, 0)
        commun.assert_called_once()
        ancien.assert_not_called()

    def test_une_permutation_interrompue_reste_sur_l_ancien_chemin(self):
        code, commun, ancien, possible_, _ = self.installer(interrompue=True)
        commun.assert_not_called()
        possible_.assert_not_called()
        ancien.assert_called_once()           # l'ancien chemin a démarré

    def test_une_installation_refusee_par_le_commun_garde_l_ancien_chemin(self):
        # Dossier parent protégé : la permutation n'a besoin que du dossier d'installation.
        _, commun, ancien, _, sortie = self.installer(possible=(False, "parent_protege"))
        commun.assert_not_called()
        ancien.assert_called_once()
        self.assertIn("parent_protege", sortie)


class ProcessusEtRelance(unittest.TestCase):
    def test_tous_les_processus_des_fiches_sauf_celui_ci(self):
        fiches = [{"pid": 100, "enfants": [101, 102]}, {"pid": 200, "enfants": []},
                  {"pid": os.getpid(), "enfants": [101]}, {"pid": None}]
        with mock.patch.object(runtime, "lire_instances", return_value=fiches):
            self.assertEqual(maj._pids_en_cours(), [100, 101, 102, 200])

    def test_une_ligne_de_commande_par_composition(self):
        compositions = [[["serve", "--port", "8765"], ["watch", "--loop", "60"]], []]
        with mock.patch.object(maj, "_compositions_en_cours", return_value=compositions):
            self.assertEqual(maj._verbes_a_relancer(),
                             [["serve", "--port", "8765", "watch", "--loop", "60"], ["start"]])


class FauxInstallateur:
    def __init__(self, etat):
        self._etat = etat
        self._fil = None
        self.args = None

    def demarrer(self):
        return True

    def etat(self):
        return dict(self._etat)


class SecondTemps(unittest.TestCase):
    def lancer(self, etat):
        faux = FauxInstallateur(etat)

        def fabriquer(*args, **kwargs):
            faux.args = (args, kwargs)
            return faux

        with mock.patch.object(maj.maj_install, "Installateur", side_effect=fabriquer), \
                mock.patch.object(maj, "_verificateur"), \
                mock.patch.object(maj, "_application_commune"), \
                mock.patch.object(maj, "_conclure_sans_relance") as conclure, \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            code = maj._installer_commun()
        return code, conclure, faux

    def test_succes_l_application_s_arrete_sans_conclusion(self):
        code, conclure, faux = self.lancer({"etat": "redemarrage", "erreur": None})
        self.assertEqual(code, 0)
        conclure.assert_not_called()
        kwargs = faux.args[1]
        self.assertIs(kwargs["verbes_relance"], maj._verbes_a_relancer)
        self.assertIs(kwargs["pids_a_attendre"], maj._pids_en_cours)
        self.assertIsNotNone(kwargs["contexte"])        # le contexte TLS strict

    def test_echec_publie_la_raison_pour_la_page(self):
        erreur = {"code": "parent_protege",
                  "message": {"fr": "Dossier protégé", "en": "Protected folder"}}
        with mock.patch.object(maj, "_langue", return_value="en"):
            code, conclure, _ = self.lancer({"etat": "erreur", "erreur": erreur})
        self.assertEqual(code, 1)
        self.assertIn("Protected folder", conclure.call_args.args[0])

    def test_rien_a_installer_dit_deja_a_jour(self):
        code, conclure, _ = self.lancer({"etat": "inactif", "erreur": None})
        self.assertEqual(code, 0)
        self.assertIn(runtime.VERSION, conclure.call_args.args[0])


class Description(unittest.TestCase):
    def test_service_et_agent_sous_lesquels_ce_processus_tourne(self):
        import autostart
        with mock.patch.object(autostart, "unite_systemd", return_value="blink2video-start.service"), \
                mock.patch.object(sys, "platform", "linux"), \
                mock.patch.dict(os.environ, {"XPC_SERVICE_NAME": "com.autre.app"}):
            app = maj._application_commune()
        self.assertEqual(app.unite_systemd, "blink2video-start.service")
        self.assertEqual(app.label_launchd, "")              # pas le nôtre
        with mock.patch.object(autostart, "unite_systemd", return_value=""), \
                mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.dict(os.environ, {"XPC_SERVICE_NAME": "com.nico579.blink2video-start"}):
            app = maj._application_commune()
        self.assertEqual(app.label_launchd, "com.nico579.blink2video-start")
        self.assertEqual(app.unite_systemd, "")
        self.assertIn(maj.DOSSIER_TRAVAIL, app.noms_toleres)  # restes de l'ancien mécanisme

    def test_la_racine_des_archives_s_appelle_blink2video(self):
        with mock.patch.object(maj.maj_install, "disposition", return_value="d") as disposition:
            self.assertEqual(maj._disposition_commune(maj.maj_install.Application("blink2video")), "d")
        kwargs = disposition.call_args.kwargs
        self.assertEqual(kwargs["racine_attendue"], "blink2video")
        self.assertTrue(kwargs["asset_name"].startswith("blink2video-"))
        self.assertNotIn("windows7", kwargs["asset_name"])


class AutoTest(unittest.TestCase):
    """--self-test-version, lancé pour de vrai : avant tout dossier d'état."""

    def lancer(self, *arguments):
        env = dict(os.environ, BLINK_HOME=tempfile.mkdtemp(prefix="blink-auto-test-"))
        return subprocess.run([sys.executable, str(RACINE / "blink2video.py"), *arguments],
                              cwd=RACINE, env=env, capture_output=True, text=True, timeout=60)

    def test_reconnait_sa_version_avec_ou_sans_v(self):
        self.assertEqual(self.lancer("--self-test-version", runtime.VERSION).returncode, 0)
        self.assertEqual(self.lancer("--self-test-version", "v" + runtime.VERSION).returncode, 0)

    def test_refuse_une_autre_version_ou_rien(self):
        self.assertEqual(self.lancer("--self-test-version", "0.0.1").returncode, 1)
        self.assertEqual(self.lancer("--self-test-version").returncode, 1)

    def test_ne_touche_a_aucun_dossier_d_etat(self):
        home = Path(tempfile.mkdtemp(prefix="blink-auto-test-vide-"))
        env = dict(os.environ, BLINK_HOME=str(home))
        subprocess.run([sys.executable, str(RACINE / "blink2video.py"), "--self-test-version",
                        runtime.VERSION], cwd=RACINE, env=env, capture_output=True, timeout=60)
        self.assertEqual(list(home.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
