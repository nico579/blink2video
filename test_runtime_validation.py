"""Non-régression des validateurs d'arguments partagés."""

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


_TEST_HOME = tempfile.TemporaryDirectory(prefix="blink-validation-")
os.environ["BLINK_BOOTSTRAP"] = "none"
os.environ["BLINK_HOME"] = _TEST_HOME.name

import runtime  # noqa: E402


def analyser(parse_args, programme: str, *arguments: str):
    """Appelle un parseur de module sans toucher aux arguments du test runner."""
    with mock.patch.object(sys, "argv", [programme, *arguments]):
        return parse_args()


class ValidationBoucleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = argparse.ArgumentParser(prog="test-loop")
        runtime.ajouter_boucle(self.parser)

    def test_absence_et_valeur_implicite_conservees(self) -> None:
        self.assertIsNone(self.parser.parse_args([]).loop)
        self.assertEqual(self.parser.parse_args(["--loop"]).loop, 10)

    def test_cadence_positive_acceptee(self) -> None:
        self.assertEqual(self.parser.parse_args(["--loop", "1"]).loop, 1)

    def test_zero_negatif_et_texte_refuses_proprement(self) -> None:
        for valeur in ("0", "-1", "texte"):
            with self.subTest(valeur=valeur):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as erreur:
                        self.parser.parse_args(["--loop", valeur])
                self.assertEqual(erreur.exception.code, 2)


class ValidationPortTests(unittest.TestCase):
    def test_bornes_valides_acceptees(self) -> None:
        self.assertEqual(runtime.port_valide("1"), 1)
        self.assertEqual(runtime.port_valide("65535"), 65535)

    def test_ports_hors_plage_et_texte_refuses(self) -> None:
        for valeur in ("0", "-1", "65536", "texte"):
            with self.subTest(valeur=valeur):
                with self.assertRaises(argparse.ArgumentTypeError):
                    runtime.port_valide(valeur)

    def test_serve_utilise_le_validateur_partage(self) -> None:
        import serve

        self.assertEqual(analyser(serve.parse_args, "serve", "--port", "1").port, 1)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as erreur:
                analyser(serve.parse_args, "serve", "--port", "65536")
        self.assertEqual(erreur.exception.code, 2)

    def test_watch_utilise_le_validateur_partage(self) -> None:
        import watch

        self.assertEqual(
            analyser(watch.parse_args, "watch", "--port", "65535").port,
            65535,
        )
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as erreur:
                analyser(watch.parse_args, "watch", "--port", "0")
        self.assertEqual(erreur.exception.code, 2)


class ValidationJoursTests(unittest.TestCase):
    def test_zero_et_valeur_positive_sont_acceptes(self) -> None:
        self.assertEqual(runtime.jours_non_negatifs("0"), 0)
        self.assertEqual(runtime.jours_non_negatifs("30"), 30)

    def test_valeur_negative_et_texte_sont_refuses(self) -> None:
        for valeur in ("-1", "texte"):
            with self.subTest(valeur=valeur):
                with self.assertRaises(argparse.ArgumentTypeError):
                    runtime.jours_non_negatifs(valeur)


class ProcessusSansPsTests(unittest.TestCase):
    """AUDIT-2026-08-13, 28.85 : `ps` absent (python:3.12-slim, sans procps)
    plantait toute l'appli des le premier "start" en conteneur - constate en
    reel sur l'image Docker Hub. Reproduit ici l'environnement POSIX sans
    dependre d'une vraie machine sans `ps`."""

    def test_identite_processus_sans_ps_degrade_sans_planter(self) -> None:
        with mock.patch.object(runtime.os, "name", "posix"), \
             mock.patch.object(runtime, "lancer", side_effect=FileNotFoundError(2, "No such file or directory", "ps")):
            self.assertIsNone(runtime.identite_processus(os.getpid()))

    def test_processus_vivant_sans_ps_reste_vivant(self) -> None:
        with mock.patch.object(runtime.os, "name", "posix"), \
             mock.patch.object(runtime.os, "kill", return_value=None), \
             mock.patch.object(runtime, "lancer", side_effect=FileNotFoundError(2, "No such file or directory", "ps")):
            self.assertTrue(runtime.processus_vivant(os.getpid()))


