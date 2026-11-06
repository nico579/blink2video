"""Journal des téléchargements : chaque clip rapatrié laisse une ligne dans
telechargements.log, et la notification « N nouveaux clips » en laisse une aussi.
Né des « clips fantômes » : un toast annonçait deux clips que la page ne
montrait pas, et rien ne permettait de dire lesquels il annonçait.

Vrai passage un_passage() pour les deux sources, montage calqué sur
test_blink_engine_notification_webhook.py."""

import contextlib
import datetime as dt
import io
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import blink_engine
import blink_models
import runtime
from test_blink_engine_notification_webhook import VIDEO


class JournalTelechargementsUnPassage(unittest.IsolatedAsyncioTestCase):
    async def _passage(self, source, nombre=1):
        with tempfile.TemporaryDirectory(prefix="blink-journal-dl-") as dossier:
            racine = Path(dossier)
            sync = SimpleNamespace(sync_id=10, network_id=7)

            class Clip:
                def __init__(self, numero, camera):
                    self.id = str(numero)
                    self.name = camera
                    self.network_id = 7
                    self.device_id = "cam-" + camera
                    self.created_at = dt.datetime(
                        2026, 9, 7, 10, numero, tzinfo=dt.timezone.utc)
                    self.size = 1

                async def download_to(self, _blink, target):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(VIDEO)
                    return True

                async def delete_video(self, _blink):
                    return True

            clips = [Clip(n, "Jardin") for n in range(1, nombre + 1)]
            arguments = SimpleNamespace(
                since=None, camera=None, command="download",
                output=racine / "clips", hub=None, overwrite=False,
                source=source, loop=None,
            )

            async def telecharger_usb(blink, clip, target, _overwrite):
                await clip.download_to(blink, target)
                return "downloaded"

            with contextlib.ExitStack() as patches:
                patches.enter_context(mock.patch.object(
                    runtime, "app_dir", return_value=racine))
                runtime.ecrire_suppression_auto(set())
                for nom in ("read_local_manifest", "read_cloud_manifest"):
                    patches.enter_context(mock.patch.object(
                        blink_models, nom, new=mock.AsyncMock(return_value=clips)))
                patches.enter_context(mock.patch.object(
                    blink_engine, "download_clip", side_effect=telecharger_usb))
                patches.enter_context(mock.patch.object(
                    blink_engine, "hub_lock", side_effect=lambda *_a: contextlib.nullcontext()))
                patches.enter_context(mock.patch.object(
                    blink_engine.md, "valid_mp4_complet",
                    side_effect=blink_engine.md.valid_mp4))
                patches.enter_context(mock.patch.object(runtime, "travail"))
                for nom in ("marquer", "toast", "notifier_nouveau_media"):
                    patches.enter_context(mock.patch.object(runtime, nom))
                patches.enter_context(mock.patch.object(
                    runtime, "lire_langue", return_value="fr"))
                patches.enter_context(mock.patch.object(
                    runtime, "lire_reglages", return_value={"port": 8765}))
                patches.enter_context(contextlib.redirect_stdout(io.StringIO()))
                code = await blink_engine.un_passage(
                    object(), arguments, [("Maison", sync)])
            self.assertEqual(code, 0)
            journal = racine / blink_engine.JOURNAL_TELECHARGEMENTS
            lignes = journal.read_text(encoding="utf-8").splitlines() if journal.is_file() else []
            return lignes

    async def _verifier(self, source):
        lignes = await self._passage(source, nombre=2)
        telechargements = [l for l in lignes if f"  {source}  " in l]
        self.assertEqual(len(telechargements), 2, lignes)
        for ligne in telechargements:
            self.assertRegex(
                ligne,
                rf"^\d{{4}}-\d\d-\d\d \d\d:\d\d:\d\d  {source}  Jardin/.+\.mp4  {len(VIDEO)} o$")
        # La ligne de notification porte le même nombre que le toast.
        self.assertTrue(lignes[-1].endswith("  notification  2 clip(s) annoncé(s)"), lignes)
        # Aucun identifiant de compte ni URL dans le journal.
        self.assertFalse(any("http" in l or "network" in l for l in lignes))

    async def test_clips_usb_journalises(self):
        await self._verifier("usb")

    async def test_clips_cloud_journalises(self):
        await self._verifier("cloud")

    async def test_inventaire_dit_ce_que_le_registre_contenait(self):
        lignes = await self._passage("usb", nombre=2)
        inventaires = [ligne for ligne in lignes
                       if "nouveaux 2" in ligne or "new 2" in ligne]
        self.assertEqual(len(inventaires), 1, lignes)
        self.assertRegex(inventaires[0], r"regist(re|ry) 0")

    async def test_rien_a_telecharger_n_ecrit_rien(self):
        lignes = await self._passage("usb", nombre=0)
        self.assertEqual(lignes, [])


