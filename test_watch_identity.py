"""Surveillance et préférences : deux homonymes ne sont pas une caméra."""

import asyncio
import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

os.environ["BLINK_BOOTSTRAP"] = "none"
_TEST_HOME = tempfile.TemporaryDirectory(prefix="blink-watch-identite-import-")
os.environ["BLINK_HOME"] = _TEST_HOME.name

import runtime
import serve
import watch


class TestIdentitesSurveillance(unittest.TestCase):
    def setUp(self):
        temporaire = tempfile.TemporaryDirectory(prefix="blink-watch-identite-")
        self.addCleanup(temporaire.cleanup)
        self.base = Path(temporaire.name)
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(mock.patch.object(watch, "BASE_DIR", self.base))
        self.patches.enter_context(mock.patch.object(runtime, "lire_langue", return_value="fr"))
        self.blink = SimpleNamespace(
            refresh=mock.AsyncMock(),
            homescreen={"cameras": [
                {"name": "Jardin", "network_id": 111, "id": 1, "status": "offline"},
                {"name": "Jardin", "network_id": 222, "id": 2, "status": "online"},
            ]},
            sync={
                "Maison": SimpleNamespace(arm=True, network_id=111, cameras={
                    "Jardin": SimpleNamespace(motion_enabled=True, attributes={"battery": "low"})}),
                "Annexe": SimpleNamespace(arm=True, network_id=222, cameras={
                    "Jardin": SimpleNamespace(motion_enabled=True, attributes={"battery": "ok"})}),
            })

    def lire(self):
        @contextlib.asynccontextmanager
        async def session():
            yield object()

        with mock.patch.object(watch.blink_auth, "session_http_temporaire", new=session), \
                mock.patch.object(watch.blink_auth, "connect_saved",
                                  new=mock.AsyncMock(return_value=self.blink)):
            return asyncio.run(watch.read_state(dt.timezone.utc))

    @staticmethod
    def libelle(etat, appareil):
        return next(nom for nom, camera in etat["cameras"].items()
                    if camera["device_id"] == str(appareil))

    def test_homonymes_reseaux_distincts_alertent_et_ne_dependent_pas_de_l_ordre(self):
        courant = self.lire()
        self.assertEqual(len(courant["cameras"]), 2)
        nom = self.libelle(courant, 1)
        self.assertIn("111", nom)
        self.assertFalse(courant["cameras"][nom]["online"])
        alertes, retours = watch.compare({}, courant, dt.timezone.utc, set())
        self.assertEqual(len(alertes), 2)
        self.assertTrue(all(nom in alerte for alerte in alertes))
        self.assertEqual(retours, [])
        self.blink.homescreen["cameras"].reverse()
        self.blink.sync = dict(reversed(list(self.blink.sync.items())))
        suivant = self.lire()
        self.assertEqual(courant["cameras"], suivant["cameras"])
        self.assertEqual(watch.compare(courant, suivant, dt.timezone.utc, set()), ([], []))

    def test_homonymes_meme_reseau_meme_si_blinkpy_a_deja_ecrase_un_objet(self):
        self.blink.homescreen["cameras"][1]["network_id"] = 111
        for info in self.blink.homescreen["cameras"]:
            info["enabled"] = True
            info["battery"] = "low" if info["id"] == 1 else "ok"
        self.blink.sync.pop("Annexe")
        camera = self.blink.sync["Maison"].cameras["Jardin"]
        camera.camera_id = 2
        camera.attributes["battery"] = "ok"
        courant = self.lire()
        self.assertEqual(len(courant["cameras"]), 2)
        self.assertNotEqual(self.libelle(courant, 1), self.libelle(courant, 2))
        alertes, _ = watch.compare({}, courant, dt.timezone.utc, set())
        self.assertEqual(len(alertes), 2)
        self.assertTrue(all(self.libelle(courant, 1) in a for a in alertes))

    def test_nom_unique_inchange_et_suivi_par_identifiant_quand_homonyme_disparait(self):
        ancien = self.lire()
        self.blink.homescreen["cameras"].pop()
        self.blink.sync.pop("Annexe")
        courant = self.lire()
        self.assertEqual(list(courant["cameras"]), ["Jardin"])
        self.assertEqual(watch.compare(ancien, courant, dt.timezone.utc, set()), ([], []))
        self.assertEqual(watch.normaliser_sourdines(
            {self.libelle(ancien, 1)}, courant["cameras"], ancien["cameras"]), {"Jardin"})

    def test_ancien_etat_ambigu_ne_masque_pas_la_premiere_alerte_reelle(self):
        courant = self.lire()
        ancien = {"cameras": {"Jardin": {
            "online": False, "armed": True, "battery": "low", "system_armed": True}}}
        alertes, retours = watch.compare(ancien, courant, dt.timezone.utc, set())
        self.assertEqual(len(alertes), 2)
        self.assertEqual(retours, [])

    def test_ancien_etat_unique_reste_compatible(self):
        self.blink.homescreen["cameras"].pop()
        self.blink.sync.pop("Annexe")
        courant = self.lire()
        ancien = {"cameras": {"Jardin": {
            "online": False, "armed": True, "battery": "low", "system_armed": True}}}
        self.assertEqual(watch.compare(ancien, courant, dt.timezone.utc, set()), ([], []))

    def test_ancien_nom_en_sourdine_se_migre_en_deux_choix_independants(self):
        courant = self.lire()
        ignores = watch.normaliser_sourdines({"Jardin"}, courant["cameras"])
        self.assertEqual(ignores, set(courant["cameras"]))
        ignores -= {self.libelle(courant, 1)}
        alertes, _ = watch.compare({}, courant, dt.timezone.utc, ignores)
        self.assertEqual(len(alertes), 2)
        self.assertEqual(watch.compare({}, courant, dt.timezone.utc, {"Jardin"}), ([], []))

    def test_migration_persistante_puis_reactivation_d_un_seul_homonyme(self):
        courant = self.lire()
        fichier = self.base / "watch_state.json"
        fichier.write_text(json.dumps({"cameras": {"Jardin": {}},
                                       "ignored": ["Jardin"]}), encoding="utf-8")
        args = SimpleNamespace(dry_run=False, timezone="UTC", loop=None, test=False,
                               ignore=[], unignore=[self.libelle(courant, 1)])
        with mock.patch.object(watch, "WATCH_STATE", fichier), \
                mock.patch.object(watch, "read_state", new=mock.AsyncMock(return_value=courant)), \
                mock.patch.object(runtime, "verrou"), \
                mock.patch.object(watch, "journal"), \
                mock.patch.object(watch, "popup"), \
                mock.patch.object(watch, "toast"), \
                mock.patch.object(watch, "parse_args", return_value=args), \
                mock.patch.object(runtime, "repeter", return_value=0), \
                contextlib.redirect_stdout(io.StringIO()):
            watch._controler(args, dt.timezone.utc)
            self.assertEqual(json.loads(fichier.read_text(encoding="utf-8"))["ignored"],
                             sorted(courant["cameras"]))
            self.assertEqual(watch.main(), 0)
        enregistre = json.loads(fichier.read_text(encoding="utf-8"))
        self.assertEqual(enregistre["ignored"], [self.libelle(courant, 2)])
        self.assertEqual(enregistre["cameras"], courant["cameras"])

    def test_activite_d_un_homonyme_ne_masque_pas_le_silence_de_l_autre(self):
        self.blink.homescreen["cameras"][0]["status"] = "online"
        self.blink.sync["Maison"].cameras["Jardin"].attributes["battery"] = "ok"
        maintenant = dt.datetime.now(dt.timezone.utc)
        vieux = (maintenant - dt.timedelta(days=5)).isoformat()
        recent = (maintenant - dt.timedelta(hours=1)).isoformat()
        registre = self.base / "Blink_Clips" / watch.md.DOWNLOAD_STATE
        registre.parent.mkdir()
        registre.write_text(json.dumps({"clips": {
            "premier": {"camera": "Jardin", "network_id": "111", "device_id": "1",
                        "created_at": vieux, "excluded": True},
            "second": {"camera": "Jardin", "network_id": "222", "device_id": "2",
                       "created_at": recent},
            "ancien_sans_identite": {"camera": "Jardin", "created_at": recent},
        }}), encoding="utf-8")
        courant = self.lire()
        nom = self.libelle(courant, 1)
        self.assertEqual(courant["last_clip"][nom], vieux)
        self.assertEqual(courant["last_clip"][self.libelle(courant, 2)], recent)
        alertes, _ = watch.compare({}, courant, dt.timezone.utc, set())
        self.assertEqual(len(alertes), 1)
        self.assertIn(nom, alertes[0])
        self.assertIn("aucun clip", alertes[0])

    def test_nom_deja_suffixe_par_utilisateur_ne_peut_ecraser_un_homonyme(self):
        nom = self.libelle(self.lire(), 1)
        etats = list(self.lire()["cameras"].values())
        etats.append({**etats[1], "name": nom, "device_id": "3"})
        cameras = watch._libelles_cameras(etats)
        self.assertEqual(len(cameras), 3)
        self.assertEqual(cameras[nom]["device_id"], "3")