class ProcessusVivantWindowsTests(unittest.TestCase):
    """Revue du 27/08 : sous Windows, processus_vivant() interroge l'OS
    directement via OpenProcess/GetExitCodeProcess (API Win32), sans passer
    par un sous-processus tasklist - à la fois pour la sûreté (bug 1 : un
    échec d'ouverture ne prouve rien sur le pid, sauf s'il est absent) et
    pour la vitesse/déterminisme (constaté en réel : arreter() vérifie
    plusieurs membres chaque seconde pendant l'attente, un tasklist par
    vérification rend le délai dépendant de la charge machine)."""

    @staticmethod
    def _get_exit_code(code: int):
        def cb(handle, ref):
            ref._obj.value = code
            return 1
        return cb

    def test_pid_vivant_est_detecte(self) -> None:
        with mock.patch.object(runtime.os, "name", "nt"), \
             mock.patch("ctypes.windll.kernel32.OpenProcess", return_value=12345), \
             mock.patch("ctypes.windll.kernel32.GetExitCodeProcess",
                         side_effect=self._get_exit_code(259)), \
             mock.patch("ctypes.windll.kernel32.CloseHandle", return_value=1):
            self.assertTrue(runtime.processus_vivant(os.getpid()))

    def test_pid_termine_est_bien_mort(self) -> None:
        with mock.patch.object(runtime.os, "name", "nt"), \
             mock.patch("ctypes.windll.kernel32.OpenProcess", return_value=12345), \
             mock.patch("ctypes.windll.kernel32.GetExitCodeProcess",
                         side_effect=self._get_exit_code(0)), \
             mock.patch("ctypes.windll.kernel32.CloseHandle", return_value=1):
            self.assertFalse(runtime.processus_vivant(999_999_999))

    def test_ouverture_refusee_reste_vivant(self) -> None:
        with mock.patch.object(runtime.os, "name", "nt"), \
             mock.patch("ctypes.windll.kernel32.OpenProcess", return_value=0), \
             mock.patch("ctypes.windll.kernel32.GetLastError", return_value=5):  # ERROR_ACCESS_DENIED
            self.assertTrue(runtime.processus_vivant(os.getpid()))

    def test_ouverture_echouee_pid_absent_est_bien_mort(self) -> None:
        with mock.patch.object(runtime.os, "name", "nt"), \
             mock.patch("ctypes.windll.kernel32.OpenProcess", return_value=0), \
             mock.patch("ctypes.windll.kernel32.GetLastError", return_value=87):  # ERROR_INVALID_PARAMETER
            self.assertFalse(runtime.processus_vivant(999_999_999))

    def test_get_exit_code_en_echec_reste_vivant(self) -> None:
        with mock.patch.object(runtime.os, "name", "nt"), \
             mock.patch("ctypes.windll.kernel32.OpenProcess", return_value=12345), \
             mock.patch("ctypes.windll.kernel32.GetExitCodeProcess", return_value=0), \
             mock.patch("ctypes.windll.kernel32.CloseHandle", return_value=1):
            self.assertTrue(runtime.processus_vivant(os.getpid()))


