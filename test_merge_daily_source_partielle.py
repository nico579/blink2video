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
            self.assertEqual(code, 1)
            fusion.assert_not_called()
            periodes.assert_not_called()
            self.assertEqual(state_path.read_bytes(), state_original)
            if quotidienne_presente:
                self.assertEqual(daily.read_bytes(), b'quotidienne-complete')
            else:
                self.assertFalse(daily.exists())

    def test_un_brut_manquant_preserve_la_journaliere_et_son_etat(self):
        self.verifier()

    def test_un_brut_invalide_preserve_la_journaliere(self):
        self.verifier(invalide=True)

    def test_aucune_journaliere_partielle_ne_peut_etre_creee(self):
        self.verifier(quotidienne_presente=False)

    def test_exclusion_expresse_autorise_la_reconstruction(self):
        self.verifier(exclu=True)
