"""Conservation par catégorie (issue #120).

Le danger n'est pas d'effacer, c'est ce que l'assemblage fait ensuite de ce
qui reste : une journalière effacée alors que ses bruts sont là revient, une
mensuelle dont les journalières ont disparu passait pour une période vide et
était supprimée, une journée à moitié purgée serait réassemblée plus courte.
Ces tests suivent donc chaque purge d'un vrai passage d'assemblage.
"""
import argparse
import contextlib
import datetime as dt
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import blink_registre
import merge_daily as md
import runtime
from test_merge_daily_sauvegarde_incrementale import mp4_structurel

AUJOURDHUI = dt.date(2026, 11, 2)
VIEUX = dt.date(2026, 9, 1)      # 62 jours
RECENT = dt.date(2026, 11, 1)    # 1 jour


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="blink-conservation-")
        self.addCleanup(tmp.cleanup)
        self.racine = Path(tmp.name)
        patch = mock.patch.dict(os.environ, {"BLINK_HOME": str(self.racine / "home")})
        patch.start()
        self.addCleanup(patch.stop)
        for nom in ("clips", "normalized", "excluded", "daily", "weekly", "monthly",
                    "direct", "snapshots", "home"):
            (self.racine / nom).mkdir()
        self.registre = {}

    def args(self, **conservation):
        valeurs = dict(md.DEFAUTS_CONSERVATION)
        valeurs.update(live_output=self.racine / "direct",
                       snapshots_output=self.racine / "snapshots")
        valeurs.update(conservation)
        return argparse.Namespace(
            input=self.racine / "clips", output=self.racine / "daily",
            weekly_output=self.racine / "weekly", monthly_output=self.racine / "monthly",
            normalized_output=self.racine / "normalized",
            excluded_output=self.racine / "excluded",
            no_weekly=True, no_monthly=False, no_timestamp=True,
            exclude=[], include=[], timezone="UTC", date=None, camera=None,
            force=False, font=None, font_size=None, font_color="white",
            box_opacity=0.55, preset="veryfast", crf=21, **valeurs)

    def clip(self, nom, jour, camera="Salon"):
        chemin = self.racine / "clips" / camera / f"{nom}.mp4"
        chemin.parent.mkdir(parents=True, exist_ok=True)
        chemin.write_bytes(mp4_structurel())
        self.registre[nom] = {"path": f"{camera}/{nom}.mp4", "camera": camera,
                              "created_at": f"{jour.isoformat()}T10:00:00+00:00"}
        (self.racine / "clips" / md.DOWNLOAD_STATE).write_text(
            json.dumps({"version": 2, "clips": self.registre}), encoding="utf-8")
        return chemin

    def video(self, dossier, nom, camera="Salon", decalage=0):
        chemin = self.racine / dossier / camera / f"{nom}_{camera}.mp4"
        chemin.parent.mkdir(parents=True, exist_ok=True)
        chemin.write_bytes(mp4_structurel())
        # Plus récente que tout ce qui précède, comme une vraie vidéo assemblée.
        instant = time.time() + 10 + decalage
        os.utime(chemin, (instant, instant))
        return chemin

    def purger(self, **conservation):
        with contextlib.redirect_stdout(io.StringIO()):
            return md.purger_anciens(self.args(**conservation), md.ZoneInfo("UTC"),
                                     aujourdhui=AUJOURDHUI)

    def lire_registre(self):
        return json.loads((self.racine / "clips" / md.DOWNLOAD_STATE)
                          .read_text(encoding="utf-8"))["clips"]


