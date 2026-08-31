"""Garde-fous de l'édition Windows 7, sans réseau ni construction du bundle."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import build
import build_blinkpy_win7
import build_xr_tester
import maj
import runtime


class Windows7BuildTests(unittest.TestCase):
    def test_build_profiles_are_fully_isolated(self):
        normal = set(build._chemins(False))
        legacy = set(build._chemins(True))
        self.assertTrue(normal.isdisjoint(legacy))
        self.assertEqual(build.WIN7_PYTHON, (3, 8, 10))
        self.assertEqual(build_blinkpy_win7.WIN7_VERSION, "0.25.9+win7.1")

    def test_certifi_is_a_direct_common_dependency(self):
        # requirements.in porte l'intention (paquets non figés) ; .txt est
        # généré par pip-compile (versions et empreintes, jamais à la main).
        requirements = {
            ligne.strip()
            for ligne in (Path(__file__).parent / "requirements.in")
            .read_text(encoding="utf-8")
            .splitlines()
            if ligne.strip() and not ligne.lstrip().startswith("#")
        }
        self.assertIn("certifi", requirements)
        verrouille = build.REQUIREMENTS.read_text(encoding="utf-8")
        self.assertIn("certifi==", verrouille)
        self.assertEqual(runtime.DEPENDANCES.get("certifi"), "certifi")

    def test_workflows_build_both_profiles_from_main_only(self):
        workflows = Path(__file__).parent / ".github" / "workflows"
        win7 = (workflows / "build-win7.yml").read_text(encoding="utf-8")
        release = (workflows / "release.yml").read_text(encoding="utf-8")

        self.assertIn("  pull_request:\n    branches:\n      - main", win7)
        self.assertIn("  push:\n    branches:\n      - main", win7)
        self.assertNotIn("      - windows7-experimental", win7)
        self.assertIn("      - 'v[0-9]+.[0-9]+.[0-9]+'", release)
        self.assertNotIn("      - 'v*'", release)
        # 3 checkouts directs (version, build, docker) + 1 fois comme entree
        # "ref" du job reutilisable build-win7 (meme etiquette de release,
        # jamais la HEAD de main que ce job construirait par defaut).
        self.assertEqual(
            release.count("ref: ${{ github.event.inputs.tag || github.ref }}"),
            4,
        )
        # build-win7.yml sert a la fois le CI continu sur main (push/pull_
        # request ci-dessus) et la release, via le meme job plutot qu'une
        # copie : sans workflow_call, une correction du build Win7 devrait
        # etre faite deux fois pour rester valable aux deux endroits.
        self.assertIn("workflow_call:", win7)
        self.assertIn("uses: ./.github/workflows/build-win7.yml", release)
        self.assertIn("needs: [build, build-win7]", release)

    def test_ci_execute_tous_les_modules_unittest(self):
        workflows = Path(__file__).parent / ".github" / "workflows"
        ci = (workflows / "ci.yml").read_text(encoding="utf-8")
        win7 = (workflows / "build-win7.yml").read_text(encoding="utf-8")
        commande = "python -B -m unittest discover -v"
        self.assertIn(commande, ci)
        self.assertIn(commande, win7.replace(
            ".\\build_venv_win7\\Scripts\\python.exe", "python"
        ))

    def test_publication_stable_survit_a_un_echec_win7(self):
        release = (
            Path(__file__).parent / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "if: ${{ always() && needs.build.result == 'success' }}",
            release,
        )

    def test_testeur_xr_prepare_sa_version_depuis_un_checkout_propre(self):
        with tempfile.TemporaryDirectory() as dossier, mock.patch.object(
            build_xr_tester, "BASE_DIR", Path(dossier)
        ):
            version_info = build_xr_tester.preparer_version_info()

            self.assertEqual(version_info, Path(dossier) / ".version_info.txt")
            contenu = version_info.read_text(encoding="utf-8")
            self.assertIn(f"FileVersion', u'{runtime.VERSION}'", contenu)
            self.assertIn("OriginalFilename', u'Tester-XR.exe'", contenu)

    def test_win7_build_rejects_another_platform(self):
        with mock.patch.object(build.sys, "platform", "linux"):
            with self.assertRaises(SystemExit) as erreur:
                build.verifier_interpreteur_win7()
        self.assertIn("Windows", str(erreur.exception))

    def test_win7_build_rejects_an_existing_venv_from_another_python(self):
        resultat = mock.Mock(
            returncode=0,
            stdout="3.8.20|64|cpython\n",
            stderr="",
        )
        with mock.patch.object(build.subprocess, "run", return_value=resultat):
            with self.assertRaises(SystemExit) as erreur:
                build.verifier_python_win7(Path("python.exe"))
        self.assertIn("3.8.20|64|cpython", str(erreur.exception))
        self.assertIn("--propre", str(erreur.exception))

    def test_marker_identifies_only_frozen_bundle(self):
        with tempfile.TemporaryDirectory() as dossier:
            racine = Path(dossier)
            (racine / runtime.WINDOWS7_BUILD_MARKER).write_text("win7")
            with mock.patch.object(runtime, "resource_dir", return_value=racine):
                with mock.patch.object(runtime, "frozen", return_value=False):
                    self.assertFalse(runtime.build_windows7())
                with mock.patch.object(runtime, "frozen", return_value=True):
                    self.assertTrue(runtime.build_windows7())
                    self.assertIn("Windows 7", runtime.version_affichee())

    def test_update_is_disabled_without_network(self):
        with mock.patch.object(runtime, "build_windows7", return_value=True), \
                mock.patch.object(maj, "_interroger") as interroger:
            self.assertEqual(maj.disponible(force=True), {})
            with redirect_stdout(StringIO()) as sortie:
                self.assertEqual(maj.installer(force=True), 0)
            self.assertIn("désactivée", sortie.getvalue())
            interroger.assert_not_called()

    def test_normal_windows_never_selects_legacy_asset(self):
        assets = [
            {"name": "blink2video-windows7-x86_64-experimental.zip",
             "browser_download_url": "legacy", "size": 1},
            {"name": "blink2video-windows-x86_64.zip",
             "browser_download_url": "normal", "size": 2},
        ]
        with mock.patch.object(maj.sys, "platform", "win32"), \
                mock.patch.object(maj.platform, "machine", return_value="AMD64"):
            choisie = maj._archive_de_ce_systeme(assets)
        self.assertEqual(choisie.get("url"), "normal")


if __name__ == "__main__":
    unittest.main()
