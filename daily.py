"""Tout : contrôler l'état, rapatrier les nouveaux clips, assembler les vidéos.

C'est le verbe de l'usage courant, celui qu'on automatise. Il enchaîne les trois
autres dans l'ordre de leurs dépendances : l'état d'abord, pour qu'une panne
soit signalée même si la suite échoue, puis le téléchargement, puis
l'assemblage de ce qui vient d'arriver.

    blink all                    une fois
    blink all --loop 10          toutes les dix minutes
    blink all --loop 10 --serve  avec l'interface web, levée une fois

L'interface est une option et non une étape : ce n'est pas une tâche à répéter
mais un service, levé avant la première itération et laissé à demeure.
"""

import argparse
import subprocess
import sys

import runtime

runtime.bootstrap()

import watch


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="all",
        description=__doc__.splitlines()[0],
        epilog="Équivaut à watch, puis download, puis merge.",
    )
    parser.add_argument("--hub", default="Maison", help="nom du Sync Module")
    parser.add_argument("--camera", help="limiter à une caméra")
    parser.add_argument("--since", type=int, help="limiter aux N derniers jours")
    parser.add_argument(
        "--serve", action="store_true",
        help="lever l'interface web avant de commencer, et la laisser en place",
    )
    parser.add_argument(
        "--port", type=int, default=8765, help="port de l'interface (défaut : 8765)",
    )
    parser.add_argument("--timezone", default="Europe/Paris")
    parser.add_argument(
        "--notify", choices=("popup", "mail", "both"), default="popup",
        help="comment signaler une anomalie (défaut : une boîte de dialogue)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="montrer sans notifier, sans enregistrer l'état ni télécharger",
    )
    parser.add_argument(
        "--no-watch", action="store_true", help="ne pas contrôler l'état",
    )
    parser.add_argument(
        "--no-download", action="store_true", help="ne pas rapatrier de clips",
    )
    parser.add_argument(
        "--no-merge", action="store_true", help="ne pas assembler les vidéos",
    )
    runtime.ajouter_boucle(parser)
    return parser.parse_args()


def main() -> int:
    from zoneinfo import ZoneInfo

    args = parse_args()
    config = watch.load_config()
    timezone = ZoneInfo(args.timezone)

    a_faire = [nom for nom, retire in (("watch", args.no_watch),
                                       ("download", args.no_download),
                                       ("merge", args.no_merge)) if not retire]
    if not a_faire:
        print("Tout est désactivé, rien à faire.")
        return 1

    if args.serve:
        if watch.ensure_server(args.port):
            print(f"Interface levée sur http://127.0.0.1:{args.port}/")
        else:
            print(f"Interface déjà en fonctionnement sur le port {args.port}.")

    print("Étapes : " + ", ".join(a_faire))
    return runtime.repeter(
        lambda: watch.un_tour(args, config, timezone, a_faire),
        args.loop, watch.journal,
    )


if __name__ == "__main__":
    raise SystemExit(main())