class VerrouTests(unittest.TestCase):
    """B-05 : acquisition atomique, jamais de vol d'un propriétaire vivant,
    récupération d'un verrou abandonné par un processus mort."""

    def fichier(self, nom: str) -> "object":
        return runtime.app_dir() / f".blink_{nom}.lock"

    def test_B05_verrou_d_un_processus_mort_est_recupere(self) -> None:
        cible = self.fichier("crash-test")
        cible.write_text(json.dumps(
            {"owner": "victime", "pid": 999_999, "jeton": "perime", "at": 0}
        ), encoding="utf-8")
        with mock.patch.object(runtime, "processus_vivant", return_value=False):
            with runtime.verrou("crash-test", "sauveteur"):
                self.assertTrue(cible.exists())
                contenu = json.loads(cible.read_text(encoding="utf-8"))
                self.assertEqual(contenu["owner"], "sauveteur")
        self.assertFalse(cible.exists())

    def test_verrou_pid_recycle_identite_differente_est_recupere(self) -> None:
        """AUDIT-2026-08-13, 28.82/28.84 : constaté en réel, la boucle merge
        est restée bloquée plus de 15h après un redémarrage Windows. Un pid
        vivant (recyclé par un autre processus après la mort du vrai
        propriétaire) ne doit pas passer pour lui : seule l'identité
        (date de démarrage réelle, jamais recyclée) fait foi."""
        cible = self.fichier("pid-recycle")
        cible.write_text(json.dumps(
            {"owner": "fantome", "pid": os.getpid(), "jeton": "perime",
             "at": time.time(), "identite": "identite-qui-ne-correspond-a-rien"}
        ), encoding="utf-8")
        with runtime.verrou("pid-recycle", "sauveteur"):
            self.assertTrue(cible.exists())
            contenu = json.loads(cible.read_text(encoding="utf-8"))
            self.assertEqual(contenu["owner"], "sauveteur")
        self.assertFalse(cible.exists())

    def test_verrou_meme_identite_reste_protege(self) -> None:
        """Contrepoint du precedent (B-05) : un pid vivant dont l'identite
        correspond vraiment ne doit toujours pas etre vole."""
        cible = self.fichier("meme-identite")
        identite = runtime.identite_processus(os.getpid())
        cible.write_text(json.dumps(
            {"owner": "legitime", "pid": os.getpid(), "jeton": "valide",
             "at": time.time(), "identite": identite}
        ), encoding="utf-8")
        with self.assertRaises(runtime.BusyError):
            with runtime.verrou("meme-identite", "voleur", attente=0):
                pass
        cible.unlink()

    def test_verrou_ancien_format_sans_identite_reste_protege(self) -> None:
        """Une marque ecrite avant ce correctif n'a pas de champ "identite" :
        rien a comparer, donc pas de purge a tort - ancien comportement
        conserve pour ce cas."""
        cible = self.fichier("ancien-format")
        cible.write_text(json.dumps(
            {"owner": "legitime", "pid": os.getpid(), "jeton": "valide",
             "at": time.time()}
        ), encoding="utf-8")
        with self.assertRaises(runtime.BusyError):
            with runtime.verrou("ancien-format", "voleur", attente=0):
                pass
        cible.unlink()

    def test_verrou_identite_actuelle_inconnue_reste_protege(self) -> None:
        """Revue du 27/08, bug 1 : si identite_processus() échoue à
        interroger le pid actuel (OpenProcess refusé, par exemple), un None
        ne doit pas valoir "identité différente" - seulement "on ne sait
        pas", donc pas de purge à tort d'un propriétaire pourtant vivant."""
        cible = self.fichier("identite-inconnue")
        cible.write_text(json.dumps(
            {"owner": "legitime", "pid": os.getpid(), "jeton": "valide",
             "at": time.time(), "identite": "identite-enregistree-a-la-creation"}
        ), encoding="utf-8")
        with mock.patch.object(runtime, "identite_processus", return_value=None):
            with self.assertRaises(runtime.BusyError):
                with runtime.verrou("identite-inconnue", "voleur", attente=0):
                    pass
        cible.unlink()

    def test_verrou_corrompu_leve_busyerror_au_lieu_de_boucler(self) -> None:
        """Bug #3, revue de code du 0eab463 : un fichier de verrou présent
        mais illisible (JSON corrompu) bouclait indéfiniment en ignorant
        `attente`, jamais de BusyError, jamais de main rendue. Lancé sur un
        thread à part avec join(timeout) : si le correctif régresse, ce test
        échoue par timeout plutôt que de pendre toute la suite."""
        cible = self.fichier("corrompu")
        cible.write_text("pas du json valide", encoding="utf-8")
        resultat = {}

        def tenter():
            debut = time.monotonic()
            try:
                with runtime.verrou("corrompu", "moi", attente=0.2):
                    pass
            except runtime.BusyError:
                resultat["busy"] = True
            except Exception as erreur:  # pragma: no cover - diagnostic seulement
                resultat["erreur"] = erreur
            resultat["duree"] = time.monotonic() - debut

        fil = threading.Thread(target=tenter, daemon=True)
        fil.start()
        fil.join(timeout=5)
        self.assertFalse(fil.is_alive(), "verrou() sur fichier corrompu ne rend jamais la main")
        self.assertTrue(resultat.get("busy"), resultat.get("erreur"))
        self.assertLess(resultat["duree"], 3)
        cible.unlink(missing_ok=True)

    def test_verrou_double_purge_ne_supprime_pas_un_verrou_frais(self) -> None:
        """Bug #3, revue de code du 0eab463 : un second processus qui conclut
        aussi « propriétaire mort » ne doit pas supprimer, entre-temps, le
        verrou qu'un premier vient de recréer sous un jeton différent - il
        doit constater le nouveau jeton et renoncer, pas le voler."""
        cible = self.fichier("double-purge")
        perime = {"owner": "victime", "pid": 999_999, "jeton": "perime", "at": 0}
        frais = {"owner": "rescape", "pid": os.getpid(), "jeton": "frais", "at": time.time()}
        cible.write_text(json.dumps(perime), encoding="utf-8")

        lectures = [perime, frais, frais, frais, frais]

        def vivant(pid):
            return pid == os.getpid()

        with mock.patch.object(runtime, "_lire_verrou", side_effect=lectures), \
             mock.patch.object(runtime, "processus_vivant", side_effect=vivant):
            with self.assertRaises(runtime.BusyError):
                with runtime.verrou("double-purge", "voleur", attente=0):
                    pass
        # Le fichier réel n'a jamais bougé : la lecture simulée « frais » ne
        # provient que du mock, mais la garantie testée est que rien n'a
        # tenté de le supprimer entre les deux lectures.
        self.assertTrue(cible.exists())
        cible.unlink()

    def test_B05_liberation_ne_retire_que_son_propre_jeton(self) -> None:
        cible = self.fichier("jeton-etranger")
        with runtime.verrou("jeton-etranger", "moi"):
            # Un autre propriétaire a repris ce fichier entre-temps (cas
            # limite) : notre sortie ne doit pas effacer sa marque.
            cible.write_text(json.dumps(
                {"owner": "autrui", "pid": os.getpid(), "jeton": "pas-le-mien"}
            ), encoding="utf-8")
        self.assertTrue(cible.exists())
        self.assertEqual(
            json.loads(cible.read_text(encoding="utf-8"))["owner"], "autrui")
        cible.unlink()

    def test_B05_relache_normalement_a_la_sortie(self) -> None:
        cible = self.fichier("cycle-normal")
        with runtime.verrou("cycle-normal", "moi"):
            self.assertTrue(cible.exists())
        self.assertFalse(cible.exists())


