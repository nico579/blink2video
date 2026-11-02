"""Une source indisponible n'autorise pas à raccourcir les vidéos existantes."""
import contextlib
import datetime as dt
import io
import json
import unittest
from unittest import mock

import merge_daily as md
import runtime
import test_merge_daily_sauvegarde_incrementale as fixtures


class TestsSourcePartielle(unittest.TestCase):
    def verifier(self, *, invalide=False, exclu=False, quotidienne_presente=True):
        fixture = fixtures.TestsSauvegardeIncrementale()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        args = fixture._args()
        args.no_weekly = args.no_monthly = False
        source = args.input / 'a.mp4'
        source.write_bytes(fixtures.mp4_structurel())
        if invalide:
            (args.input / 'b.mp4').write_bytes(b'invalide')
        clips = {nom: {'path': nom+'.mp4', 'camera': 'Salon',
                       'created_at': '2026-09-07T10:00:00+00:00',
                       'excluded': exclu and nom == 'b'} for nom in ('a', 'b')}
        (args.input / md.DOWNLOAD_STATE).write_text(json.dumps({'clips': clips}), encoding='utf-8')
        daily = args.output / 'Salon/2026-09-07_Salon.mp4'
        daily.parent.mkdir()
        if quotidienne_presente:
            daily.write_bytes(b'quotidienne-complete')
        state_path = args.output / md.MERGE_STATE
        state_path.write_text(json.dumps({'groups': {'Salon|2026-09-07': {'fingerprint': 'ancien'}}}), encoding='utf-8')
        state_original = state_path.read_bytes()
        info = md.ClipInfo(created=dt.datetime(2026,9,7,10,tzinfo=dt.timezone.utc), source=source,
                           duration=5, width=1280, height=720, fps=15, has_audio=False)
        with mock.patch.object(md, 'find_ffmpeg', return_value='ffmpeg-simule'), \
             mock.patch.object(md, 'clip_info', return_value=info), \
             mock.patch.object(md, 'camera_target', return_value=(1280,720,15)), \
             mock.patch.object(md, 'normalize_clip', return_value=(True,'',True)), \
             mock.patch.object(md, 'merge_group', return_value=(True,'')) as fusion, \
             mock.patch.object(md, 'build_periods', return_value=(0,0,0)) as periodes, \
             mock.patch.object(runtime, 'travail'), contextlib.redirect_stdout(io.StringIO()):
            code = md._executer(args)
        if exclu:
            self.assertEqual(code, 0)
            fusion.assert_called_once()
            self.assertEqual(periodes.call_count, 2)
        else:
            # Jamais de journalière refaite avec les seuls clips restants.
            fusion.assert_not_called()
            self.assertEqual(state_path.read_bytes(), state_original)
            if quotidienne_presente:
                self.assertEqual(daily.read_bytes(), b'quotidienne-complete')
            else:
                self.assertFalse(daily.exists())
            if invalide:
                # Fichier là mais illisible : source indisponible, échec, agrégats reportés.
                self.assertEqual(code, 1)
                periodes.assert_not_called()
            else:
                # Fichier absent : effacé à la main (#118), noté comme purgé.
                # Pas un échec, et les agrégats continuent d'être faits.
                self.assertEqual(code, 0)
                self.assertEqual(periodes.call_count, 2)
                registre = json.loads((args.input / md.DOWNLOAD_STATE).read_text(encoding='utf-8'))
                self.assertTrue(registre['clips']['b'].get('purge_manuelle'))
                self.assertNotIn('purged_at', registre['clips']['a'])

    def test_un_brut_efface_a_la_main_revient_s_il_est_remis(self):
        fixture = fixtures.TestsSauvegardeIncrementale()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        racine = fixture._args().input
        etat = racine / md.DOWNLOAD_STATE
        etat.write_text(json.dumps({'clips': {'b': {
            'path': 'b.mp4', 'camera': 'Salon', 'created_at': '2026-09-07T10:00:00+00:00'}}}),
            encoding='utf-8')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(md.suivre_purges_manuelles(racine), 1)
            self.assertTrue(json.loads(etat.read_text(encoding='utf-8'))['clips']['b']['purge_manuelle'])
            (racine / 'b.mp4').write_bytes(fixtures.mp4_structurel())
            self.assertEqual(md.suivre_purges_manuelles(racine), 1)
            self.assertEqual(md.suivre_purges_manuelles(racine), 0)
        entree = json.loads(etat.read_text(encoding='utf-8'))['clips']['b']
        self.assertNotIn('purged_at', entree)
        self.assertNotIn('purge_manuelle', entree)

    def test_un_brut_efface_a_la_main_preserve_la_journaliere_sans_echec(self):
        self.verifier()

    def test_un_brut_invalide_preserve_la_journaliere(self):
        self.verifier(invalide=True)

    def test_aucune_journaliere_partielle_ne_peut_etre_creee(self):
        self.verifier(quotidienne_presente=False)

    def test_exclusion_expresse_autorise_la_reconstruction(self):
        self.verifier(exclu=True)


