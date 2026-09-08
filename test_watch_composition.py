"""Le verbe watch reste un contrôleur, pas un lanceur de serveur ou de fusion."""

import contextlib
import datetime as dt
import os
import sys
import tempfile
import unittest
from unittest import mock


_IMPORT_HOME = tempfile.TemporaryDirectory(prefix="blink-watch-composition-")
with mock.patch.dict(os.environ, {"BLINK_BOOTSTRAP": "none",
                                  "BLINK_HOME": _IMPORT_HOME.name}):
    import watch


class TestsCompositionWatch(unittest.TestCase):
    def setUp(self):
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        self.interdits = []
        for module, nom in ((watch.runtime, "demarrer"),
                            (watch.runtime, "lancer"),
                            (watch.runtime, "marquer"),
                            (watch.md, "save_json"),
                            (watch, "read_state"),
                            (watch, "popup"),
                            (watch, "toast")):
            espion = self.patches.enter_context(mock.patch.object(
                module, nom, side_effect=AssertionError("Appel interdit : " + nom)))
            self.interdits.append(espion)

    def tearDown(self):
        for espion in self.interdits:
            espion.assert_not_called()

    def test_options_historiques_restent_acceptees(self):
        arguments = ["watch.py", "--timezone", "UTC", "--loop", "2",
                     "--port", "8899", "--dry-run", "--test",
                     "--ignore", "Salon", "Terrasse", "--unignore", "Jardin"]
        with mock.patch.object(sys, "argv", arguments):
            args = watch.parse_args()
        self.assertEqual(args.timezone, "UTC")
        self.assertEqual(args.loop, 2)
        self.assertEqual(args.port, 8899)
        self.assertTrue(args.dry_run)
        self.assertTrue(args.test)
        self.assertEqual(args.ignore, ["Salon", "Terrasse"])
        self.assertEqual(args.unignore, ["Jardin"])

    def test_main_delegue_uniquement_le_controle_a_la_boucle_commune(self):
        for options, cadence in (([], None), (["--loop", "2"], 2)):
            with self.subTest(options=options), \
                    mock.patch.object(sys, "argv", ["watch.py", "--timezone", "UTC",
                                                  "--port", "8899", *options]), \
                    mock.patch.object(watch, "un_tour") as tour, \
                    mock.patch.object(watch.runtime, "repeter", return_value=17) as repeter:
                self.assertEqual(watch.main(), 17)
                tour.assert_not_called()
                repeter.assert_called_once()
                travail, minutes, journal = repeter.call_args[0]
                self.assertEqual(minutes, cadence)
                self.assertIs(journal, watch.journal)
                travail()
                tour.assert_called_once()
                args, fuseau = tour.call_args[0]
                self.assertEqual(args.port, 8899)
                self.assertEqual(args.loop, cadence)
                self.assertEqual(fuseau.key, "UTC")

    def test_mode_test_notifie_sans_controle_ni_boucle(self):
        with mock.patch.object(sys, "argv", ["watch.py", "--test", "--timezone", "UTC"]), \
                mock.patch.object(watch, "popup") as popup, \
                mock.patch.object(watch, "un_tour") as tour, \
                mock.patch.object(watch.runtime, "repeter") as repeter:
            self.assertEqual(watch.main(), 0)
        popup.assert_called_once()
        tour.assert_not_called()
        repeter.assert_not_called()

    def test_un_tour_marque_le_passage_apres_le_controle(self):
        ordre = mock.Mock()
        args = object()
        with mock.patch.object(watch, "_controler", ordre.controler), \
                mock.patch.object(watch.runtime, "marquer", ordre.marquer):
            watch.un_tour(args, dt.timezone.utc)
        self.assertEqual(ordre.mock_calls, [mock.call.controler(args, dt.timezone.utc),
                                           mock.call.marquer("watch")])

    def test_un_tour_ne_marque_pas_un_controle_interrompu(self):
        with mock.patch.object(watch, "_controler", side_effect=RuntimeError("arrêt")), \
                mock.patch.object(watch.runtime, "marquer") as marquer:
            with self.assertRaisesRegex(RuntimeError, "arrêt"):
                watch.un_tour(object(), dt.timezone.utc)
        marquer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
