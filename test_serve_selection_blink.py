"""Contrat HTTP de suppression Blink, sans caméra, réseau ni écriture réelle."""

import asyncio
import copy
import datetime as dt
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


_IMPORT_HOME = tempfile.TemporaryDirectory(prefix="blink-selection-import-")
with mock.patch.dict(os.environ, {"BLINK_BOOTSTRAP": "none",
                                  "BLINK_HOME": _IMPORT_HOME.name}):
    import serve


INSTANT = dt.datetime(2026, 9, 8, 10, 0, tzinfo=dt.timezone.utc)


class TestsSuppressionSelectionBlink(unittest.TestCase):
    def patch(self, objet, nom, **options):
        patch = mock.patch.object(objet, nom, **options)
        resultat = patch.start()
        self.addCleanup(patch.stop)
        return resultat

    def setUp(self):
        dossier = tempfile.TemporaryDirectory(prefix="blink-selection-")
        self.addCleanup(dossier.cleanup)
        self.paths = {"input": Path(dossier.name) / "clips"}
        self.entrees = {}
        self.etat = {"clips": {}}
        self.lecture = self.patch(serve, "read_entries", return_value=self.entrees)
        self.charge = self.patch(serve.blink_registre, "load_download_state",
                                  return_value=self.etat)
        self.sauve = self.patch(serve.blink_registre, "save_download_state")
        self.blink = object()
        self.session = self.patch(serve, "BLINK")
        self.session.call.side_effect = lambda operation, **_: asyncio.run(
            operation(self.blink))
        self.modules = {"1": SimpleNamespace(sync_id=1),
                        "2": SimpleNamespace(sync_id=2)}
        self.session.find_sync_module.side_effect = (
            lambda blink, entree: self.modules[str(entree.get("sync_id") or "1")])
        self.manifestes = {"1": [], "2": []}
        self.manifeste = self.patch(
            serve.blink_models, "read_local_manifest", new_callable=mock.AsyncMock,
            side_effect=lambda sync: self.manifestes[str(sync.sync_id)])
        self.cloud = self.patch(serve.blink_models, "CloudClip")
        self.cloud.return_value.delete_video = mock.AsyncMock(return_value=True)
        self.slot = self.patch(serve, "MODULE_SLOT")
        self.slot.acquire.return_value = True
        self.pris = self.patch(serve, "_slot_pris")
        self.rendu = self.patch(serve, "_slot_rendu")
        self.message_occupe = self.patch(serve, "_slot_occupe_message",
                                         return_value="module déjà occupé")
        self.verrou = self.patch(serve.blink_engine, "hub_lock")
        # Si un mauvais routage déclenche un travail local, échouer avant
        # tout processus, thread ou changement de registre/exclusion.
        self.interdits = [self.patch(
            objet, nom, side_effect=AssertionError("Mutation interdite : " + nom))
            for objet, nom in ((serve.runtime, "lancer"),
                               (serve.runtime, "demarrer"),
                               (serve.threading, "Thread"),
                               (serve.md, "set_excluded"),
                               (serve, "_ecrire_exclusion_directe"))]

    def tearDown(self):
        for interdit in self.interdits:
            interdit.assert_not_called()

    def entree(self, nom, remote_id="7", camera="Salon", source="usb",
               sync_id="1", created_at=None, **extras):
        identity = "Salon/2026-09/" + nom + ".mp4"
        entree = {"path": identity, "remote_id": remote_id, "camera": camera,
                  "source": source, "sync_id": sync_id,
                  "created_at": created_at or INSTANT.isoformat()}
        entree.update(extras)
        self.entrees[nom] = entree
        self.etat["clips"][nom] = copy.deepcopy(entree)
        return identity

    def clip(self, id_clip, camera="Salon", secondes=0, resultat=True):
        return SimpleNamespace(id=id_clip, name=camera,
                               created_at=INSTANT + dt.timedelta(seconds=secondes),
                               delete_video=mock.AsyncMock(return_value=resultat))

    def supprimer(self, identities):
        corps = json.dumps({"supprimer": identities}).encode("utf-8")
        handler = object.__new__(serve.Handler)
        handler.path = "/api/appliquer-selection"
        handler.paths = self.paths
        handler.client_address = ("127.0.0.1", 12345)
        handler.headers = {"Host": "127.0.0.1:8765", "X-Blink-Token": serve.TOKEN,
                           "Content-Length": str(len(corps))}
        handler.rfile = io.BytesIO(corps)
        handler.send_json = mock.Mock()
        handler.send_error = mock.Mock()
        handler.do_POST()
        handler.send_error.assert_not_called()
        handler.send_json.assert_called_once()
        args = handler.send_json.call_args[0]
        self.assertEqual(len(args), 1)
        self.assertIs(args[0]["ok"], True)
        return args[0]["resultats"]

    def test_cloud_prefere_identifiant_fichier_et_utilise_repli_ancien(self):
        moderne = self.entree("clip_123_abcdef123456", remote_id="999", source="cloud")
        ancien = self.entree("ancien", remote_id=456, camera=" Terrasse ", source="cloud")
        self.assertEqual(self.supprimer([moderne, ancien]),
                         {moderne: "supprime", ancien: "supprime"})
        self.assertEqual(self.cloud.call_args_list, [
            mock.call({"id": 123, "device_name": "Salon", "created_at": INSTANT.isoformat()}),
            mock.call({"id": 456, "device_name": "Terrasse", "created_at": INSTANT.isoformat()})])
        self.cloud.return_value.delete_video.assert_has_awaits(
            [mock.call(self.blink), mock.call(self.blink)])
        self.manifeste.assert_not_awaited()
        self.session.find_sync_module.assert_not_called()
        self.assertEqual(self.session.call.call_args[1], {"timeout": 120})
        self.assertTrue(all(e["source_deleted"] for e in self.etat["clips"].values()))

    def test_inconnu_et_identifiant_absent_restent_des_resultats_distincts(self):
        absent = self.entree("sans-identifiant", remote_id=None)
        inconnu = "Salon/2026-09/inconnu.mp4"
        self.entrees["invalide"] = None
        self.assertEqual(self.supprimer([inconnu, absent]),
                         {inconnu: "inconnu", absent: "identifiant_introuvable"})
        self.cloud.assert_not_called()
        self.manifeste.assert_not_awaited()
        self.charge.assert_not_called()
        self.sauve.assert_not_called()

    def test_usb_un_manifeste_par_module_et_delai_par_camera_distincte(self):
        salon = self.entree("salon", camera="Salon")
        terrasse = self.entree("terrasse", camera="Terrasse")
        jardin = self.entree("jardin", camera="Jardin", sync_id="2")
        nuage = self.entree("cloud", camera="Entrée", source="cloud")
        clips = [self.clip(10), self.clip(20, "Terrasse"), self.clip(30, "Jardin")]
        self.manifestes.update({"1": clips[:2], "2": clips[2:]})
        self.assertEqual(self.supprimer([salon, terrasse, jardin, nuage]),
                         dict.fromkeys([salon, terrasse, jardin, nuage], "supprime"))
        self.assertEqual(self.manifeste.await_args_list,
                         [mock.call(self.modules["1"]), mock.call(self.modules["2"])])
        self.assertEqual(self.session.call.call_args[1], {"timeout": 300})
        for clip in clips:
            clip.delete_video.assert_awaited_once_with(self.blink)
        self.sauve.assert_called_once_with(self.paths["input"], self.etat)

    def test_usb_reindexe_ignore_id_perime_et_accepte_bornes_deux_secondes(self):
        for ecart in (-2, 2):
            with self.subTest(ecart=ecart):
                identity = self.entree("clip_7_abcdef123456", camera=" Salon ")
                ancien_numero = self.clip(7, "Garage")
                bon = self.clip(99, "sALOn", secondes=ecart)
                trop_loin = self.clip(100, secondes=2.001)
                self.manifestes["1"] = [ancien_numero, bon, trop_loin]
                self.assertEqual(self.supprimer([identity]), {identity: "supprime"})
                bon.delete_video.assert_awaited_once_with(self.blink)
                ancien_numero.delete_video.assert_not_awaited()
                trop_loin.delete_video.assert_not_awaited()

    def test_usb_refuse_ambiguite_meme_si_id_historique_correspond(self):
        identity = self.entree("clip_7_abcdef123456")
        clips = [self.clip(7), self.clip(8, secondes=1)]
        self.manifestes["1"] = clips
        self.assertEqual(self.supprimer([identity]), {identity: "ambigu"})
        for clip in clips:
            clip.delete_video.assert_not_awaited()
        self.assertNotIn("source_deleted", self.etat["clips"]["clip_7_abcdef123456"])

    def test_usb_absence_et_date_invalide_ne_suppriment_aucun_clip(self):
        absent = self.entree("absent", camera="Terrasse")
        sans_date = self.entree("date-invalide", created_at="non-date")
        clip = self.clip(7)
        self.manifestes["1"] = [clip]
        self.assertEqual(self.supprimer([absent, sans_date]),
                         {absent: "deja_absent", sans_date: "deja_absent"})
        clip.delete_video.assert_not_awaited()
        self.assertTrue(all(e["source_deleted"] for e in self.etat["clips"].values()))

    def test_echec_clip_ne_stoppe_pas_le_reste_du_lot(self):
        usb = self.entree("usb-refuse")
        nuage = self.entree("cloud-refuse", source="cloud")
        erreur = self.entree("usb-erreur", camera="Terrasse")
        ok = self.entree("usb-ok", camera="Jardin")
        clips = [self.clip(7, resultat=False), self.clip(8, "Terrasse"), self.clip(9, "Jardin")]
        clips[1].delete_video.side_effect = OSError("simulée")
        self.manifestes["1"] = clips
        self.cloud.return_value.delete_video.return_value = False
        self.assertEqual(self.supprimer([usb, nuage, erreur, ok]),
                         {usb: "echec", nuage: "echec", erreur: "echec: OSError", ok: "supprime"})
        self.assertNotIn("source_deleted", self.etat["clips"]["usb-refuse"])
        self.assertNotIn("source_deleted", self.etat["clips"]["cloud-refuse"])
        self.assertTrue(self.etat["clips"]["usb-ok"]["source_deleted"])

    def test_manifeste_marque_aussi_usb_absents_du_meme_module_uniquement(self):
        cible = self.entree("cible", remote_id="7")
        self.entree("absent_8_abcdef123456", remote_id="7")
        self.entree("ancien-absent", remote_id="9")
        self.entree("present_7_abcdef123456", remote_id="8")
        self.entree("autre-module", remote_id="8", sync_id="2")
        self.entree("nuage", remote_id="8", source="cloud")
        self.entree("sans-id", remote_id=None)
        self.entree("deja-marque", source_deleted=True)
        self.etat["clips"]["invalide"] = None
        self.manifestes["1"] = [self.clip(7)]
        self.assertEqual(self.supprimer([cible]), {cible: "supprime"})
        marques = {cle for cle, entree in self.etat["clips"].items()
                   if isinstance(entree, dict) and entree.get("source_deleted")}
        self.assertEqual(marques, {"cible", "absent_8_abcdef123456", "ancien-absent", "deja-marque"})
        self.manifeste.assert_awaited_once_with(self.modules["1"])
        self.sauve.assert_called_once_with(self.paths["input"], self.etat)

    def test_slot_occupe_ne_prend_pas_verrou_et_ne_libere_pas_autrui(self):
        cible = self.entree("cible")
        inconnu = "inconnu.mp4"
        self.slot.acquire.return_value = False
        self.assertEqual(self.supprimer([cible, inconnu]),
                         {cible: "echec: BusyError", inconnu: "inconnu"})
        self.slot.acquire.assert_called_once_with(blocking=False)
        self.slot.release.assert_not_called()
        self.pris.assert_not_called()
        self.rendu.assert_not_called()
        self.verrou.assert_not_called()
        self.session.call.assert_not_called()
        self.sauve.assert_not_called()

    def test_verrou_externe_occupe_libere_slot_local(self):
        cible = self.entree("cible")
        self.verrou.return_value.__enter__.side_effect = serve.blink_engine.BusyError("occupé")
        self.assertEqual(self.supprimer([cible]), {cible: "echec: BusyError"})
        self.verrou.assert_called_once_with("suppression manuelle")
        self.pris.assert_called_once_with("suppression manuelle")
        self.rendu.assert_called_once_with()
        self.slot.release.assert_called_once_with()
        self.session.call.assert_not_called()

    def test_timeout_session_libere_verrou_et_slot(self):
        cible = self.entree("cible")
        self.session.call.side_effect = TimeoutError("simulée")
        self.assertEqual(self.supprimer([cible]), {cible: "echec: TimeoutError"})
        self.assertEqual(self.session.call.call_args[1], {"timeout": 120})
        self.verrou.return_value.__exit__.assert_called_once()
        self.rendu.assert_called_once_with()
        self.slot.release.assert_called_once_with()
        self.sauve.assert_not_called()

    def test_exception_tardive_ne_remplace_pas_resultat_deja_obtenu(self):
        cible = self.entree("cible", source="cloud")

        def operation_puis_erreur(operation, **options):
            asyncio.run(operation(self.blink))
            raise TimeoutError("simulée après la suppression")

        self.session.call.side_effect = operation_puis_erreur
        self.assertEqual(self.supprimer([cible]), {cible: "supprime"})
        self.assertTrue(self.etat["clips"]["cible"]["source_deleted"])
        self.rendu.assert_called_once_with()
        self.slot.release.assert_called_once_with()

    def test_erreur_manifeste_est_locale_au_clip_et_module_suivant_continue(self):
        premier = self.entree("premier")
        second = self.entree("second", sync_id="2", camera="Jardin")
        clip = self.clip(7, "Jardin")
        self.manifeste.side_effect = [OSError("simulée"), [clip]]
        self.assertEqual(self.supprimer([premier, second]),
                         {premier: "echec: OSError", second: "supprime"})
        clip.delete_video.assert_awaited_once_with(self.blink)
        self.assertNotIn("source_deleted", self.etat["clips"]["premier"])
        self.slot.release.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
