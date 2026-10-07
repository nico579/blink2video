"""nico579-commons est une dépendance d'exécution de blink2video : la sortie du
service systemd (autostart.sortir_du_service) emploie sa relance, née de ce
fichier (issue #35). Sans l'extra « tray », elle n'exige que la bibliothèque
standard : l'icône (pystray, Pillow) reste facultative depuis les sources et
absente de l'image Docker. Ces tests tiennent d'accord les endroits où la
bibliothèque est déclarée, et la façon dont autostart s'en sert.
"""

from __future__ import annotations

import contextlib
import io
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

import autostart
import runtime

RACINE = Path(__file__).parent
UNITE = "blink2video-start.service"
ENV = {"XDG_RUNTIME_DIR": "/run/user/1000", "PATH": "/usr/bin"}


def lignes_utiles(nom: str) -> list:
    return [ligne.strip()
            for ligne in (RACINE / nom).read_text(encoding="utf-8").splitlines()
            if ligne.strip() and not ligne.lstrip().startswith("#")]


class TestsDeclarationDeLaBibliotheque(unittest.TestCase):
    def test_le_demarrage_lit_requirements_in_et_le_verrou(self):
        # Une seule liste : celle de requirements.in, plus de seconde dans runtime.
        self.assertTrue(any(ligne.startswith("nico579-commons>=")
                            for ligne in lignes_utiles("requirements.in")))
        moteur = runtime.amorcage()
        self.assertEqual(moteur.fichier_dependances, RACINE / "requirements.in")
        self.assertEqual(moteur.fichier_verrou, RACINE / "requirements.txt")
        self.assertIn("nico579-commons", runtime._amorcage.dependances_directes(
            moteur.fichier_dependances))

    def test_sous_python_3_8_pas_de_verrou(self):
        # Le verrou est compilé pour 3.11 : l'édition Windows 7 installe requirements.in.
        with mock.patch.object(runtime.sys, "version_info", (3, 8, 10, "final", 0)):
            moteur = runtime.amorcage()
        self.assertIsNone(moteur.fichier_verrou)
        self.assertNotIn("--require-hashes", moteur.commande("py"))

    def test_les_deux_verrous_l_epinglent_a_la_meme_version(self):
        versions = set()
        for nom in ("requirements.txt", "requirements-build.txt"):
            trouve = re.search(r"(?m)^nico579-commons==(\d+\.\d+\.\d+) ",
                               (RACINE / nom).read_text(encoding="utf-8"))
            self.assertIsNotNone(trouve, f"{nom} n'épingle pas nico579-commons")
            versions.add(trouve.group(1))
        self.assertEqual(len(versions), 1, versions)

    def test_le_verrou_de_construction_ne_la_redeclare_pas(self):
        # Elle y arrive par « -r requirements.in » : deux déclarations
        # pourraient diverger.
        self.assertEqual([ligne for ligne in lignes_utiles("requirements-build.in")
                          if ligne.startswith("nico579-commons")], [])

    def test_l_icone_reste_facultative_depuis_les_sources(self):
        verrou = (RACINE / "requirements.txt").read_text(encoding="utf-8")
        self.assertNotRegex(verrou, r"(?mi)^(pystray|pillow)==")

    def test_le_bundle_windows_7_l_epingle_aussi(self):
        self.assertRegex((RACINE / "requirements-win7.txt").read_text(encoding="utf-8"),
                         r"(?m)^nico579-commons==\d+\.\d+\.\d+$")


class TestsMessageDuModeNone(unittest.TestCase):
    def test_le_mode_none_explique_ce_qui_manque_et_sort(self):
        sortie = io.StringIO()
        with mock.patch.object(runtime, "frozen", return_value=False), \
                mock.patch.dict(runtime.os.environ, {"BLINK_BOOTSTRAP": "none"}), \
                mock.patch.object(sys, "argv", ["blink2video"]), \
                mock.patch.object(runtime._amorcage, "dependances_absentes",
                                  return_value=["aiohttp", "nico579-commons"]), \
                contextlib.redirect_stdout(sortie):
            with self.assertRaises(SystemExit) as sortie_du_programme:
                runtime.bootstrap()
        self.assertEqual(sortie_du_programme.exception.code, 1)
        texte = sortie.getvalue()
        self.assertIn("aiohttp, nico579-commons", texte)
        self.assertIn(f"pip install -r {RACINE / 'requirements.txt'}", texte)


class TestsSortieDuServiceViaLaBibliotheque(unittest.TestCase):
    def test_la_bibliotheque_fait_l_essai_et_le_prefixe(self):
        portee = ["systemd-run", "--user", "--scope", "--quiet", "--"]
        commande = ["blink2video", "restart", "--finaliser"]
        with mock.patch.object(autostart, "unite_systemd", return_value=UNITE), \
                mock.patch.object(autostart.relance_commune, "hors_du_service",
                                  return_value=[*portee, *commande]) as hors, \
                mock.patch.object(autostart.runtime, "demarrer") as demarrer:
            self.assertTrue(autostart.sortir_du_service(commande, dict(ENV)))
        self.assertEqual(hors.call_args.args[0], commande)
        self.assertEqual(hors.call_args.kwargs["nom"], "blink2video")
        self.assertEqual(hors.call_args.kwargs["unite"], UNITE)
        self.assertEqual(demarrer.call_args.args[0], [*portee, *commande])
        self.assertEqual(demarrer.call_args.kwargs["env"][autostart.UNITE_ENV], UNITE)

    def test_commande_inchangee_c_est_qu_on_reste_dans_le_service(self):
        commande = ["blink2video", "restart"]
        with mock.patch.object(autostart, "unite_systemd", return_value=UNITE), \
                mock.patch.object(autostart.relance_commune, "hors_du_service",
                                  return_value=list(commande)), \
                mock.patch.object(autostart.runtime, "demarrer") as demarrer:
            self.assertFalse(autostart.sortir_du_service(commande, dict(ENV)))
        demarrer.assert_not_called()

    def test_l_essai_a_vide_passe_par_runtime_lancer_avec_l_environnement_systemctl(self):
        # hors_du_service() reçoit le lanceur de blink2video : l'essai part
        # avec l'environnement complété pour joindre le bus de l'utilisateur.
        env_attendu = {"XDG_RUNTIME_DIR": "/run/user/1000", "PATH": "/usr/bin"}
        lanceur = {}

        def faux_hors_du_service(commande, *, nom, unite, lancer):
            lanceur["reponse"] = lancer(["systemd-run", "true"], check=False)
            return list(commande)

        with mock.patch.object(autostart, "unite_systemd", return_value=UNITE), \
                mock.patch.object(autostart, "env_systemctl", return_value=env_attendu), \
                mock.patch.object(autostart.relance_commune, "hors_du_service",
                                  side_effect=faux_hors_du_service), \
                mock.patch.object(autostart.runtime, "lancer") as lancer:
            autostart.sortir_du_service(["blink2video", "restart"], dict(ENV))
        self.assertEqual(lancer.call_args.kwargs["env"], env_attendu)
        self.assertEqual(lancer.call_args.args[0], ["systemd-run", "true"])


class TestsUniteSystemdViaLaBibliotheque(unittest.TestCase):
    def test_le_nom_de_l_application_est_celui_de_blink2video(self):
        with mock.patch.object(autostart.relance_commune, "unite_systemd",
                               return_value=UNITE) as unite:
            self.assertEqual(autostart.unite_systemd(Path("/proc/self/cgroup")), UNITE)
        self.assertEqual(unite.call_args.args[0], "blink2video")


if __name__ == "__main__":
    unittest.main()
