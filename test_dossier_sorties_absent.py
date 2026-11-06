"""Issue #134 : un dossier des vidéos choisi sur un disque à part, encore
démonté, ne renaît pas sur le disque système.

Le cas décisif n'est pas la boucle de téléchargement mais la vignette : un seul
mkdir(parents=True) direct recréait le dossier, et tout repartait au mauvais
endroit. D'où le balayage du source en fin de fichier."""

from __future__ import annotations

import asyncio
import io
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ["BLINK_BOOTSTRAP"] = "none"
_TEST_HOME = tempfile.TemporaryDirectory(prefix="blink-sorties-absent-")
os.environ["BLINK_HOME"] = _TEST_HOME.name

import blink_engine  # noqa: E402
import blink_registre  # noqa: E402
import merge_daily  # noqa: E402
import runtime  # noqa: E402
import serve  # noqa: E402
from test_runtime_dossier_stockage import BaseDossiers  # noqa: E402


class BaseDisqueDemonte(BaseDossiers):
    """Un dossier choisi dans les réglages, dont le disque n'est pas monté."""

    def setUp(self) -> None:
        super().setUp()
        self.disque = self.racine / "export" / "cam" / "blink2video"
        runtime.ecrire_dossier_stockage(str(self.disque))
        self.demonter()

    def demonter(self) -> None:
        self.disque.rmdir()
        self.disque.parent.rmdir()
        self.assertFalse(self.disque.parent.exists())

    def monter(self) -> None:
        self.disque.mkdir(parents=True)


class TestsDossierManquant(BaseDisqueDemonte):
    def test_dossier_choisi_absent_signale(self):
        self.assertEqual(runtime.dossier_sorties_manquant(), self.disque)

    def test_dossier_revenu_n_est_plus_signale(self):
        self.monter()
        self.assertIsNone(runtime.dossier_sorties_manquant())

    def test_dossier_par_defaut_jamais_signale(self):
        runtime.ecrire_dossier_stockage("")
        self.defaut_sorties.rmdir()
        self.assertIsNone(runtime.dossier_sorties_manquant())
        runtime.creer_sous_sorties(self.defaut_sorties / "Blink_Clips")
        self.assertTrue((self.defaut_sorties / "Blink_Clips").is_dir())


class TestsCreationGardee(BaseDisqueDemonte):
    def test_refuse_sans_rien_creer(self):
        for cible in (self.disque, self.disque / ".blink_thumbs" / "cameras"):
            with self.assertRaises(runtime.DossierSortiesAbsent) as contexte:
                runtime.creer_sous_sorties(cible)
            self.assertIn(str(self.disque), str(contexte.exception))
        self.assertFalse(self.disque.parent.exists())

    def test_est_un_oserror_comme_un_disque_plein(self):
        self.assertTrue(issubclass(runtime.DossierSortiesAbsent, OSError))

    def test_chemin_ailleurs_reste_libre(self):
        ailleurs = self.racine / "autre" / "sortie"
        runtime.creer_sous_sorties(ailleurs)
        self.assertTrue(ailleurs.is_dir())

    def test_nom_voisin_n_est_pas_sous_le_dossier(self):
        voisin = self.disque.with_name(self.disque.name + "-bis")
        runtime.creer_sous_sorties(voisin)
        self.assertTrue(voisin.is_dir())

    def test_disque_revenu_la_creation_reprend(self):
        self.monter()
        runtime.creer_sous_sorties(self.disque / "Blink_Clips")
        self.assertTrue((self.disque / "Blink_Clips").is_dir())

    def test_registre_ne_recree_pas_le_dossier(self):
        with self.assertRaises(runtime.DossierSortiesAbsent):
            blink_registre.save_download_state(
                self.disque / "Blink_Clips", {"version": 2, "clips": {}})
        self.assertFalse(self.disque.parent.exists())


class TestsReglages(BaseDisqueDemonte):
    def test_changer_un_autre_reglage_ne_recree_pas_le_dossier(self):
        serve._verifier_dossier_reglages(str(self.disque))
        runtime.ecrire_dossier_stockage(
            str(self.disque), reglages={**runtime.lire_reglages(), "port": 8899})
        self.assertFalse(self.disque.parent.exists())
        self.assertEqual(runtime.lire_reglages()["port"], 8899)
        self.assertEqual(runtime.lire_reglages()["dossier_sorties"], str(self.disque))

    def test_choisir_un_autre_dossier_le_cree(self):
        autre = self.racine / "autre"
        serve._verifier_dossier_reglages(str(autre))
        runtime.ecrire_dossier_stockage(str(autre))
        self.assertTrue(autre.is_dir())
        self.assertIsNone(runtime.dossier_sorties_manquant())

    def test_revenir_au_defaut_le_cree(self):
        runtime.ecrire_dossier_stockage("")
        self.assertTrue(self.defaut_sorties.is_dir())


