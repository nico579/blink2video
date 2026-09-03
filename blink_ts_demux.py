"""Demultiplexeur MPEG-TS minimal : assez de ISO/IEC 13818-1 pour isoler
le flux elementaire H.264 depuis un flux Blink, sans passer par PyAV.

PAT (PID 0) -> PID du PMT -> PMT -> PID video (stream_type 0x1B = H.264)
-> reassemblage en un flux elementaire continu (concatenation des charges
utiles PES sur ce PID, entete PES saute a chaque nouveau paquet PES, son
PTS retenu au passage - cf. plus bas) -> decoupage en NAL units des que
leur fin est connue (prochain start code vu), sans attendre qu'un paquet
PES ou une image entiere soit complet - une image-cle peut peser plusieurs
dizaines de Ko, largement plus qu'un seul paquet TS (188 octets), alors que
SPS/PPS tiennent en une poignee d'octets tout au debut.

Le PTS de chaque paquet PES (ITU-T H.222.0, 2.4.3.7) est extrait et associe
aux NAL units qui en proviennent : un horodatage improvise a la reception
(plutot que celui, reel, encode par la camera a la capture) rend la lecture
saccadee des que le reseau livre par rafales plutot qu'a cadence reguliere
(constate en reel, 2026-09-03) - le recepteur perd alors l'information de
rythme dont il a besoin pour lisser l'affichage. Commodite : le PTS MPEG
est deja cadence a 90 kHz, la meme horloge que WebRTC utilise pour la
video RTP - aucune conversion a faire, juste le lire correctement."""

from typing import Optional


