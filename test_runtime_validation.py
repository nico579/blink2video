"""Non-régression des validateurs d'arguments partagés."""

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
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

        with mock.patch("time.sleep") as sommeil, \
             contextlib.redirect_stdout(io.StringIO()):
            code = runtime.repeter(travail, 5)

        self.assertEqual(code, 0)
        self.assertEqual(len(appels), 3)
        self.assertEqual(sommeil.call_count, 2)

    def test_O05_echeance_calculee_depuis_le_debut_du_tour(self) -> None:
        """Un tour de 15 s à cadence 1 min doit dormir ~45 s, pas 60 s."""
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

        self.assertEqual(durees[0], 45.0)


if __name__ == "__main__":
    unittest.main()
