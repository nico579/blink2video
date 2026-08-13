"""Non-régression des validateurs d'arguments partagés."""

import argparse
import contextlib
import io
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


if __name__ == "__main__":
    unittest.main()
