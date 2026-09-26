"""Agent launchd de macOS : un nom par entrée, et la remise à jour des agents
posés par une version antérieure.

Jusqu'à la 0.14, chaque agent s'appelait com.nico579.blink2video, quel que
soit son fichier : launchd identifiant un agent par ce nom, deux entrées ne
pouvaient pas cohabiter. L'agent porte désormais le nom de son fichier, comme
le veut la convention d'Apple. Un agent déjà installé est réécrit sur disque
au démarrage (nom et politique de relance, issue #31), pour prendre effet à
l'ouverture de session suivante."""

import contextlib
import io
import os
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_IMPORT_HOME = tempfile.TemporaryDirectory(prefix="blink-agent-macos-import-")
with mock.patch.dict(os.environ, {"BLINK_BOOTSTRAP": "none",
                                  "BLINK_HOME": _IMPORT_HOME.name}):
    import autostart

ANCIEN = {
    "Label": "com.nico579.blink2video",
    "ProgramArguments": ["/Applications/blink2video.app/Contents/MacOS/blink2video",
                         "start"],
    "WorkingDirectory": "/Users/joel/Library/Application Support/blink2video",
    "RunAtLoad": True,
    "KeepAlive": True,
}


class AgentMacosTests(unittest.TestCase):
    def setUp(self):
        temporaire = tempfile.TemporaryDirectory(prefix="blink-agent-macos-")
        self.addCleanup(temporaire.cleanup)
        self.maison = Path(temporaire.name)
        self.agents = self.maison / "Library/LaunchAgents"
        for correctif in (
            mock.patch.dict(os.environ, {"BLINK_HOME": str(self.maison / "etat")}),
            mock.patch.object(autostart.Path, "home", return_value=self.maison),
        ):
            correctif.start()
            self.addCleanup(correctif.stop)

    def appliquer(self, etat: str, quoi: tuple) -> list:
        """Lance _macos pour de vrai, launchctl excepté ; rend ses appels."""
        with mock.patch.object(autostart.runtime, "lancer") as lancer, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(autostart._macos(etat, False, quoi), 0)
        return [appel.args[0] for appel in lancer.call_args_list]

    def test_chaque_agent_porte_le_nom_de_son_fichier(self):
        self.appliquer("on", ("start",))
        self.appliquer("on", ("watch", "--loop"))
        fichiers = sorted(self.agents.glob("*.plist"))
        self.assertEqual([f.name for f in fichiers],
                         ["com.nico579.blink2video-start.plist",
                          "com.nico579.blink2video-watch.plist"])
        for fichier in fichiers:
            agent = plistlib.loads(fichier.read_bytes())
            self.assertEqual(agent["Label"], fichier.stem)

    def test_on_et_off_dechargent_l_agent_de_l_ancien_nom(self):
        cible = str(self.agents / "com.nico579.blink2video-start.plist")
        retirer = ["launchctl", "remove", "com.nico579.blink2video"]
        self.assertEqual(self.appliquer("on", ("start",)),
                         [retirer, ["launchctl", "load", cible]])
        self.assertEqual(self.appliquer("off", ("start",)),
                         [retirer, ["launchctl", "unload", cible]])

    def test_agent_d_une_version_anterieure_est_reecrit(self):
        self.agents.mkdir(parents=True)
        fichier = self.agents / "com.nico579.blink2video-start.plist"
        fichier.write_bytes(plistlib.dumps(ANCIEN))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(autostart.migrer_agents_macos(), [fichier])
        agent = plistlib.loads(fichier.read_bytes())
        self.assertEqual(agent, dict(ANCIEN, Label="com.nico579.blink2video-start",
                                     KeepAlive={"SuccessfulExit": False}))
        self.assertEqual(list(self.agents.glob("*.tmp")), [])

    def test_agent_ecrit_aujourd_hui_n_est_jamais_reecrit(self):
        # Sinon chaque démarrage le réécrirait.
        self.appliquer("on", ("start",))
        fichier = self.agents / "com.nico579.blink2video-start.plist"
        avant = fichier.read_bytes()
        self.assertEqual(autostart.migrer_agents_macos(), [])
        self.assertEqual(fichier.read_bytes(), avant)

    def test_fichier_illisible_ou_etranger_reste_intact(self):
        self.agents.mkdir(parents=True)
        illisible = self.agents / "com.nico579.blink2video-start.plist"
        illisible.write_bytes(b"<plist><dict><key>Label")
        etranger = self.agents / "com.nico579.lidar2map.plist"
        etranger.write_bytes(plistlib.dumps(dict(ANCIEN, Label="com.nico579.lidar2map")))
        avant = {f: f.read_bytes() for f in (illisible, etranger)}
        self.assertEqual(autostart.migrer_agents_macos(), [])
        self.assertEqual({f: f.read_bytes() for f in avant}, avant)


if __name__ == "__main__":
    unittest.main()
