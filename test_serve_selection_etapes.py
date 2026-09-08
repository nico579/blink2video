"""Une sélection mixte conserve son instantané et l'ordre de ses effets."""

import asyncio
import copy
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


class TestsEtapesSelection(unittest.TestCase):
    def setUp(self):
        temporaire = tempfile.TemporaryDirectory(prefix="blink-selection-etapes-")
        self.addCleanup(temporaire.cleanup)
        self.racine = Path(temporaire.name)
        self.paths = {nom: self.racine / nom for nom in
                      ("input", "normalized", "excluded", "direct", "thumbs")}
        for chemin in self.paths.values():
            chemin.mkdir()
        self.evenements = []
        self.travaux = []
        self.entrees = {}
        self.etat = {"clips": {}}

        self.lire = self.simuler(serve, "read_entries", return_value=self.entrees)
        self.exclure = self.simuler(serve.md, "set_excluded")
        self.reglages = self.simuler(serve.runtime, "lire_reglages", return_value={
            "merge_jour": False, "timezone": "UTC"})
        self.lancer = self.simuler(serve.runtime, "lancer",
                                  side_effect=AssertionError("Sous-processus interdit"))
        self.simuler(serve.threading, "Thread", side_effect=self.thread_controle)
        self.simuler(serve, "MODULE_SLOT")
        self.simuler(serve, "_slot_pris")
        self.simuler(serve, "_slot_rendu")
        self.simuler(serve.blink_engine, "hub_lock")
        self.appel_blink = self.simuler(serve.BLINK, "call", side_effect=self.operation_blink)
        self.simuler(serve.BLINK, "find_sync_module",
                     side_effect=AssertionError("Module USB non prévu"))
        self.clip_cloud = self.simuler(serve.blink_models, "CloudClip",
                                      side_effect=self.cloud_simule)
        self.charger = self.simuler(serve.blink_registre, "load_download_state",
                                   return_value=self.etat)
        self.sauver = self.simuler(serve.blink_registre, "save_download_state")

    def simuler(self, objet, nom, **options):
        patch = mock.patch.object(objet, nom, **options)
        resultat = patch.start()
        self.addCleanup(patch.stop)
        return resultat

    def thread_controle(self, *, target, daemon, args=(), kwargs=None):
        self.assertTrue(daemon)
        self.travaux.append(lambda: target(*args, **(kwargs or {})))
        return SimpleNamespace(start=lambda: self.evenements.append("thread-demarre"))

    def operation_blink(self, operation, *, timeout):
        self.evenements.append("appel-blink")
        return asyncio.run(operation(object()))

    def cloud_simule(self, donnees):
        async def supprimer(blink):
            self.evenements.append(("supprimer-cloud", donnees["id"]))
            return True
        return SimpleNamespace(delete_video=supprimer)

    def ajouter_clip(self, identity, identifiant="42"):
        entree = {"path": identity, "remote_id": identifiant, "source": "cloud",
                  "camera": "Salon", "created_at": "2026-09-08T12:00:00+00:00"}
        self.entrees[identity] = entree
        self.etat["clips"][identity] = copy.deepcopy(entree)
        return entree

    def ajouter_direct(self, identity):
        chemin = self.paths["direct"] / identity
        chemin.parent.mkdir(parents=True, exist_ok=True)
        chemin.write_bytes(b"video locale de test")
        return chemin

    def poster(self, payload):
        handler = object.__new__(serve.Handler)
        handler.path = "/api/appliquer-selection"
        handler.paths = self.paths
        corps = json.dumps(payload).encode("utf-8")
        handler.headers = {"Host": "127.0.0.1", "X-Blink-Token": serve.TOKEN,
                           "Content-Length": str(len(corps))}
        handler.rfile = io.BytesIO(corps)
        reponses = []

        def repondre(resultat, code=200):
            self.evenements.append("reponse")
            reponses.append((code, resultat))

        handler.send_json = repondre
        handler.do_POST()
        self.assertEqual(len(reponses), 1)
        return reponses[0]

    def test_lot_mixte_separe_directs_et_clips_et_demarre_avant_suppression(self):
        clip_statut = "clip-statut.mp4"
        clip_supprime = "clip-supprime.mp4"
        direct_statut = "Salon/2026-09/direct-statut.mp4"
        direct_supprime = "Salon/2026-09/direct-supprime.mp4"
        self.ajouter_clip(clip_statut, "41")
        self.ajouter_clip(clip_supprime, "42")
        # La présence locale du direct prime même si son identité apparaît
        # également dans le registre : elle ne doit jamais atteindre Blink.
        self.ajouter_clip(direct_supprime, "99")
        conserve = self.ajouter_direct(direct_statut)
        supprime = self.ajouter_direct(direct_supprime)

        code, resultat = self.poster({
            "exclure": [clip_statut, direct_statut],
            "inclure": [clip_statut, direct_statut],
            "supprimer": [clip_supprime, direct_supprime],
        })

        self.assertEqual(code, 200)
        self.assertEqual(resultat, {"ok": True, "resultats": {
            clip_supprime: "supprime", direct_supprime: "supprime"}})
        self.assertEqual(self.evenements, ["thread-demarre", "appel-blink",
                                          ("supprimer-cloud", 42), "reponse"])
        self.clip_cloud.assert_called_once()
        self.assertTrue(self.etat["clips"][clip_supprime]["source_deleted"])
        self.assertNotIn("source_deleted", self.etat["clips"][direct_supprime])
        self.assertTrue(conserve.exists())
        self.assertFalse(supprime.exists())
        self.assertEqual(serve._lire_exclusion_directe(self.paths), set())
        self.exclure.assert_not_called()

        # Le travail de registre peut réellement commencer après la réponse.
        self.assertEqual(len(self.travaux), 1)
        self.travaux[0]()
        self.assertEqual(self.exclure.call_args_list, [
            mock.call(self.paths["input"], self.paths["normalized"],
                      self.paths["excluded"], [str(self.paths["input"] / clip_statut)], True),
            mock.call(self.paths["input"], self.paths["normalized"],
                      self.paths["excluded"], [str(self.paths["input"] / clip_statut)], False),
        ])
        self.lire.assert_called_once_with(self.paths)
        self.lancer.assert_not_called()

    def test_reconstruction_differee_reutilise_un_seul_instantane(self):
        identity = "clip-jour.mp4"
        self.ajouter_clip(identity)
        self.reglages.return_value["merge_jour"] = True
        self.lancer.side_effect = None
        options = self.simuler(serve.runtime, "options_fusion", return_value=["--force"])
        commande = self.simuler(serve.runtime, "self_command", return_value=["fusion-simulee"])
        self.poster({"exclure": [identity], "inclure": [identity]})

        # Une nouvelle lecture fournirait un autre état ; le travail garde
        # celui de la requête, y compris pour dédupliquer caméra/jour.
        self.lire.return_value = {}
        self.travaux[0]()
        self.lire.assert_called_once_with(self.paths)
        options.assert_called_once_with(self.reglages.return_value)
        commande.assert_called_once_with("merge", "--camera", "Salon",
                                         "--date", "2026-09-08", "--force")
        self.lancer.assert_called_once()
        self.appel_blink.assert_not_called()

    def test_champs_falsy_restent_des_selections_vides(self):
        for champ in ("exclure", "inclure", "supprimer"):
            for valeur in (None, False, 0, "", {}):
                with self.subTest(champ=champ, valeur=valeur):
                    self.lire.reset_mock()
                    code, resultat = self.poster({champ: valeur})
                    self.assertEqual((code, resultat), (
                        200, {"ok": True, "resultats": {}}))
                    self.lire.assert_called_once_with(self.paths)
                    self.exclure.assert_not_called()
                    self.appel_blink.assert_not_called()
                    self.charger.assert_not_called()
                    self.sauver.assert_not_called()
                    self.lancer.assert_not_called()
                    self.assertEqual(self.travaux, [])
                    self.assertFalse(
                        (self.paths["thumbs"] / serve.DIRECT_EXCLUSION).exists())

    def test_direct_disparu_apres_repartition_reste_local(self):
        identity = "direct.mp4"
        chemin = self.ajouter_direct(identity)

        def lire_apres_disparition(paths):
            chemin.unlink()
            return self.entrees

        self.lire.side_effect = lire_apres_disparition
        code, resultat = self.poster({"supprimer": [identity]})
        self.assertEqual((code, resultat), (200, {"ok": True, "resultats": {
            identity: "deja_absent"}}))
        self.appel_blink.assert_not_called()
        self.charger.assert_not_called()
        self.exclure.assert_not_called()
        self.assertEqual(self.travaux, [])

    def test_direct_apparu_apres_repartition_ne_change_pas_la_selection(self):
        identity = "apparition.mp4"

        def lire_apres_apparition(paths):
            self.ajouter_direct(identity)
            return self.entrees

        self.lire.side_effect = lire_apres_apparition
        code, resultat = self.poster({"supprimer": [identity]})
        self.assertEqual((code, resultat), (200, {"ok": True, "resultats": {
            identity: "inconnu"}}))
        self.assertTrue((self.paths["direct"] / identity).exists())
        # Le comportement historique passe encore par l'opération Blink
        # vide ; aucun objet clip ne doit toutefois être créé ou supprimé.
        self.clip_cloud.assert_not_called()
        self.charger.assert_not_called()

    def test_resolution_modifiee_refuse_la_suppression_des_deux_fichiers(self):
        identity = "direct.mp4"
        initial = self.ajouter_direct(identity)
        autre = self.ajouter_direct("autre.mp4")
        self.simuler(serve, "_chemin_direct_confine", side_effect=[
            initial.resolve(), autre.resolve()])
        code, resultat = self.poster({"supprimer": [identity]})
        self.assertEqual((code, resultat), (200, {"ok": True, "resultats": {
            identity: "echec: ValueError"}}))
        self.assertTrue(initial.exists())
        self.assertTrue(autre.exists())
        self.appel_blink.assert_not_called()
        self.charger.assert_not_called()


if __name__ == "__main__":
    unittest.main()