class EchecsCloudRecurrents(unittest.IsolatedAsyncioTestCase):
    """Issue #139 : 21 clips à la barre, puis 19, puis 19 encore, et rien de
    neuf dans la galerie. Un média refusé n'entre pas au registre et revient à
    chaque tour ; le journal doit dire combien, et pourquoi."""

    def _clip(self, numero, contenu):
        clip = blink_models.CloudClip({
            "id": numero, "device_name": "Jardin", "device_id": 22, "network_id": 7,
            "created_at": f"2026-11-05T20:{numero:02d}:00+00:00",
            "media": f"https://example.invalid/{numero}",
        })

        async def telecharger(_blink, cible):
            cible.parent.mkdir(parents=True, exist_ok=True)
            cible.write_bytes(contenu)
            return True

        clip.download_to = telecharger
        return clip

    async def _tour(self, racine, clips):
        arguments = SimpleNamespace(
            since=None, camera=None, command="download", output=racine / "clips",
            hub=None, overwrite=False, source="cloud", loop=None,
        )
        with contextlib.ExitStack() as patches:
            patches.enter_context(mock.patch.object(
                runtime, "app_dir", return_value=racine))
            runtime.ecrire_suppression_auto(set())
            patches.enter_context(mock.patch.object(
                blink_models, "read_cloud_manifest",
                new=mock.AsyncMock(return_value=clips)))
            patches.enter_context(mock.patch.object(
                blink_engine.md, "valid_mp4_complet",
                side_effect=lambda chemin: Path(chemin).read_bytes() == VIDEO))
            patches.enter_context(mock.patch.object(
                blink_engine.md, "raison_refus_mp4",
                return_value="outil=ffprobe ligne=Invalid data"))
            patches.enter_context(mock.patch.object(runtime, "travail"))
            patches.enter_context(mock.patch.object(runtime, "notifier_nouveau_media"))
            patches.enter_context(mock.patch.object(
                runtime, "lire_langue", return_value="fr"))
            sortie = io.StringIO()
            patches.enter_context(contextlib.redirect_stdout(sortie))
            resultat = await blink_engine.traiter_cloud(object(), arguments, [])
        return resultat, sortie.getvalue()

    async def test_media_refuse_revient_et_le_journal_dit_pourquoi(self):
        with tempfile.TemporaryDirectory(prefix="blink-journal-dl-") as dossier:
            racine = Path(dossier)
            bon = self._clip(1, VIDEO)
            refuses = [self._clip(n, b"refuse") for n in (2, 3, 4)]

            premier, sortie = await self._tour(racine, [bon, *refuses])
            self.assertEqual((premier.downloaded, premier.failed), (1, 3))
            self.assertIn("outil=ffprobe ligne=Invalid data", sortie)

            second, _ = await self._tour(racine, [bon, *refuses])
            self.assertEqual((second.downloaded, second.failed), (0, 3))

            lignes = (racine / blink_engine.JOURNAL_TELECHARGEMENTS).read_text(
                encoding="utf-8").splitlines()
        inventaires = [l for l in lignes if "  cloud : registre" in l
                       or "  cloud: registry" in l]
        self.assertEqual(len(inventaires), 2, lignes)
        self.assertRegex(inventaires[0], r"regist(re|ry) 0, ")
        self.assertRegex(inventaires[1], r"regist(re|ry) 1, ")
        # Une ligne par tour et par motif, pas une par clip : le fichier n'est
        # pas borné et la boucle cloud tourne chaque minute.
        echecs = [l for l in lignes if "3 clip(s)" in l]
        self.assertEqual(len(echecs), 2, lignes)
        for ligne in echecs:
            self.assertIn("outil=ffprobe ligne=Invalid data", ligne)
            self.assertRegex(ligne, r"2026-11-05_.+\.mp4$")
        self.assertFalse(any("http" in l for l in lignes), lignes)

    async def test_motifs_differents_restent_separes(self):
        with tempfile.TemporaryDirectory(prefix="blink-journal-dl-") as dossier:
            racine = Path(dossier)
            refuse = self._clip(1, b"refuse")
            coupe = self._clip(2, VIDEO)

            async def planter(_blink, _cible):
                raise OSError("adresse privée https://example.invalid/2")

            coupe.download_to = planter
            await self._tour(racine, [refuse, coupe])
            lignes = (racine / blink_engine.JOURNAL_TELECHARGEMENTS).read_text(
                encoding="utf-8").splitlines()
        echecs = [l for l in lignes if "1 clip(s)" in l]
        self.assertEqual(len(echecs), 2, lignes)
        self.assertTrue(any("OSError" in l for l in echecs), echecs)
        self.assertFalse(any("http" in l for l in lignes), lignes)

    async def test_tour_sans_echec_n_ecrit_pas_de_ligne_d_echec(self):
        with tempfile.TemporaryDirectory(prefix="blink-journal-dl-") as dossier:
            racine = Path(dossier)
            await self._tour(racine, [self._clip(1, VIDEO)])
            journal = (racine / blink_engine.JOURNAL_TELECHARGEMENTS).read_text(
                encoding="utf-8")
        self.assertNotIn("clip(s) [", journal)


class AideJournal(unittest.TestCase):
    def test_chemin_hors_du_dossier_se_replie_sur_le_nom(self):
        with tempfile.TemporaryDirectory(prefix="blink-journal-dl-") as dossier:
            racine = Path(dossier)
            fichier = racine / "ailleurs" / "x.mp4"
            fichier.parent.mkdir()
            fichier.write_bytes(b"abc")
            with mock.patch.object(runtime, "app_dir", return_value=racine):
                blink_engine._journaliser_telechargement("usb", fichier, racine / "clips")
            ligne = (racine / blink_engine.JOURNAL_TELECHARGEMENTS).read_text(encoding="utf-8")
            self.assertTrue(re.search(r"  usb  x\.mp4  3 o\n$", ligne), ligne)

    def test_fichier_disparu_n_est_pas_fatal(self):
        with tempfile.TemporaryDirectory(prefix="blink-journal-dl-") as dossier:
            racine = Path(dossier)
            with mock.patch.object(runtime, "app_dir", return_value=racine):
                blink_engine._journaliser_telechargement(
                    "cloud", racine / "clips" / "a" / "b.mp4", racine / "clips")
            ligne = (racine / blink_engine.JOURNAL_TELECHARGEMENTS).read_text(encoding="utf-8")
            self.assertIn("  cloud  a/b.mp4  0 o", ligne)


if __name__ == "__main__":
    unittest.main()