class Bruts(Base):
    def test_journee_ancienne_purgee_entiere_et_marquee_recente_gardee(self):
        a, b = self.clip("a", VIEUX), self.clip("b", VIEUX)
        recent = self.clip("c", RECENT)
        self.video("daily", VIEUX.isoformat())
        self.assertEqual(self.purger(keep_clips_days=30), 2)
        self.assertFalse(a.exists() or b.exists())
        self.assertTrue(recent.exists())
        registre = self.lire_registre()
        self.assertTrue(registre["a"]["purged_at"] and registre["b"]["purged_at"])
        self.assertNotIn("purged_at", registre["c"])

    def test_sans_journaliere_les_bruts_restent(self):
        a = self.clip("a", VIEUX)
        self.assertEqual(self.purger(keep_clips_days=30), 0)
        self.assertTrue(a.exists())

    def test_journaliere_plus_ancienne_qu_un_clip_les_bruts_restent(self):
        """Elle ne contient pas le clip arrivé après elle."""
        self.video("daily", VIEUX.isoformat(), decalage=-100)
        a = self.clip("a", VIEUX)
        self.assertEqual(self.purger(keep_clips_days=30), 0)
        self.assertTrue(a.exists())

    def test_sans_assemblage_les_bruts_suivent_la_duree(self):
        a = self.clip("a", VIEUX)
        self.assertEqual(self.purger(keep_clips_days=30, purge_only=True), 1)
        self.assertFalse(a.exists())

    def test_le_marqueur_survit_a_une_copie_de_telechargeur_perimee(self):
        self.clip("a", VIEUX)
        perimee = {"version": 2, "clips": json.loads(json.dumps(self.registre))}
        self.video("daily", VIEUX.isoformat())
        self.purger(keep_clips_days=30)
        with mock.patch.object(blink_registre.atomique, "ecrire_json",
                               wraps=blink_registre.atomique.ecrire_json):
            blink_registre._ecrire_registre(self.racine / "clips" / md.DOWNLOAD_STATE, perimee)
        self.assertTrue(self.lire_registre()["a"]["purged_at"])


class Agregats(Base):
    def test_journaliere_seulement_apres_ses_bruts_et_sa_mensuelle(self):
        self.clip("a", VIEUX)
        journaliere = self.video("daily", VIEUX.isoformat())
        self.assertEqual(self.purger(keep_daily_days=30), 0, "bruts encore là")
        self.purger(keep_clips_days=30)
        self.assertEqual(self.purger(keep_daily_days=30), 0, "pas de mensuelle")
        self.video("monthly", "2026-09", decalage=5)
        self.assertEqual(self.purger(keep_daily_days=30), 1)
        self.assertFalse(journaliere.exists())

    def test_mensuelle_seulement_sans_journaliere(self):
        mensuelle = self.video("monthly", "2026-09")
        journaliere = self.video("daily", VIEUX.isoformat())
        self.assertEqual(self.purger(keep_monthly_days=30), 0)
        journaliere.unlink()
        self.assertEqual(self.purger(keep_monthly_days=30), 1)
        self.assertFalse(mensuelle.exists())

    def test_fin_de_periode(self):
        self.assertEqual(md.fin_de_periode("2026-02", "monthly"), dt.date(2026, 2, 28))
        self.assertEqual(md.fin_de_periode("2026-12", "monthly"), dt.date(2026, 12, 31))
        self.assertEqual(md.fin_de_periode("2026-W01", "weekly"), dt.date(2026, 1, 4))
        self.assertIsNone(md.fin_de_periode("n'importe", "monthly"))


class DirectsEtPhotos(Base):
    def test_d_apres_la_date_du_fichier(self):
        ancien = self.racine / "direct" / "Salon" / "2026-09" / "x.mp4"
        neuf = self.racine / "snapshots" / "Salon" / "y.jpg"
        for chemin in (ancien, neuf):
            chemin.parent.mkdir(parents=True)
            chemin.write_bytes(b"x")
        vieux = time.time() - 40 * 86400
        os.utime(ancien, (vieux, vieux))
        self.assertEqual(self.purger(keep_live_days=30, keep_snapshots_days=30), 1)
        self.assertFalse(ancien.exists())
        self.assertFalse(ancien.parent.exists(), "dossier vidé retiré")
        self.assertTrue(neuf.exists())


