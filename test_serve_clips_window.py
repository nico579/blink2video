"""Non-régression du plafonnement et du cache de durée de /api/clips.

Deux défauts corrigés hors séquence le 17 août 2026 (voir AUDIT-2026-08-13.md,
section 28.6) : l'inventaire grossissait indéfiniment sans fenêtre par défaut,
et la durée d'un clip écarté était re-sondée par ffmpeg à chaque requête."""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python 3.8, édition Windows 7
    from backports.zoneinfo import ZoneInfo

os.environ["BLINK_BOOTSTRAP"] = "none"
_TEST_HOME = tempfile.TemporaryDirectory(prefix="blink-window-")
os.environ["BLINK_HOME"] = _TEST_HOME.name

import serve  # noqa: E402 - bootstrap neutralisé avant import


class BacASable(unittest.TestCase):
    def setUp(self) -> None:
        self.temporaire = tempfile.TemporaryDirectory(prefix="blink_window_test_")
        self.racine = Path(self.temporaire.name)
        self.paths = {
            "input": self.racine / "clips",
            "normalized": self.racine / "normalized",
            "excluded": self.racine / "excluded",
            "thumbs": self.racine / "thumbs",
        }
        for chemin in self.paths.values():
            chemin.mkdir(parents=True, exist_ok=True)
        self.timezone = ZoneInfo("UTC")

    def tearDown(self) -> None:
        self.temporaire.cleanup()

    def ecrire_registre(self, entries: dict) -> None:
        (self.paths["input"] / serve.md.DOWNLOAD_STATE).write_text(
            json.dumps({"clips": entries}), encoding="utf-8"
        )

    def entree(self, identity: str, created: str, excluded: bool = False) -> dict:
        return {
            "path": identity, "created_at": created, "camera": "jardin",
            "excluded": excluded, "source": "usb",
        }


class FenetreParDefautTests(BacASable):
    def test_clip_recent_visible_clip_ancien_masque_par_defaut(self) -> None:
        recent = self.entree("recent.mp4", "2026-08-15T12:00:00+00:00")
        ancien = self.entree("ancien.mp4", "2026-01-01T12:00:00+00:00")
        self.ecrire_registre({"recent.mp4": recent, "ancien.mp4": ancien})

        # Un horodatage complet, pas juste une date (depuis le 27 août 2026 :
        # collect() borne désormais à l'heure près, pour la plage
        # personnalisée du panneau Période). Minuit du jour de coupure
        # reproduit exactement l'ancienne troncature à la date.
        cutoff = (dt.datetime(2026, 8, 17, tzinfo=self.timezone)
                  - dt.timedelta(days=30))
        resultat = serve.collect(self.paths, self.timezone, depuis=cutoff)

        identites = {c["identity"] for c in resultat["clips"]}
        self.assertEqual(identites, {"recent.mp4"})
        self.assertEqual(resultat["total_known"], 2)
        self.assertTrue(resultat["filtered"])

    def test_depuis_none_renvoie_tout_l_historique(self) -> None:
        recent = self.entree("recent.mp4", "2026-08-15T12:00:00+00:00")
        ancien = self.entree("ancien.mp4", "2026-01-01T12:00:00+00:00")
        self.ecrire_registre({"recent.mp4": recent, "ancien.mp4": ancien})

        resultat = serve.collect(self.paths, self.timezone, depuis=None)

        identites = {c["identity"] for c in resultat["clips"]}
        self.assertEqual(identites, {"recent.mp4", "ancien.mp4"})
        self.assertFalse(resultat["filtered"])


class PlagePersonnaliseeTests(BacASable):
    """Filtre à l'heure près (panneau Période) : signalé sur Reddit,
    2026-08-27, pour retrouver un incident précis sans défiler tout
    l'historique d'une caméra extérieure toujours armée."""

    def test_jusqua_exclut_les_clips_plus_recents(self) -> None:
        tot = self.entree("tot.mp4", "2026-08-10T08:00:00+00:00")
        tard = self.entree("tard.mp4", "2026-08-20T08:00:00+00:00")
        self.ecrire_registre({"tot.mp4": tot, "tard.mp4": tard})

        borne = dt.datetime(2026, 8, 15, tzinfo=self.timezone)
        resultat = serve.collect(self.paths, self.timezone, jusqua=borne)

        identites = {c["identity"] for c in resultat["clips"]}
        self.assertEqual(identites, {"tot.mp4"})
        self.assertTrue(resultat["filtered"])

    def test_depuis_et_jusqua_ciblent_une_fenetre_precise_a_l_heure(self) -> None:
        avant = self.entree("avant.mp4", "2026-08-15T09:00:00+00:00")
        pendant = self.entree("pendant.mp4", "2026-08-15T14:30:00+00:00")
        apres = self.entree("apres.mp4", "2026-08-15T20:00:00+00:00")
        self.ecrire_registre({"avant.mp4": avant, "pendant.mp4": pendant,
                              "apres.mp4": apres})

        depuis = dt.datetime(2026, 8, 15, 12, 0, tzinfo=self.timezone)
        jusqua = dt.datetime(2026, 8, 15, 18, 0, tzinfo=self.timezone)
        resultat = serve.collect(self.paths, self.timezone,
                                 depuis=depuis, jusqua=jusqua)

        identites = {c["identity"] for c in resultat["clips"]}
        self.assertEqual(identites, {"pendant.mp4"})