class TestsAgregatsProteges(unittest.TestCase):
    """Audit du 26/09/2026, B02 : une journée qui a perdu à la fois ses clips
    bruts et sa journalière protège les agrégats de sa semaine et de son
    mois, même quand la fusion est filtrée sur un autre jour (--date)."""

    LUNDI, MARDI = dt.date(2026, 9, 7), dt.date(2026, 9, 8)

    def setUp(self):
        fixture = fixtures.TestsSauvegardeIncrementale()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.args = fixture._args()
        self.args.no_weekly = self.args.no_monthly = False
        # Même semaine ISO et même mois : la précondition du scénario.
        self.assertEqual(md.period_label(self.LUNDI, 'weekly'),
                         md.period_label(self.MARDI, 'weekly'))

    def _agregat(self, dossier, period, contenu):
        label = md.period_label(self.LUNDI, period)
        chemin = dossier / 'Salon' / f'{label}_Salon.mp4'
        chemin.parent.mkdir(parents=True, exist_ok=True)
        chemin.write_bytes(contenu)
        etat = {'groups': {f'Salon|{label}': {
            'fingerprint': 'ancien', 'path': f'Salon/{label}_Salon.mp4', 'days': 2}}}
        (dossier / md.MERGE_STATE).write_text(json.dumps(etat), encoding='utf-8')
        return chemin, (dossier / md.MERGE_STATE).read_bytes()

    def test_filtre_sur_mardi_ne_raccourcit_pas_la_semaine_ni_le_mois(self):
        args = self.args
        args.date = self.MARDI.isoformat()
        # Lundi : brut disparu et journalière absente. Mardi : tout est là.
        (args.input / 'mardi.mp4').write_bytes(fixtures.mp4_structurel())
        clips = {
            'lundi': {'path': 'lundi.mp4', 'camera': 'Salon',
                      'created_at': '2026-09-07T10:00:00+00:00'},
            'mardi': {'path': 'mardi.mp4', 'camera': 'Salon',
                      'created_at': '2026-09-08T10:00:00+00:00'},
        }
        (args.input / md.DOWNLOAD_STATE).write_text(
            json.dumps({'clips': clips}), encoding='utf-8')
        journaliere = args.output / 'Salon' / '2026-09-08_Salon.mp4'
        journaliere.parent.mkdir()
        journaliere.write_bytes(fixtures.mp4_structurel())
        hebdo, etat_hebdo = self._agregat(args.weekly_output, 'weekly', b'semaine-complete')
        mensuel, etat_mensuel = self._agregat(args.monthly_output, 'monthly', b'mois-complet')
        info = md.ClipInfo(created=dt.datetime(2026, 9, 8, 10, tzinfo=dt.timezone.utc),
                           source=args.input / 'mardi.mp4', duration=5, width=1280,
                           height=720, fps=15, has_audio=False)

        with mock.patch.object(md, 'find_ffmpeg', return_value='ffmpeg-simule'), \
             mock.patch.object(md, 'clip_info', return_value=info), \
             mock.patch.object(md, 'camera_target', return_value=(1280, 720, 15)), \
             mock.patch.object(md, 'normalize_clip', return_value=(True, '', True)), \
             mock.patch.object(md, 'merge_group', return_value=(True, '')), \
             mock.patch.object(md, 'concat_videos', return_value=(True, '')) as concat, \
             mock.patch.object(runtime, 'travail'), \
             contextlib.redirect_stdout(io.StringIO()) as sortie:
            md._executer(args)

        concat.assert_not_called()
        self.assertEqual(hebdo.read_bytes(), b'semaine-complete')
        self.assertEqual(mensuel.read_bytes(), b'mois-complet')
        self.assertEqual((args.weekly_output / md.MERGE_STATE).read_bytes(), etat_hebdo)
        self.assertEqual((args.monthly_output / md.MERGE_STATE).read_bytes(), etat_mensuel)
        self.assertIn(md.msg('periode_protegee', periode='Salon|2026-W37'), sortie.getvalue())

    def test_une_periode_protegee_sans_journaliere_n_est_pas_supprimee(self):
        hebdo, etat = self._agregat(self.args.weekly_output, 'weekly', b'semaine-complete')
        cle = f"Salon|{md.period_label(self.LUNDI, 'weekly')}"
        with contextlib.redirect_stdout(io.StringIO()):
            md.build_periods('ffmpeg-simule', md.ZoneInfo('UTC'), self.args.output,
                             self.args.weekly_output, 'weekly', False, 'veryfast', 21,
                             proteges={cle})
        self.assertEqual(hebdo.read_bytes(), b'semaine-complete')
        self.assertEqual((self.args.weekly_output / md.MERGE_STATE).read_bytes(), etat)

    def test_une_periode_vide_non_protegee_est_supprimee_sans_planter(self):
        # Trouvé avec B02 : le message de ce nettoyage passait cle=... à
        # msg(cle, **valeurs) et levait TypeError, avant même d'enregistrer
        # l'état ; chaque fusion suivante plantait au même endroit.
        hebdo, _ = self._agregat(self.args.weekly_output, 'weekly', b'semaine-complete')
        with contextlib.redirect_stdout(io.StringIO()):
            md.build_periods('ffmpeg-simule', md.ZoneInfo('UTC'), self.args.output,
                             self.args.weekly_output, 'weekly', False, 'veryfast', 21)
        self.assertFalse(hebdo.exists())
        etat = json.loads((self.args.weekly_output / md.MERGE_STATE).read_text(encoding='utf-8'))
        self.assertEqual(etat['groups'], {})

    def test_une_journee_indisponible_avec_sa_journaliere_ne_protege_rien(self):
        # Le brut a disparu mais la journalière est là : l'agrégat la reprend
        # telle quelle, rien n'est perdu, la période se reconstruit.
        journaliere = self.args.output / 'Salon' / '2026-09-07_Salon.mp4'
        journaliere.parent.mkdir()
        journaliere.write_bytes(fixtures.mp4_structurel())
        indisponibles = {('Salon', self.LUNDI.isoformat())}
        self.assertEqual(md.periodes_a_proteger(indisponibles, self.args.output, 'weekly'),
                         set())
        journaliere.unlink()
        self.assertEqual(md.periodes_a_proteger(indisponibles, self.args.output, 'weekly'),
                         {'Salon|2026-W37'})
