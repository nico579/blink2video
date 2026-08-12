"""Tout : contrôler l'état, rapatrier les nouveaux clips, assembler les vidéos.

C'est le verbe de l'usage courant, celui qu'on automatise. Il enchaîne les trois
autres dans l'ordre de leurs dépendances : l'état d'abord, pour qu'une panne
soit signalée même si la suite échoue, puis le téléchargement, puis
l'assemblage de ce qui vient d'arriver.

    blink2video all               une fois
    blink2video all --loop 10     toutes les dix minutes
    blink2video serve all --loop  la même chose, avec l'interface web

Pour ne pas tout faire, on ne retire pas d'étape : on nomme les verbes voulus,
« blink2video download » puis « blink2video merge ».
"""

import argparse
import subprocess
import sys

import runtime

runtime.bootstrap()

import watch


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="blink2video all",
        description=__doc__.splitlines()[0],
        epilog="Équivaut à watch, puis download, puis merge.",
    )
    parser.add_argument("--hub", default="Maison", help="nom du Sync Module")
    parser.add_argument("--camera", help="limiter à une caméra")
    parser.add_argument("--since", type=int, help="limiter aux N derniers jours")
    parser.add_argument("--timezone", default="Europe/Paris")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="montrer sans notifier, sans enregistrer l'état ni télécharger",
    )
    runtime.ajouter_boucle(parser)
    return parser.parse_args()


def main() -> int:
    from zoneinfo import ZoneInfo

    args = parse_args()
    timezone = ZoneInfo(args.timezone)

    # Pas de --no-quelque-chose : pour ne pas tout faire, on nomme les verbes
    # qu'on veut, « blink2video download » puis « blink2video merge ». Un verbe qui se
    # décrit par ce qu'il ne fait pas en fait trop.
    a_faire = ["watch", "download", "merge"]
    print("Étapes : " + ", ".join(a_faire))
    return runtime.repeter(
        lambda: watch.un_tour(args, timezone, a_faire),
        args.loop, watch.journal,
    )


if __name__ == "__main__":
    raise SystemExit(main())
