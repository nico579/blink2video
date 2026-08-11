"""Tient les README au même niveau que le code, à partir de la même table.

La liste des verbes existait en trois exemplaires : l'aide en ligne, la table
de délégation, et les README. Ils ont divergé, comme toujours dans ce cas :
« autostart » manquait dans une description, « login » et « list » n'apparaissaient
nulle part dans la liste des verbes affichée.

Une seule table fait désormais foi, `runtime.VERBES`. Ce programme en recopie le
contenu entre deux balises dans chaque README.

    python docs.py           met les README à jour
    python docs.py --check   échoue s'ils ne sont plus à jour

Le mode --check tourne en intégration continue : une dérive doit être signalée,
pas tolérée jusqu'à ce que quelqu'un s'en aperçoive.
"""

import argparse
import sys
from pathlib import Path

import runtime


BASE_DIR = Path(__file__).resolve().parent
DEBUT = "<!-- verbes:début -->"
FIN = "<!-- verbes:fin -->"


def bloc(langue: str) -> str:
    """Liste des verbes, en bloc de code, dans la langue demandée."""
    largeur = max(len(v) for v in runtime.VERBES)
    lignes = [f"{verbe:<{largeur}}   # {texte}"
              for verbe, (_, texte) in runtime.VERBES.items()]
    entete = ("Une commande, un verbe par action. `blink <verbe> --help` donne "
              "les options de chacun."
              if langue == "fr" else
              "One command, one verb per action. `blink <verb> --help` gives "
              "each one's options.")
    corps = "\n".join(f"blink {ligne}" for ligne in lignes)
    return f"{DEBUT}\n{entete}\n\n```bash\n{corps}\n```\n{FIN}"


def appliquer(verifier: bool) -> int:
    ecarts = []
    for chemin, langue in (("README.md", "en"), ("README.fr.md", "fr")):
        fichier = BASE_DIR / chemin
        texte = fichier.read_text(encoding="utf-8")
        if DEBUT not in texte or FIN not in texte:
            print(f"{chemin} : balises {DEBUT} … {FIN} absentes")
            ecarts.append(chemin)
            continue
        avant, reste = texte.split(DEBUT, 1)
        _, apres = reste.split(FIN, 1)
        attendu = avant + bloc(langue) + apres
        if attendu == texte:
            print(f"{chemin} : à jour")
            continue
        ecarts.append(chemin)
        if verifier:
            print(f"{chemin} : PÉRIMÉ, lancez « python docs.py »")
        else:
            fichier.write_text(attendu, encoding="utf-8")
            print(f"{chemin} : mis à jour")

    if verifier and ecarts:
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="échouer si les README ne sont pas à jour, sans les modifier")
    args = parser.parse_args()
    return appliquer(args.check)


if __name__ == "__main__":
    raise SystemExit(main())