class PassageApresPurge(Base):
    """La purge suivie d'un vrai _executer : la journalière et la mensuelle
    restent, aucun échec, aucune reconstruction de la journée purgée."""

    def test_journaliere_et_mensuelle_intactes_passage_reussi(self):
        self.clip("a", VIEUX)
        journaliere = self.video("daily", VIEUX.isoformat())
        mensuelle = self.video("monthly", "2026-09", decalage=5)
        contenu = mensuelle.read_bytes()
        # La mensuelle est connue de l'état d'assemblage : c'est par là qu'une
        # période sans journalière était supprimée comme « vide ».
        (self.racine / "monthly" / md.MERGE_STATE).write_text(json.dumps({
            "version": 1, "groups": {"Salon|2026-09": {
                "fingerprint": "x", "path": "Salon/2026-09_Salon.mp4", "days": 1}}}),
            encoding="utf-8")
        # VIEUX a plus de 30 jours à la vraie date : pas d'horloge simulée ici.
        args = self.args(keep_clips_days=30, keep_daily_days=30)
        with mock.patch.object(md, "find_ffmpeg", return_value="ffmpeg-simule"), \
                mock.patch.object(md, "merge_group") as fusion, \
                mock.patch.object(md, "concat_videos") as concat, \
                mock.patch.object(runtime, "travail"), \
                contextlib.redirect_stdout(io.StringIO()) as sortie:
            premier = md._executer(args)
            second = md._executer(args)
        self.assertEqual((premier, second), (0, 0), sortie.getvalue())
        fusion.assert_not_called()
        concat.assert_not_called()
        self.assertFalse(journaliere.exists(), "journalière purgée après ses bruts")
        self.assertEqual(mensuelle.read_bytes(), contenu)
        self.assertEqual(md.journees_purgees(args.input, md.ZoneInfo("UTC")),
                         {("Salon", VIEUX.isoformat())})


class Reglages(Base):
    def test_ordre(self):
        ordre = runtime.ordre_conservation_invalide
        self.assertIsNone(ordre({}))
        self.assertIsNone(ordre({"conservation_clips": 30}))
        self.assertIsNone(ordre({"conservation_clips": 30, "conservation_jour": 90,
                                 "conservation_mois": 90}))
        self.assertEqual(ordre({"conservation_jour": 90}),
                         ("conservation_jour", "conservation_clips"))
        self.assertEqual(ordre({"conservation_clips": 90, "conservation_jour": 30}),
                         ("conservation_jour", "conservation_clips"))
        self.assertEqual(ordre({"conservation_clips": 30, "conservation_mois": 365}),
                         ("conservation_mois", "conservation_jour"))

    def test_aller_retour_et_ligne_de_commande(self):
        defaut = runtime.lire_reglages()
        self.assertEqual([defaut[champ] for champ in runtime.CONSERVATION], [0] * 6)
        self.assertNotIn("--keep-clips-days", runtime.options_fusion(defaut))
        runtime.ecrire_reglages(**{**defaut, "conservation_clips": 30,
                                   "conservation_photos": 7})
        relu = runtime.lire_reglages()
        self.assertEqual((relu["conservation_clips"], relu["conservation_photos"]), (30, 7))
        options = runtime.options_fusion(relu)
        self.assertIn("--keep-clips-days", options)
        self.assertEqual(options[options.index("--keep-snapshots-days") + 1], "7")

    def test_assemblage_coupe_la_boucle_ne_fait_que_purger(self):
        defaut = runtime.lire_reglages()
        runtime.ecrire_reglages(**{**defaut, "merge_jour": False, "conservation_photos": 7})
        commande = " ".join(map(str, runtime.standard()))
        self.assertIn("merge --loop 5 --purge-only", commande)


if __name__ == "__main__":
    unittest.main()