class RoutePlageTests(BacASable):
    """/api/clips : préréglages (RANGE_PRESETS_HOURS) et plage personnalisée,
    au niveau de la route plutôt que de collect() directement."""

    def construire_handler(self):
        handler = serve.Handler.__new__(serve.Handler)
        handler.paths = self.paths
        handler.timezone = self.timezone
        handler.ffmpeg = ""
        handler.headers = {"Host": "127.0.0.1"}
        return handler

    def appeler(self, chemin: str):
        handler = self.construire_handler()
        handler.path = chemin
        reponses = []
        handler.send_json = lambda payload, code=200: reponses.append((code, payload))
        handler.do_GET()
        return reponses

    def test_preset_today_ne_renvoie_que_les_24_dernieres_heures(self) -> None:
        # Bornes calculées depuis l'heure réelle plutôt qu'un dt.datetime.now
        # simulé : mocker la classe datetime affecterait tout le processus
        # (module partagé par tous les fichiers qui l'importent, merge_daily
        # compris), pour un gain nul ici - 2 h et 30 h laissent une marge
        # large autour de la borne des 24 h, sans le moindre risque de test
        # instable.
        maintenant = dt.datetime.now(self.timezone)
        recent = self.entree("recent.mp4",
                             (maintenant - dt.timedelta(hours=2)).isoformat())
        vieux = self.entree("vieux.mp4",
                            (maintenant - dt.timedelta(hours=30)).isoformat())
        self.ecrire_registre({"recent.mp4": recent, "vieux.mp4": vieux})

        (code, corps), = self.appeler("/api/clips?preset=today")

        self.assertEqual(code, 200)
        identites = {c["identity"] for c in corps["clips"]}
        self.assertEqual(identites, {"recent.mp4"})

    def test_depuis_jusqua_invalides_rendent_une_erreur_400(self) -> None:
        (code, corps), = self.appeler("/api/clips?depuis=pas-une-date")
        self.assertEqual(code, 400)
        self.assertIn("error", corps)

    def test_all_ignore_preset_et_plage(self) -> None:
        recent = self.entree("recent.mp4", "2026-08-15T12:00:00+00:00")
        ancien = self.entree("ancien.mp4", "2026-01-01T12:00:00+00:00")
        self.ecrire_registre({"recent.mp4": recent, "ancien.mp4": ancien})

        (code, corps), = self.appeler("/api/clips?all=1&preset=today")

        self.assertEqual(code, 200)
        identites = {c["identity"] for c in corps["clips"]}
        self.assertEqual(identites, {"recent.mp4", "ancien.mp4"})


class CacheDureeTests(BacASable):
    def test_duree_d_un_clip_ecarte_n_est_sondee_qu_une_fois(self) -> None:
        identity = "jardin/ecarte.mp4"
        media = self.paths["excluded"] / identity
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(b"\x00" * 16)
        self.ecrire_registre({
            identity: self.entree(identity, "2026-08-15T12:00:00+00:00", excluded=True),
        })

        appels = []

        def faux_probe(ffmpeg, source):
            appels.append(source)
            return 12.5

        with mock.patch.object(serve, "probe_duration", side_effect=faux_probe):
            premier = serve.collect(self.paths, self.timezone, ffmpeg="ffmpeg")
            second = serve.collect(self.paths, self.timezone, ffmpeg="ffmpeg")

        self.assertEqual(len(appels), 1, "ffmpeg -i n'aurait dû être relancé qu'une fois")
        self.assertEqual(premier["clips"][0]["duration"], 12.5)
        self.assertEqual(second["clips"][0]["duration"], 12.5)
        self.assertTrue((self.paths["thumbs"] / serve.EXCLUDED_DURATIONS).is_file())

    def test_fichier_remplace_est_re_sonde(self) -> None:
        identity = "jardin/ecarte.mp4"
        media = self.paths["excluded"] / identity
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(b"\x00" * 16)
        self.ecrire_registre({
            identity: self.entree(identity, "2026-08-15T12:00:00+00:00", excluded=True),
        })

        appels = []

        def faux_probe(ffmpeg, source):
            appels.append(source)
            return float(len(appels))

        with mock.patch.object(serve, "probe_duration", side_effect=faux_probe):
            serve.collect(self.paths, self.timezone, ffmpeg="ffmpeg")
            media.write_bytes(b"\x00" * 32)  # taille différente : empreinte changée
            resultat = serve.collect(self.paths, self.timezone, ffmpeg="ffmpeg")

        self.assertEqual(len(appels), 2, "un média remplacé doit être re-sondé")
        self.assertEqual(resultat["clips"][0]["duration"], 2.0)


if __name__ == "__main__":
    unittest.main()
