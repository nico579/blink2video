"""Non-régression de --no-weekly / --no-monthly (AUDIT-2026-08-13.md,
section 28.39) : remplace --no-periods, qui coupait hebdomadaire et
mensuel ensemble sans possibilité de choisir l'un sans l'autre - la
journalière, elle, reste toujours construite, les agrégats en dépendant."""

from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import merge_daily


class TestsFlagsIndependants(unittest.TestCase):
    def test_parse_args_no_weekly_et_no_monthly_existent_separement(self):
        # parse_args() lit sys.argv : substitué le temps de l'appel plutôt
        # que de dupliquer la construction du parser.
        import sys
        ancien_argv = sys.argv
        try:
            sys.argv = ["merge_daily.py", "--no-weekly"]
            args = merge_daily.parse_args()
        finally:
            sys.argv = ancien_argv
        self.assertTrue(args.no_weekly)
        self.assertFalse(args.no_monthly)

    def test_no_periods_n_existe_plus(self):
        import sys
        ancien_argv = sys.argv
        try:
            sys.argv = ["merge_daily.py", "--no-periods"]
            with self.assertRaises(SystemExit):
                merge_daily.parse_args()
        finally:
            sys.argv = ancien_argv


class TestsExecuterRespecteLesDrapeaux(unittest.TestCase):
    """_executer() construit hebdo puis mensuel dans une même boucle : on
    vérifie ici que chacun peut être coupé indépendamment, sans passer par
    un vrai ffmpeg (find_ffmpeg et consorts simulés, aucun clip réel)."""

    def setUp(self) -> None:
        self.temporaire = tempfile.TemporaryDirectory(prefix="blink_periodes_test_")
        self.racine = Path(self.temporaire.name)
        for nom in ("clips", "normalized", "excluded", "daily", "weekly", "monthly"):
            (self.racine / nom).mkdir()

    def tearDown(self) -> None:
        self.temporaire.cleanup()

    def construire_args(self, no_weekly: bool, no_monthly: bool) -> argparse.Namespace:
        return argparse.Namespace(
            input=self.racine / "clips",
            output=self.racine / "daily",
            weekly_output=self.racine / "weekly",
            monthly_output=self.racine / "monthly",
            normalized_output=self.racine / "normalized",
            excluded_output=self.racine / "excluded",
            no_weekly=no_weekly,
            no_monthly=no_monthly,
            no_timestamp=True,
            exclude=[],
            include=[],
            timezone="UTC",
            date=None,
            camera=None,
            force=False,
            font=None,
            preset="veryfast",
            crf=21,
        )

    def executer_avec_simulation(self, args):
        with mock.patch.object(merge_daily, "find_ffmpeg", return_value="ffmpeg"), \
             mock.patch.object(merge_daily, "load_groups", return_value={}), \
             mock.patch.object(merge_daily, "build_periods",
                               return_value=(0, 0, 0)) as periodes_simulees:
            merge_daily._executer(args)
        return periodes_simulees

    def test_les_deux_periodes_construites_par_defaut(self):
        periodes_simulees = self.executer_avec_simulation(
            self.construire_args(no_weekly=False, no_monthly=False))
        appels = [appel.args[4] for appel in periodes_simulees.call_args_list]
        self.assertEqual(appels, ["weekly", "monthly"])

    def test_no_weekly_ne_construit_que_le_mensuel(self):
        periodes_simulees = self.executer_avec_simulation(
            self.construire_args(no_weekly=True, no_monthly=False))
        appels = [appel.args[4] for appel in periodes_simulees.call_args_list]
        self.assertEqual(appels, ["monthly"])

    def test_no_monthly_ne_construit_que_lhebdomadaire(self):
        periodes_simulees = self.executer_avec_simulation(
            self.construire_args(no_weekly=False, no_monthly=True))
        appels = [appel.args[4] for appel in periodes_simulees.call_args_list]
        self.assertEqual(appels, ["weekly"])

    def test_les_deux_drapeaux_ne_construisent_aucune_periode(self):
        periodes_simulees = self.executer_avec_simulation(
            self.construire_args(no_weekly=True, no_monthly=True))
        periodes_simulees.assert_not_called()


if __name__ == "__main__":
    unittest.main()
