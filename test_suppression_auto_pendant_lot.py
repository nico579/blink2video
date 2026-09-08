"""Les refus de suppression s'appliquent au lot USB/cloud en cours.

Transferts et suppressions simulés ; préférences et copies sur disque temporaire.
"""

import contextlib
import datetime as dt
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import blink_engine
import blink_models
import blink_registre
import runtime


def _boite(nom, contenu=b""):
    return (len(contenu) + 8).to_bytes(4, "big") + nom + contenu


VIDEO = (
    _boite(b"ftyp", b"isom\x00\x00\x02\x00isomiso2")
    + _boite(b"moov") + _boite(b"mdat", b"video-simulee")
)


class SuppressionAutoPendantLotTests(unittest.IsolatedAsyncioTestCase):
    async def _executer(self, source, changement=None, autorise=True,
                        cameras=("Salon", "Salon")):
        with tempfile.TemporaryDirectory(prefix="blink-suppression-lot-") as dossier:
            racine = Path(dossier)
            sync = SimpleNamespace(sync_id=10, network_id=7)
            supprimes = []
            copies = []

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
                    if changement:
                        changement("transfert", self, sync)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(VIDEO)
                    copies.append(self.id)
                    return True

                async def delete_video(self, _blink):
                    supprimes.append(self.id)
                    if changement:
                        changement("suppression", self, sync)
                    return True

            clips = [Clip(numero, camera)
                     for numero, camera in enumerate(cameras, 1)]
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
                runtime.ecrire_suppression_auto({
                    blink_registre.camera_setting_key(sync, clip)
                    for clip in clips
                } if autorise else set())
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
                progression = patches.enter_context(mock.patch.object(runtime, "travail"))
                for nom in ("marquer", "toast"):
                    patches.enter_context(mock.patch.object(runtime, nom))
                patches.enter_context(mock.patch.object(
                    runtime, "lire_langue", return_value="fr"))
                patches.enter_context(mock.patch.object(
                    runtime, "lire_reglages", return_value={"port": 8765}))
                patches.enter_context(contextlib.redirect_stdout(io.StringIO()))
                code = await blink_engine.un_passage(
                    object(), arguments, [("Maison", sync)])
                self.assertEqual(code, 0)
                self.assertEqual(copies, ["1", "2"])
                etat = blink_registre.load_download_state(arguments.output)
                entrees = sorted(etat["clips"].values(), key=lambda entree: entree["created_at"])
                self.assertEqual(len(entrees), 2)
                for entree in entrees:
                    self.assertEqual((arguments.output / entree["path"]).read_bytes(), VIDEO)
                ticks = [appel.args[1:3] for appel in progression.call_args_list
                         if appel.kwargs.get("cle") == "phase.download_clips"]
                self.assertEqual(ticks[-1], (2, 2))
                return supprimes, [bool(e.get("source_deleted")) for e in entrees]

    async def test_desactivation_pendant_le_premier_transfert_empeche_toutes_les_suppressions(self):
        def desactiver(phase, clip, _sync):
            if phase == "transfert" and clip.id == "1":
                runtime.ecrire_suppression_auto(set())

        for source in ("usb", "cloud"):
            with self.subTest(source=source):
                self.assertEqual(await self._executer(source, desactiver),
                                 ([], [False, False]))

    async def test_desactivation_pendant_le_second_transfert_protege_le_second_clip(self):
        def desactiver(phase, clip, _sync):
            if phase == "transfert" and clip.id == "2":
                runtime.ecrire_suppression_auto(set())

        for source in ("usb", "cloud"):
            with self.subTest(source=source):
                self.assertEqual(await self._executer(source, desactiver),
                                 (["1"], [True, False]))

    async def test_requete_de_suppression_deja_envoyee_peut_finir_mais_pas_la_suivante(self):
        def desactiver(phase, clip, _sync):
            if phase == "suppression" and clip.id == "1":
                runtime.ecrire_suppression_auto(set())

        for source in ("usb", "cloud"):
            with self.subTest(source=source):
                self.assertEqual(await self._executer(source, desactiver),
                                 (["1"], [True, False]))

    async def test_desactiver_une_camera_conserve_l_autorisation_de_l_autre(self):
        def desactiver(phase, clip, sync):
            if phase == "transfert" and clip.id == "1":
                autorisations = runtime.lire_suppression_auto()
                autorisations.discard(blink_registre.camera_setting_key(sync, clip))
                runtime.ecrire_suppression_auto(autorisations)

        for source in ("usb", "cloud"):
            with self.subTest(source=source):
                self.assertEqual(await self._executer(
                    source, desactiver, cameras=("Salon", "Jardin")),
                    (["2"], [False, True]))

    async def test_autorisation_inchangee_conserve_les_suppressions(self):
        for source in ("usb", "cloud"):
            with self.subTest(source=source):
                self.assertEqual(await self._executer(source),
                                 (["1", "2"], [True, True]))

    async def test_camera_non_autorisee_conserve_tous_les_clips(self):
        for source in ("usb", "cloud"):
            with self.subTest(source=source):
                self.assertEqual(await self._executer(source, autorise=False),
                                 ([], [False, False]))


if __name__ == "__main__":
    unittest.main()
