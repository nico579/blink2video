"""Une copie périmée ne doit jamais faire reculer une décision d'exclusion."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import blink_registre
import runtime


class TestsDateExclusion(unittest.TestCase):
    def test_meme_etat_conserve_la_date_la_plus_recente(self):
        for excluded in (False, True):
            ancien = {'excluded': excluded, 'excluded_at': '2026-09-07T10:10:00+00:00'}
            recent = {'excluded': excluded, 'excluded_at': '2026-09-07T10:30:00+00:00'}
            for disque, entrant in ((ancien, recent), (recent, ancien)):
                with self.subTest(excluded=excluded, disque=disque):
                    self.assertEqual(blink_registre._exclusion_a_retenir(disque, entrant),
                                     (excluded, recent['excluded_at']))

    def test_sauvegardes_retardees_ne_defont_pas_la_derniere_decision(self):
        for derniere_exclusion in (False, True):
            with self.subTest(excluded=derniere_exclusion), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                def etat(excluded, minute):
                    return {'version': 2, 'clips': {'clip': {
                        'path': 'clip.mp4', 'camera': 'Salon',
                        'created_at': '2026-09-07T09:00:00+00:00',
                        'excluded': excluded, 'excluded_at': f'2026-09-07T10:{minute}:00+00:00'}}}
                nouveau = etat(derniere_exclusion, '30')
                with mock.patch.object(runtime, 'app_dir', return_value=root):
                    blink_registre.save_download_state(root, copy.deepcopy(nouveau))
                    blink_registre.save_download_state(root, etat(derniere_exclusion, '10'))
                    blink_registre.save_download_state(root, etat(not derniere_exclusion, '20'))
                reel = blink_registre.load_download_state(root)['clips']['clip']
                self.assertEqual(reel['excluded'], derniere_exclusion)
                self.assertEqual(reel['excluded_at'], nouveau['clips']['clip']['excluded_at'])
