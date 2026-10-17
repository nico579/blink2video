"""La relance qui suit une mise à jour garde chacun de ses verbes.

Vu en vrai le 2026-10-17, à la première mise à jour par l'installateur commun
(0.23.0 vers 0.23.1) : l'assistant de nico579-commons 0.5.0 perd les arguments
vides. « serve … --trusted-host "" watch --loop 10 download … » revenait
« … --trusted-host watch --loop 10 download … » : --trusted-host avalait la
relève, et serve mourait sur un --loop qu'il ne connaît pas. Page et relève
restaient éteintes jusqu'à une relance à la main."""

import unittest
from unittest import mock

import maj
import runtime

STANDARD = ["serve", "--port", "8765", "--timezone", "Europe/Paris", "--trusted-host", "",
            "watch", "--loop", "10",
            "download", "--from", "all", "--usb-loop", "10", "--cloud-loop", "1"]
GROUPES = [["serve", "--port", "8765", "--timezone", "Europe/Paris", "--trusted-host", ""],
           ["watch", "--loop", "10"],
           ["download", "--from", "all", "--usb-loop", "10", "--cloud-loop", "1"]]


def abimee(ligne: list) -> list:
    """Ce que l'assistant de nico579-commons 0.5.0 fait d'une ligne de relance."""
    return [mot for mot in ligne if mot]


class LignesAbimeesParLAncienAssistant(unittest.TestCase):
    def test_la_ligne_intacte_ne_change_pas(self):
        self.assertEqual(runtime.decouper_verbes(STANDARD), GROUPES)

    def test_sans_supplement_la_releve_n_est_plus_avalee(self):
        # Démarrage de Windows : « start » seul.
        self.assertEqual(runtime.decouper_verbes(abimee(STANDARD)), GROUPES)

    def test_avec_le_supplement_du_raccourci(self):
        # Raccourci du bureau : « start --open-browser », greffé après le bloc fixe.
        ligne = STANDARD[:runtime.LONGUEUR_BLOC_SERVE] + ["--open-browser"] \
            + STANDARD[runtime.LONGUEUR_BLOC_SERVE:]
        self.assertEqual(runtime.decouper_verbes(abimee(ligne)),
                         [GROUPES[0] + ["--open-browser"], GROUPES[1], GROUPES[2]])

    def test_un_hote_nomme_comme_un_verbe_reste_une_valeur(self):
        # Audit I-11 : standard() fait suivre l'hôte du verbe watch.
        self.assertEqual(
            runtime.decouper_verbes(["serve", "--trusted-host", "watch", "watch", "--loop", "10"]),
            [["serve", "--trusted-host", "watch"], ["watch", "--loop", "10"]])
        self.assertEqual(runtime.decouper_verbes(["serve", "--trusted-host", "watch"]),
                         [["serve", "--trusted-host", "watch"]])


class ListeDeRelance(unittest.TestCase):
    def relance(self, compositions):
        with mock.patch.object(maj, "_compositions_en_cours", return_value=compositions):
            return maj._verbes_a_relancer()

    def test_aucun_argument_vide_n_est_confie_a_l_assistant(self):
        lignes = self.relance([GROUPES])
        self.assertEqual(len(lignes), 1)
        self.assertNotIn("", lignes[0])
        # serve retrouve de lui-même son hôte vide, valeur par défaut.
        self.assertEqual(runtime.decouper_verbes(abimee(lignes[0])),
                         [GROUPES[0][:5], GROUPES[1], GROUPES[2]])

    def test_un_hote_renseigne_est_garde(self):
        groupes = [GROUPES[0][:6] + ["nas.local"], GROUPES[1]]
        self.assertEqual(self.relance([groupes]),
                         [[mot for groupe in groupes for mot in groupe]])

    def test_rien_ne_tournait(self):
        self.assertEqual(self.relance([[]]), [["start"]])


if __name__ == "__main__":
    unittest.main()