class RepeterTests(unittest.TestCase):
    """I-17 et O-05 : une erreur de tour ne doit pas tuer la boucle, et la
    prochaine échéance doit se calculer depuis le début du tour courant."""

    def test_I17_une_erreur_de_tour_n_arrete_pas_la_repetition(self) -> None:
        appels = []

        def travail():
            appels.append(len(appels))
            if len(appels) == 1:
                raise RuntimeError("panne transitoire")
            if len(appels) >= 3:
                raise KeyboardInterrupt
            return 0

        # Horloge et sommeil liés : depuis l'arrêt coopératif (revue du
        # 27/08), le sommeil entre deux tours est scindé en tranches d'une
        # seconde qui relisent time.monotonic() à chaque tranche - un
        # sommeil muet avec une horloge réelle non mockée boucle alors à
        # vide pendant toute la période réelle (~5 min ici) avant de s'en
        # apercevoir. Ce que ce test vérifie (une erreur n'arrête pas la
        # répétition) ne dépend pas du nombre exact de tranches.
        horloge = {"t": 0.0}

        def faux_sommeil(duree):
            horloge["t"] += duree

        with mock.patch("time.monotonic", side_effect=lambda: horloge["t"]), \
             mock.patch("time.sleep", side_effect=faux_sommeil) as sommeil, \
             contextlib.redirect_stdout(io.StringIO()):
            code = runtime.repeter(travail, 5)

        self.assertEqual(code, 0)
        self.assertEqual(len(appels), 3)
        self.assertGreater(sommeil.call_count, 0)

    def test_O05_echeance_calculee_depuis_le_debut_du_tour(self) -> None:
        """Un tour de 15 s à cadence 1 min doit dormir ~45 s au total, pas 60 s."""
        horloge = {"t": 1_000.0}

        def maintenant():
            return horloge["t"]

        appels = []

        def travail():
            appels.append(None)
            horloge["t"] += 15.0  # le tour "dure" 15 s
            if len(appels) >= 2:
                raise KeyboardInterrupt

        durees = []

        def faux_sommeil(duree):
            durees.append(duree)
            horloge["t"] += duree

        with mock.patch("time.monotonic", side_effect=maintenant), \
             mock.patch("time.sleep", side_effect=faux_sommeil), \
             contextlib.redirect_stdout(io.StringIO()):
            runtime.repeter(travail, 1)

        # Le sommeil est désormais scindé en tranches courtes (arrêt
        # coopératif, revue du 27/08) plutôt qu'un seul gros sommeil : c'est
        # leur somme qui doit valoir 45 s, pas dépasser jusqu'à 60 s.
        self.assertEqual(sum(durees), 45.0)


