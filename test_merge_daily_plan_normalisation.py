"""Contrats de planification observables depuis l'orchestration de fusion."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import merge_daily as md
import test_merge_daily_sauvegarde_incrementale as fixtures


class TestsPlanNormalisation(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TestsSauvegardeIncrementale()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.args = self.fixture._args()
        self.moment = dt.datetime(2026, 9, 8, 10, tzinfo=dt.timezone.utc)

    def _clip(self, nom, jour=8):
        return self.moment.replace(day=jour), self.args.input / nom

    def _executer(self, groupes, *, connus=None, invalides=(), dimensions=None,
                  identites_indisponibles=()):
        """Les seuls effets réels sont les registres dans le dossier temporaire."""
        registre = {"version": 1, "cameras": {}, "clips": connus or {}}
        self.fixture.registry_path.write_text(json.dumps(registre), encoding="utf-8")
        (self.args.output / md.MERGE_STATE).write_text(
            json.dumps({"version": 1, "groups": {}}), encoding="utf-8")
        dimensions = dimensions or {}
        invalides = set(invalides)
        sortie = io.StringIO()
        appels = SimpleNamespace()

        def information(ffmpeg, registry, identity, created, source):
            largeur, hauteur, cadence = dimensions.get(identity, (1280, 720, 15.0))
            return md.ClipInfo(created=created, source=source, duration=5.0,
                               width=largeur, height=hauteur, fps=cadence,
                               has_audio=False)

        def normalisation(ffmpeg, timezone, registry, normalized_dir, identity,
                          info, target, key, font_path, preset, crf, force,
                          on_progress=None):
            registry["clips"][identity] = {"key": key}
            return True, "", on_progress is not None

        with contextlib.ExitStack() as pile:
            def remplacer(nom, **options):
                appel = pile.enter_context(mock.patch.object(md, nom, **options))
                setattr(appels, nom, appel)
                return appel

            remplacer("find_ffmpeg", return_value="ffmpeg-simule")
            remplacer("check_drawtext_available")
            remplacer("find_font", return_value=self.args.input / "police.ttf")
            remplacer("check_timestamp_rendering")
            remplacer("load_groups", return_value=groupes)
            remplacer("journees_a_source_indisponible",
                      return_value=(set(), set(identites_indisponibles)))
            remplacer("clip_info", side_effect=information)
            remplacer("camera_target", wraps=md.camera_target)
            remplacer("render_key", side_effect=lambda identity, *args: "rendu/" + identity)
            remplacer("valid_mp4", side_effect=lambda chemin: chemin.name not in invalides)
            remplacer("normalize_clip", side_effect=normalisation)
            remplacer("progress_printer", side_effect=lambda libelle: mock.Mock(name=libelle))
            remplacer("merge_group", return_value=(True, ""))
            remplacer("build_periods", return_value=(0, 0, 0))
            remplacer("prune_normalized", return_value=0)
            appels.travail = pile.enter_context(mock.patch.object(md.runtime, "travail"))
            # Une extraction ne doit pas introduire d'appel réel à ffmpeg.
            pile.enter_context(mock.patch.object(md.runtime, "demarrer",
                                                side_effect=AssertionError("processus inattendu")))
            pile.enter_context(contextlib.redirect_stdout(sortie))
            appels.code = md._executer(self.args)
        appels.sortie = sortie.getvalue()
        appels.registre = json.loads(self.fixture.registry_path.read_text(encoding="utf-8"))
        return appels

    def test_les_cibles_utilisent_tous_les_clips_meme_avec_deux_filtres(self):
        self.args.camera = "sAlOn"
        self.args.date = "2026-09-08"
        groupes = {
            ("Terrasse", "2026-09-08"): [self._clip("terrasse.mp4")],
            ("Salon", "2026-09-08"): [self._clip("salon.mp4")],
            ("Salon", "2026-09-07"): [self._clip("salon-hd.mp4", 7)],
        }
        resultat = self._executer(groupes, dimensions={"salon-hd.mp4": (1920, 1080, 30.0)})

        self.assertEqual(resultat.code, 0)
        self.assertEqual([appel.args[1] for appel in resultat.camera_target.call_args_list],
                         ["Salon", "Terrasse"])
        self.assertEqual([info.source.name for info in resultat.camera_target.call_args_list[0].args[2]],
                         ["salon.mp4", "salon-hd.mp4"])
        resultat.normalize_clip.assert_called_once()
        self.assertEqual(resultat.normalize_clip.call_args.args[4], "salon.mp4")
        self.assertEqual(resultat.normalize_clip.call_args.args[6], (1920, 1080, 30.0))
        self.assertEqual(set(resultat.registre["cameras"]), {"Salon", "Terrasse"})
        resultat.prune_normalized.assert_not_called()

    def test_le_plan_trie_les_groupes_et_conserve_l_ordre_interne_des_clips(self):
        groupes = {
            ("Terrasse", "2026-09-08"): [self._clip("t.mp4")],
            ("Salon", "2026-09-08"): [self._clip("b.mp4"), self._clip("a.mp4")],
            ("Salon", "2026-09-07"): [self._clip("ancien.mp4", 7)],
        }
        resultat = self._executer(groupes)

        attendus = ["ancien.mp4", "b.mp4", "a.mp4", "t.mp4"]
        self.assertEqual([appel.args[0] for appel in resultat.render_key.call_args_list], attendus)
        self.assertEqual([appel.args[4] for appel in resultat.normalize_clip.call_args_list], attendus)
        self.assertEqual([[segment.name for segment in appel.args[1]]
                          for appel in resultat.merge_group.call_args_list],
                         [["ancien.mp4"], ["b.mp4", "a.mp4"], ["t.mp4"]])

    def test_les_cles_sont_calculees_aussi_hors_selection(self):
        self.args.camera = "salon"
        self.args.date = "2026-09-08"
        self.args.preset = "fast"
        self.args.crf = 27
        groupes = {
            ("Salon", "2026-09-08"): [self._clip("retenu.mp4")],
            ("Salon", "2026-09-07"): [self._clip("ancien.mp4", 7)],
            ("Jardin", "2026-09-08"): [self._clip("ailleurs.mp4")],
        }
        resultat = self._executer(groupes)

        self.assertEqual([appel.args[0] for appel in resultat.render_key.call_args_list],
                         ["ailleurs.mp4", "ancien.mp4", "retenu.mp4"])
        for appel in resultat.render_key.call_args_list:
            self.assertEqual(appel.args[3], int(appel.args[1].created.timestamp()))
            self.assertEqual(appel.args[4:], (None, "fast", 27))
        resultat.normalize_clip.assert_called_once()
        self.assertEqual(resultat.normalize_clip.call_args.args[4], "retenu.mp4")

    def test_un_filtre_sans_correspondance_calcule_encore_cibles_et_cles(self):
        self.args.camera = "absente"
        resultat = self._executer({("Salon", "2026-09-08"): [self._clip("a.mp4")]})

        self.assertEqual(resultat.code, 0)
        self.assertIn("Aucun groupe de clips à fusionner.", resultat.sortie)
        resultat.camera_target.assert_called_once()
        resultat.render_key.assert_called_once()
        resultat.normalize_clip.assert_not_called()
        resultat.valid_mp4.assert_not_called()
        resultat.prune_normalized.assert_not_called()

    def test_la_date_filtre_exactement_et_la_camera_ignore_la_casse_unicode(self):
        self.args.camera = "STRASSE"
        self.args.date = "2026-09-08"
        resultat = self._executer({
            ("Straße", "2026-09-07"): [self._clip("avant.mp4", 7)],
            ("Straße", "2026-09-08"): [self._clip("retenu.mp4")],
            ("Strasse annexe", "2026-09-08"): [self._clip("autre.mp4")],
        })

        resultat.normalize_clip.assert_called_once()
        self.assertEqual(resultat.normalize_clip.call_args.args[4], "retenu.mp4")

    def test_force_ou_cle_perimee_evitent_la_sonde_mp4(self):
        cas = [(True, "rendu/a.mp4"), (True, "ancienne"), (False, "ancienne")]
        for force, cle in cas:
            with self.subTest(force=force, cle=cle):
                self.args.force = force
                resultat = self._executer(
                    {("Salon", "2026-09-08"): [self._clip("a.mp4")]},
                    connus={"a.mp4": {"key": cle}},
                )
                resultat.valid_mp4.assert_not_called()
                resultat.progress_printer.assert_called_once_with("[1/1]")
                self.assertIsNotNone(resultat.normalize_clip.call_args.args[-1])

    def test_un_segment_a_jour_est_verifie_et_reutilise_sans_progression(self):
        resultat = self._executer(
            {("Salon", "2026-09-08"): [self._clip("a.mp4")]},
            connus={"a.mp4": {"key": "rendu/a.mp4"}},
        )

        resultat.valid_mp4.assert_called_once_with(self.args.normalized_output.resolve() / "a.mp4")
        resultat.normalize_clip.assert_called_once()
        self.assertIsNone(resultat.normalize_clip.call_args.args[-1])
        resultat.progress_printer.assert_not_called()
        self.assertIn("0 clip(s) encodé(s), 1 réutilisé(s)", resultat.sortie)

    def test_un_segment_invalide_est_reencode_meme_si_sa_cle_est_a_jour(self):
        resultat = self._executer(
            {("Salon", "2026-09-08"): [self._clip("a.mp4")]},
            connus={"a.mp4": {"key": "rendu/a.mp4"}}, invalides={"a.mp4"},
        )

        resultat.valid_mp4.assert_called_once_with(self.args.normalized_output.resolve() / "a.mp4")
        resultat.progress_printer.assert_called_once_with("[1/1]")
        self.assertIn("1 clip(s) encodé(s), 0 réutilisé(s)", resultat.sortie)

    def test_le_total_ne_compte_que_les_identites_a_encoder(self):
        resultat = self._executer({
            ("Salon", "2026-09-08"): [self._clip("a.mp4"), self._clip("reutilise.mp4")],
            ("Terrasse", "2026-09-08"): [self._clip("b.mp4")],
        }, connus={"reutilise.mp4": {"key": "rendu/reutilise.mp4"}})

        self.assertEqual(resultat.progress_printer.call_args_list,
                         [mock.call("[1/2]"), mock.call("[2/2]")])
        preparation = [appel for appel in resultat.travail.call_args_list
                       if appel.kwargs.get("cle") == "phase.prepare_clips"]
        self.assertEqual(preparation, [
            mock.call("Préparation des clips", 0, 2, cle="phase.prepare_clips"),
            mock.call("Préparation des clips", 1, 2, cle="phase.prepare_clips"),
        ])

    def test_le_nettoyage_conserve_les_segments_utilises_et_sources_indisponibles(self):
        resultat = self._executer({
            ("Salon", "2026-09-08"): [self._clip("a.mp4")],
            ("Terrasse", "2026-09-08"): [self._clip("b.mp4")],
        }, identites_indisponibles={"c.mp4"})

        resultat.prune_normalized.assert_called_once()
        self.assertEqual(resultat.prune_normalized.call_args.args[2],
                         {self.args.normalized_output.resolve() / nom
                          for nom in ("a.mp4", "b.mp4", "c.mp4")})

    def test_un_alias_de_stockage_utilise_le_chemin_resolu_a_toutes_les_etapes(self):
        dossier_reel = self.args.normalized_output.resolve()
        alias = dossier_reel.with_name("alias-normalized")
        self.args.normalized_output = alias
        resoudre = Path.resolve

        def resoudre_alias(chemin, *args, **kwargs):
            # Simule un nom court Windows ou /var -> /private/var sur macOS,
            # sans créer de lien symbolique ni demander de privilèges.
            return dossier_reel if chemin == alias else resoudre(chemin, *args, **kwargs)

        with mock.patch.object(Path, "resolve", autospec=True, side_effect=resoudre_alias):
            resultat = self._executer(
                {("Salon", "2026-09-08"): [self._clip("a.mp4")]},
                connus={"a.mp4": {"key": "rendu/a.mp4"}},
                identites_indisponibles={"b.mp4"},
            )

        self.assertEqual(resultat.code, 0)
        resultat.valid_mp4.assert_called_once_with(dossier_reel / "a.mp4")
        self.assertEqual(resultat.normalize_clip.call_args.args[3], dossier_reel)
        self.assertEqual(resultat.merge_group.call_args.args[1], [dossier_reel / "a.mp4"])
        self.assertEqual(resultat.prune_normalized.call_args.args[0], dossier_reel)
        self.assertEqual(resultat.prune_normalized.call_args.args[2],
                         {dossier_reel / "a.mp4", dossier_reel / "b.mp4"})

    def test_un_identifiant_repete_ne_gonfle_pas_le_total_annonce(self):
        resultat = self._executer({
            ("Salon", "2026-09-08"): [self._clip("a.mp4"), self._clip("a.mp4")],
        })

        self.assertIn("Normalisation : 1 clip(s) à encoder", resultat.sortie)
        preparation = [appel for appel in resultat.travail.call_args_list
                       if appel.kwargs.get("cle") == "phase.prepare_clips"]
        self.assertTrue(preparation)
        self.assertTrue(all(appel.args[2] == 1 for appel in preparation))

    def test_sans_horodatage_aucune_police_ni_sonde_drawtext(self):
        resultat = self._executer({("Salon", "2026-09-08"): [self._clip("a.mp4")]})

        resultat.check_drawtext_available.assert_not_called()
        resultat.find_font.assert_not_called()
        resultat.check_timestamp_rendering.assert_not_called()
        self.assertIsNone(resultat.render_key.call_args.args[4])
        self.assertIsNone(resultat.normalize_clip.call_args.args[8])

    def test_avec_horodatage_la_police_est_validee_et_transmise(self):
        self.args.no_timestamp = False
        self.args.font = self.args.input / "choix.ttf"
        resultat = self._executer({("Salon", "2026-09-08"): [self._clip("a.mp4")]})

        police = self.args.input / "police.ttf"
        resultat.check_drawtext_available.assert_called_once_with("ffmpeg-simule")
        resultat.find_font.assert_called_once_with(self.args.font)
        resultat.check_timestamp_rendering.assert_called_once_with("ffmpeg-simule", police)
        self.assertEqual(resultat.render_key.call_args.args[4], police)
        self.assertEqual(resultat.normalize_clip.call_args.args[8], police)


if __name__ == "__main__":
    unittest.main()