class TestsPassage(BaseDisqueDemonte):
    def args(self, commande: str = "download"):
        return SimpleNamespace(command=commande,
                               output=self.disque / "Blink_Clips")

    def passage(self, args, repetition: bool, **correctifs):
        un_passage = mock.AsyncMock(return_value=0)
        with mock.patch.object(blink_engine, "un_passage", un_passage), \
                mock.patch("builtins.print") as sortie:
            resultat = asyncio.run(blink_engine._faire_passage(
                object(), args, [], repetition))
        return resultat, un_passage, [str(a.args[0]) for a in sortie.call_args_list]

    def test_passage_unique_echoue_et_le_dit(self):
        resultat, un_passage, lignes = self.passage(self.args(), False)
        self.assertEqual(resultat, blink_engine._ResultatPassage(1, False))
        un_passage.assert_not_awaited()
        self.assertTrue(any(str(self.disque) in ligne for ligne in lignes))

    def test_list_n_a_pas_besoin_du_dossier(self):
        resultat, un_passage, _ = self.passage(self.args("list"), False)
        self.assertEqual(resultat, blink_engine._ResultatPassage(0, True))
        un_passage.assert_awaited_once()

    def test_boucle_attend_le_montage_puis_part_aussitot(self):
        regards = []

        async def dormir(_secondes):
            regards.append(1)
            if len(regards) == 3:
                self.monter()

        with mock.patch.object(blink_engine.asyncio, "sleep", dormir):
            resultat, un_passage, lignes = self.passage(self.args(), True)

        self.assertEqual(len(regards), 3)
        self.assertEqual(resultat, blink_engine._ResultatPassage(0, True))
        un_passage.assert_awaited_once()
        # Trois regards, une seule ligne : pas de journal rempli toute la nuit.
        self.assertEqual(sum(str(self.disque) in ligne for ligne in lignes), 1)
        self.assertIn(blink_engine.msg("dossier_sorties_retrouve"), lignes)

    def test_arret_demande_pendant_l_attente(self):
        async def dormir(_secondes):
            runtime_arret.return_value = True

        with mock.patch.object(runtime, "arret_demande",
                               return_value=False) as runtime_arret, \
                mock.patch.object(blink_engine.asyncio, "sleep", dormir):
            resultat, un_passage, _ = self.passage(self.args(), True)

        self.assertEqual(resultat, blink_engine._ResultatPassage(0, False))
        un_passage.assert_not_awaited()
        self.assertFalse(self.disque.parent.exists())


class TestsAssemblage(BaseDisqueDemonte):
    def lancer(self, entree: Path) -> tuple:
        args = SimpleNamespace(loop=None, input=entree)
        with mock.patch.object(merge_daily, "parse_args", return_value=args), \
                mock.patch.object(merge_daily, "_executer", return_value=0) as executer, \
                mock.patch.object(merge_daily, "journal") as journal, \
                mock.patch("builtins.print"):
            return merge_daily.main(), executer, journal

    def test_le_tour_est_saute_et_journalise(self):
        code, executer, journal = self.lancer(self.disque / "Blink_Clips")
        self.assertEqual(code, 1)
        executer.assert_not_called()
        self.assertIn(str(self.disque), journal.call_args.args[0])
        self.assertFalse(self.disque.parent.exists())

    def test_entree_explicite_ailleurs_reste_servie(self):
        code, executer, _ = self.lancer(self.racine / "ailleurs")
        self.assertEqual(code, 0)
        executer.assert_called_once()


class TestsServe(BaseDisqueDemonte):
    def test_sonde_rapide_nomme_le_dossier(self):
        reponses = []
        faux = SimpleNamespace(
            path="/api/travail", hote_autorise=lambda: True,
            jeton_valide=lambda: True,
            send_json=lambda corps, *_args: reponses.append(corps))
        serve.Handler.do_GET(faux)
        self.assertEqual(reponses[-1]["dossier_absent"], str(self.disque))
        self.monter()
        serve.Handler.do_GET(faux)
        self.assertEqual(reponses[-1]["dossier_absent"], "")

    def test_vignette_affichee_sans_recreer_le_dossier(self):
        handler = serve.Handler.__new__(serve.Handler)
        handler.paths = {"thumbs": self.disque / ".blink_thumbs"}
        handler.wfile = io.BytesIO()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        with mock.patch.object(serve.BLINK, "call", return_value=b"image"):
            handler.send_camera_thumb("jardin")
        self.assertEqual(handler.wfile.getvalue(), b"image")
        self.assertFalse(self.disque.parent.exists())

    def test_bandeau_traduit_et_present_dans_la_page(self):
        code = Path(serve.__file__).with_name("serve_app.js").read_text(encoding="utf-8")
        self.assertEqual(code.count('"dossier.absent":'), 2)
        self.assertEqual(code.count('$("dossierAbsent")'), 1)
        self.assertIn('id="dossierAbsent"', serve.PAGE)


class TestsBalayage(unittest.TestCase):
    MODULES = ("blink_engine", "blink_models", "blink_registre", "blink_webrtc",
               "diagnostic_xr", "merge_daily", "serve")

    def test_aucun_mkdir_direct_sous_les_sorties(self):
        """Tout ce que ces modules écrivent est sous dossier_sorties()."""
        racine = Path(__file__).resolve().parent
        trouves = {}
        for module in self.MODULES:
            texte = (racine / f"{module}.py").read_text(encoding="utf-8")
            for ligne in texte.splitlines():
                if re.search(r"\.mkdir\(|os\.makedirs\(", ligne):
                    trouves.setdefault(module, []).append(ligne.strip())
        # Seule la voie du choix dans les réglages crée le dossier.
        self.assertEqual(trouves, {
            "serve": ["candidat.mkdir(parents=True, exist_ok=True)"]})


if __name__ == "__main__":
    unittest.main()