class DemuxeurTSVideo:
    def __init__(self):
        self.pmt_pid = None
        self.video_pid = None
        self._flux_elementaire = bytearray()
        self._reste_ts = b""
        # [(position_dans_flux_elementaire, pts), ...], position croissante.
        self._marques_pts: list = []

    def alimenter(self, data: bytes) -> list:
        """Digere un bloc quelconque (0, 1 ou plusieurs paquets TS de 188
        octets, potentiellement mal aligne). Renvoie les (pts_ou_None,
        nal_bytes) nouvellement disponibles, dans l'ordre - nal_bytes en
        Annexe B, start code 4 octets inclus."""
        data = self._reste_ts + data
        i, n = 0, len(data)
        while i + 188 <= n:
            if data[i] != 0x47:
                j = data.find(b"\x47", i + 1)
                i = n if j == -1 else j
                continue
            self._paquet(data[i:i + 188])
            i += 188
        self._reste_ts = data[i:]
        return self._extraire_nal_completes()

    def _paquet(self, paquet: bytes) -> None:
        pusi = bool(paquet[1] & 0x40)
        pid = ((paquet[1] & 0x1F) << 8) | paquet[2]
        adaptation = (paquet[3] >> 4) & 0x3
        if adaptation not in (0x1, 0x3):
            return
        offset = 4 if adaptation == 0x1 else 5 + paquet[4]
        if offset >= len(paquet):
            return
        payload = paquet[offset:]

        if pid == 0x0000:
            self._pat(payload, pusi)
        elif self.pmt_pid is not None and pid == self.pmt_pid:
            self._pmt(payload, pusi)
        elif self.video_pid is not None and pid == self.video_pid:
            if pusi:
                # Nouveau paquet PES : ses premiers octets sont son entete
                # (00 00 01 + stream_id + longueur + flags + longueur des
                # donnees optionnelles), a sauter avant d'atteindre les
                # donnees elementaires H.264 elles-memes. Le PTS, quand
                # present, y est aussi.
                if len(payload) >= 9 and payload[0:3] == b"\x00\x00\x01":
                    pts = self._pts_depuis_entete_pes(payload)
                    if pts is not None:
                        self._marques_pts.append(
                            (len(self._flux_elementaire), pts)
                        )
                    self._flux_elementaire += payload[9 + payload[8]:]
            else:
                self._flux_elementaire += payload

    @staticmethod
    def _pts_depuis_entete_pes(payload: bytes) -> Optional[int]:
        """PTS brut (33 bits, horloge 90 kHz) depuis l'entete PES optionnel,
        si le drapeau PTS_DTS_flags l'annonce present (ITU-T H.222.0,
        2.4.3.7, tableau 2-21)."""
        if len(payload) < 14:
            return None
        pts_dts_flags = (payload[7] >> 6) & 0x3
        if pts_dts_flags == 0:
            return None
        b = payload[9:14]
        return (
            ((b[0] >> 1) & 0x07) << 30
            | b[1] << 22
            | (b[2] >> 1) << 15
            | b[3] << 7
            | (b[4] >> 1)
        )

    @staticmethod
    def _section(payload: bytes, pusi: bool):
        if not pusi or not payload:
            return None
        pointeur = payload[0]
        section = payload[1 + pointeur:]
        if len(section) < 3:
            return None
        section_length = ((section[1] & 0x0F) << 8) | section[2]
        return section, section_length

    def _pat(self, payload: bytes, pusi: bool) -> None:
        trouve = self._section(payload, pusi)
        if trouve is None:
            return
        section, section_length = trouve
        i, fin = 8, 3 + section_length - 4  # -4 : CRC32 en fin de section
        while i + 4 <= fin and i + 4 <= len(section):
            program_number = (section[i] << 8) | section[i + 1]
            pid = ((section[i + 2] & 0x1F) << 8) | section[i + 3]
            if program_number != 0 and self.pmt_pid is None:
                self.pmt_pid = pid
            i += 4

    def _pmt(self, payload: bytes, pusi: bool) -> None:
        if self.video_pid is not None:
            return
        trouve = self._section(payload, pusi)
        if trouve is None:
            return
        section, section_length = trouve
        program_info_length = ((section[10] & 0x0F) << 8) | section[11]
        i, fin = 12 + program_info_length, 3 + section_length - 4
        while i + 5 <= fin and i + 5 <= len(section):
            stream_type = section[i]
            elementary_pid = ((section[i + 1] & 0x1F) << 8) | section[i + 2]
            es_info_length = ((section[i + 3] & 0x0F) << 8) | section[i + 4]
            if stream_type == 0x1B and self.video_pid is None:
                self.video_pid = elementary_pid
            i += 5 + es_info_length

    def _pts_a(self, position: int) -> Optional[int]:
        """Dernier PTS marque a une position <= `position` (sans le
        consommer : plusieurs NAL units de la meme image le partagent)."""
        pts = None
        for p, v in self._marques_pts:
            if p <= position:
                pts = v
            else:
                break
        return pts

    def _extraire_nal_completes(self) -> list:
        """Emet toute NAL unit dont la fin est connue (un prochain start
        code a ete vu) avec son PTS, garde la derniere - potentiellement
        encore incomplete - en tampon pour le prochain appel."""
        buf = self._flux_elementaire
        premier = buf.find(b"\x00\x00\x01")
        if premier == -1:
            return []
        resultats = []
        debut_nal = premier + 3
        suivant = buf.find(b"\x00\x00\x01", debut_nal)
        while suivant != -1:
            fin = suivant
            # start code a 4 octets : le 0x00 juste avant appartient au
            # separateur, pas a cette NAL unit.
            if fin > debut_nal and buf[fin - 1] == 0:
                fin -= 1
            if fin > debut_nal:
                pts = self._pts_a(debut_nal - 3)
                resultats.append(
                    (pts, bytes(b"\x00\x00\x00\x01" + buf[debut_nal:fin]))
                )
            debut_nal = suivant + 3
            suivant = buf.find(b"\x00\x00\x01", debut_nal)
        limite = debut_nal - 3
        del buf[:limite]
        # Garder aussi le dernier marqueur avant `limite`, replace a 0 : il
        # reste le PTS en vigueur pour la NAL retenue (potentiellement
        # incomplete, en tete du tampon apres coupure). Sans lui, une NAL
        # trop grosse pour completer en un seul alimenter() (une image-cle
        # depasse largement 65536 octets) perdait son PTS des que ce
        # marqueur passait, a tort, pour perime (constate en reel : pile
        # les plus grosses images, jamais les petites qui completent d'un
        # coup - lecture saccadee, 2026-09-03).
        report = None
        gardees = []
        for p, v in self._marques_pts:
            if p < limite:
                report = v
            else:
                gardees.append((p - limite, v))
        if report is not None:
            gardees.insert(0, (0, report))
        self._marques_pts = gardees
        return resultats


def type_nal(nal_avec_start_code: bytes) -> int:
    """nal_unit_type (5 bits bas) du premier octet apres le start code."""
    i = 3 if nal_avec_start_code[2] == 1 else 4
    return nal_avec_start_code[i] & 0x1F