class TestAPIIdentitesSurveillance(unittest.TestCase):
    def setUp(self):
        self.cameras = watch._libelles_cameras([
            {"name": "Jardin", "network_id": "111", "device_id": "1"},
            {"name": "Jardin", "network_id": "222", "device_id": "2"},
        ])
        self.noms = list(self.cameras)
        self.entrees = {
            "premier": {"camera": "Jardin", "network_id": "111", "device_id": "1"},
            "second": {"camera": "Jardin", "network_id": "222", "device_id": "2"},
            "usb": {"camera": "Jardin", "network_id": "111", "sync_id": "10"},
            "ambigu": {"camera": "Jardin"},
        }
        self.cles = {cle: serve.blink_registre.camera_setting_key_from_entry(entree)
                     for cle, entree in self.entrees.items()}
        self.actives = {self.cles["premier"], self.cles["second"], self.cles["usb"]}
        self.etat = {"cameras": self.cameras, "ignored": ["Jardin"]}
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(mock.patch.object(serve, "read_entries", return_value=self.entrees))
        self.patches.enter_context(mock.patch.object(watch.md, "load_json", return_value=self.etat))
        self.patches.enter_context(mock.patch.object(runtime, "lire_suppression_auto",
                                                    side_effect=lambda: set(self.actives)))
        self.patches.enter_context(mock.patch.object(runtime, "ecrire_suppression_auto",
                                                    side_effect=self.enregistrer))
        self.patches.enter_context(mock.patch.object(runtime, "verrou_configuration"))

    def enregistrer(self, cles):
        self.actives = set(cles)

    def requete(self, route, payload=None):
        handler = serve.Handler.__new__(serve.Handler)
        handler.path, handler.paths = route, {}
        handler.headers = {"Host": "127.0.0.1", "X-Blink-Token": serve.TOKEN}
        reponses = []
        handler.send_json = lambda valeur, code=200: reponses.append((code, valeur))
        if payload is None:
            handler.do_GET()
        else:
            corps = json.dumps(payload).encode("utf-8")
            handler.headers["Content-Length"] = str(len(corps))
            handler.rfile = io.BytesIO(corps)
            handler.do_POST()
        self.assertEqual(len(reponses), 1)
        return reponses[0]

    def test_sourdine_affiche_deux_appareils_sans_troisieme_nom_ambigu(self):
        code, etat = self.requete("/api/sourdine")
        self.assertEqual(code, 200)
        self.assertEqual(etat["cameras"], self.noms)
        self.assertEqual(etat["ignored"], self.noms)

    def test_suppression_auto_desactive_un_seul_homonyme_cloud_et_usb(self):
        code, _ = self.requete("/api/suppression-auto",
                               {"camera": self.noms[0], "actif": False})
        self.assertEqual(code, 200)
        self.assertEqual(self.actives, {self.cles["second"]})
        _, etat = self.requete("/api/suppression-auto")
        self.assertEqual(etat["cameras"], [{"name": nom} for nom in self.noms])
        self.assertEqual(etat["actives"], [self.noms[1]])

    def test_activation_ne_touche_pas_l_homonyme_ni_les_anciens_clips_ambigus(self):
        self.actives = set()
        code, _ = self.requete("/api/suppression-auto",
                               {"camera": self.noms[0], "actif": True})
        self.assertEqual(code, 200)
        self.assertEqual(self.actives, {self.cles["premier"], self.cles["usb"]})

    def test_ancien_nom_ambigu_ne_peut_activer_les_deux_appareils(self):
        self.actives = set()
        code, _ = self.requete("/api/suppression-auto", {"camera": "Jardin", "actif": True})
        self.assertEqual(code, 400)
        self.assertEqual(self.actives, set())

    def test_get_suppression_auto_resout_le_registre_une_seule_fois(self):
        with mock.patch.object(watch, "_cameras_correspondantes",
                               wraps=watch._cameras_correspondantes) as rapprocher:
            code, etat = self.requete("/api/suppression-auto")
        self.assertEqual(code, 200)
        self.assertEqual(etat["actives"], self.noms)
        self.assertEqual(rapprocher.call_count, len(self.entrees))

    def test_get_suppression_auto_reflete_le_registre_courant(self):
        _, etat = self.requete("/api/suppression-auto")
        self.assertEqual(etat["actives"], self.noms)
        del self.entrees["second"]
        _, suivant = self.requete("/api/suppression-auto")
        self.assertEqual(suivant["cameras"], etat["cameras"])
        self.assertEqual(suivant["actives"], [self.noms[0]])


if __name__ == "__main__":
    unittest.main()
