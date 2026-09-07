"""Tables PMT invalides : ignorer puis reprendre sur une table complète."""

import unittest

from blink_ts_demux import DemuxeurTSVideo


class TestPMT(unittest.TestCase):
    PMT = bytes.fromhex("02 b0 12 00 01 c1 00 00 e1 00 f0 00 1b e1 00 f0 00 00 00 00 00")

    def paquet(self, section):
        payload = b"\x00" + section
        adaptation = 183 - len(payload)
        return bytes([0x47, 0x40, 100, 0x30, adaptation]) + bytes(adaptation) + payload

    def test_toutes_les_troncatures_sont_ignorees_puis_la_lecture_reprend(self):
        for taille in range(len(self.PMT)):
            with self.subTest(taille=taille):
                demux = DemuxeurTSVideo()
                demux.pmt_pid = 100
                self.assertEqual(demux.alimenter(self.paquet(self.PMT[:taille])), [])
                self.assertIsNone(demux.video_pid)
                demux.alimenter(self.paquet(self.PMT))
                self.assertEqual(demux.video_pid, 256)
                video = b"\x00\x00\x01\x65\x80\x00\x00\x01\x61\x80"
                paquet = bytes([0x47, 1, 0, 0x30, 183 - len(video)])
                paquet += bytes(183 - len(video)) + video
                self.assertEqual(demux.alimenter(paquet), [(None, b"\x00\x00\x00\x01\x65\x80")])

    def test_longueurs_et_type_invalides_ne_selectionnent_pas_de_pid(self):
        for offset, valeur in ((0, 0), (2, 12), (11, 6), (16, 1)):
            with self.subTest(offset=offset):
                section = bytearray(self.PMT)
                section[offset] = valeur
                demux = DemuxeurTSVideo()
                demux._pmt(b"\x00" + section, True)
                self.assertIsNone(demux.video_pid)


if __name__ == "__main__":
    unittest.main()
