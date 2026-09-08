"""Parcours du déploiement, sans dépôt Git ni publication réels."""

from __future__ import annotations

import contextlib
import unittest
from unittest import mock

import deploy


class ParcoursDeploiementTests(unittest.TestCase):
    def setUp(self):
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(mock.patch.object(
            deploy.subprocess, "run",
            side_effect=AssertionError("aucun sous-processus réel dans ce test")))
        self.patches.enter_context(mock.patch.object(deploy, "cprint"))
        for nom, retour in (
            ("read_code_version", "1.2.3"),
            ("verifier_depot", "a" * 40),
            ("preflight", None),
            ("compute_diff", []),
            ("commit_and_push", "b" * 40),
            ("_publier_tag", None),
            ("watch_release", None),
        ):
            doublure = self.patches.enter_context(
                mock.patch.object(deploy, nom, return_value=retour))
            setattr(self, nom, doublure)

    def _executer(self, *options):
        with mock.patch.object(deploy.sys, "argv", ["deploy.py", "-m", "message", *options]):
            self.assertEqual(deploy.main(), 0)

    def _verifier_simulation(self, tag):
        self.verifier_depot.assert_called_once_with(tag)
        self.compute_diff.assert_called_once_with(True)
        self.preflight.assert_not_called()
        self.commit_and_push.assert_not_called()
        self._publier_tag.assert_not_called()
        self.watch_release.assert_not_called()

    def test_dry_run_sans_changement_et_sans_tag_ne_publie_rien(self):
        self._executer("--dry-run")

        self._verifier_simulation("")

    def test_dry_run_sans_changement_et_avec_tag_ne_publie_rien(self):
        self._executer("--dry-run", "--new-tag")

        self._verifier_simulation("v1.2.3")

    def test_dry_run_avec_changements_et_sans_tag_ne_publie_rien(self):
        self.compute_diff.return_value = [" M runtime.py"]

        self._executer("--dry-run")

        self._verifier_simulation("")

    def test_dry_run_avec_changements_et_tag_explicite_ne_publie_rien(self):
        self.compute_diff.return_value = [" M runtime.py"]

        self._executer("--dry-run", "--new-tag", "v1.2.3")

        self._verifier_simulation("v1.2.3")

    def test_sans_changement_et_sans_tag_ne_publie_rien(self):
        self._executer()

        self.preflight.assert_called_once_with()
        self.compute_diff.assert_called_once_with(False)
        self.commit_and_push.assert_not_called()
        self._publier_tag.assert_not_called()
        self.watch_release.assert_not_called()

    def test_sans_changement_et_avec_tag_publie_le_head_initial(self):
        self._executer("--new-tag")

        self.preflight.assert_called_once_with()
        self.compute_diff.assert_called_once_with(False)
        self.commit_and_push.assert_not_called()
        self._publier_tag.assert_called_once_with("v1.2.3", "a" * 40)
        self.watch_release.assert_called_once_with("v1.2.3", "a" * 40)

    def test_avec_changements_et_sans_tag_commit_et_push(self):
        self.compute_diff.return_value = ["runtime.py"]

        self._executer()

        self.preflight.assert_called_once_with()
        self.compute_diff.assert_called_once_with(False)
        self.commit_and_push.assert_called_once_with("message", "")
        self._publier_tag.assert_not_called()
        self.watch_release.assert_not_called()

    def test_avec_changements_et_tag_suit_la_release_du_nouveau_commit(self):
        self.compute_diff.return_value = ["runtime.py"]

        self._executer("--new-tag")

        self.preflight.assert_called_once_with()
        self.compute_diff.assert_called_once_with(False)
        self.commit_and_push.assert_called_once_with("message", "v1.2.3")
        self._publier_tag.assert_not_called()
        self.watch_release.assert_called_once_with("v1.2.3", "b" * 40)


if __name__ == "__main__":
    unittest.main()