class ExtraireModeBootstrapTests(unittest.TestCase):
    """Revue du 27/08, bug 4 : reproductible avec "download --bootstrap=
    none", dont les arguments n'étaient nettoyés par personne avant
    d'atteindre argparse (aucun parseur de verbe ne déclare --bootstrap)."""

    def setUp(self) -> None:
        self.ancien = os.environ.pop("BLINK_BOOTSTRAP", None)

    def tearDown(self) -> None:
        if self.ancien is None:
            os.environ.pop("BLINK_BOOTSTRAP", None)
        else:
            os.environ["BLINK_BOOTSTRAP"] = self.ancien

    def test_retire_loption_et_pose_la_variable(self) -> None:
        reste = runtime.extraire_mode_bootstrap(
            ["download", "--bootstrap=none", "--from", "usb"])
        self.assertEqual(reste, ["download", "--from", "usb"])
        self.assertEqual(os.environ.get("BLINK_BOOTSTRAP"), "none")

    def test_argv_sans_loption_reste_inchange(self) -> None:
        argv = ["download", "--from", "usb"]
        self.assertEqual(runtime.extraire_mode_bootstrap(argv), argv)
        self.assertNotIn("BLINK_BOOTSTRAP", os.environ)


class VenvAJourTests(unittest.TestCase):
    """Revue du 27/08, bug 4 : un venv déjà créé mais incomplet (dépendance
    ajoutée depuis, install précédente interrompue) n'était jamais réparé,
    _installer() n'étant appelé qu'à la création du venv."""

    def test_venv_complet_est_reconnu(self) -> None:
        with mock.patch.object(runtime.subprocess, "run",
                               return_value=mock.Mock(returncode=0)) as appel:
            self.assertTrue(runtime._venv_a_jour("un/faux/python.exe"))
        commande = appel.call_args[0][0]
        for module in runtime.DEPENDANCES:
            self.assertIn(module, commande[2])

    def test_venv_incomplet_est_detecte(self) -> None:
        with mock.patch.object(runtime.subprocess, "run",
                               return_value=mock.Mock(returncode=1)):
            self.assertFalse(runtime._venv_a_jour("un/faux/python.exe"))


class DependancesTests(unittest.TestCase):
    def test_imageio_ffmpeg_fait_partie_des_dependances(self) -> None:
        """find_ffmpeg() (merge_daily.py) en dépend par défaut : absent du
        venv auto-créé, un compte sans ffmpeg système échouait en silence
        jusqu'au premier assemblage."""
        self.assertIn("imageio_ffmpeg", runtime.DEPENDANCES)
        self.assertEqual(runtime.DEPENDANCES["imageio_ffmpeg"], "imageio-ffmpeg")


if __name__ == "__main__":
    unittest.main()
