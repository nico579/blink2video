"""Essai isolé de suppression d'un clip conservé dans le cloud Blink.

Le mode par défaut ne supprime rien : il inventorie les clips de la caméra et
affiche les identifiants utilisables. Une suppression exige explicitement
``--supprimer``, ``--clip-id`` et une confirmation propre à cet identifiant.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json

import runtime

# Même amorçage que l'application, avant les imports de dépendances tierces.
runtime.bootstrap()

from aiohttp import ClientSession  # noqa: E402

import blink_auth  # noqa: E402


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera", default="jardin", help="nom exact de la caméra")
    parser.add_argument(
        "--depuis-jours",
        type=int,
        default=7,
        help="fenêtre d'inventaire cloud (défaut : 7 jours)",
    )
    parser.add_argument("--clip-id", type=int, help="identifiant cloud exact à cibler")
    parser.add_argument(
        "--supprimer",
        action="store_true",
        help="autoriser l'appel de suppression (sinon lecture seule)",
    )
    parser.add_argument(
        "--confirmer",
        metavar="TEXTE",
        help="doit valoir SUPPRIMER-<clip-id> pour autoriser l'effacement",
    )
    return parser.parse_args()


def instant(entree: dict) -> dt.datetime:
    try:
        valeur = dt.datetime.fromisoformat(str(entree["created_at"]))
    except (KeyError, TypeError, ValueError):
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    if valeur.tzinfo is None:
        valeur = valeur.replace(tzinfo=dt.timezone.utc)
    return valeur


def decrire(entree: dict) -> str:
    statut = "supprimé" if entree.get("deleted") else "présent"
    if entree.get("partial"):
        statut += ", partiel"
    return (
        f"id={entree.get('id')} | {entree.get('created_at', '?')} | "
        f"{entree.get('device_name', '?')} | {statut}"
    )


async def executer(args: argparse.Namespace) -> int:
    if args.depuis_jours < 1:
        raise ValueError("--depuis-jours doit être supérieur ou égal à 1")

    async with ClientSession() as session:
        blink = await blink_auth.connect_saved(session)
        if blink is None:
            print("Session Blink absente ou invalide ; reconnectez d'abord l'application.")
            return 2

        depuis = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.depuis_jours)
        donnees = await blink.get_videos_metadata(
            since=depuis.isoformat(),
            stop=401,
        )
        camera = args.camera.casefold().strip()
        clips = [
            entree
            for entree in donnees
            if str(entree.get("device_name") or "").casefold().strip() == camera
        ]
        clips.sort(key=instant, reverse=True)

        print(
            f"Caméra {args.camera!r} : {len(clips)} clip(s) trouvé(s) "
            f"sur les {args.depuis_jours} dernier(s) jour(s)."
        )
        for entree in clips[:20]:
            print("  " + decrire(entree))
        if len(clips) > 20:
            print(f"  ... {len(clips) - 20} autre(s) clip(s)")

        if args.clip_id is None:
            if args.supprimer:
                print("Refus : --supprimer exige --clip-id.")
                return 2
            print("Lecture seule terminée : aucun clip supprimé.")
            return 0

        cible = next(
            (entree for entree in clips if str(entree.get("id")) == str(args.clip_id)),
            None,
        )
        if cible is None:
            print(
                f"Refus : le clip {args.clip_id} n'appartient pas à la caméra "
                f"{args.camera!r} dans cette fenêtre."
            )
            return 2
        print("Cible vérifiée : " + decrire(cible))

        if cible.get("deleted"):
            print("Le service marque déjà ce clip comme supprimé.")
            return 0
        if not args.supprimer:
            print("Lecture seule terminée : aucun clip supprimé.")
            return 0

        confirmation = f"SUPPRIMER-{args.clip_id}"
        if args.confirmer != confirmation:
            print(f"Refus : ajoutez --confirmer {confirmation} pour cette cible exacte.")
            return 2

        url = (
            f"{blink.urls.base_url}/api/v1/accounts/{blink.account_id}/media/delete"
        )
        # Une seule requête : ne jamais réessayer automatiquement un effacement.
        resultat = await blink.auth.query(
            url=url,
            headers=blink.auth.header,
            reqtype="post",
            data=json.dumps({"media_list": [args.clip_id]}),
        )
        if not isinstance(resultat, dict):
            print(f"Réponse de suppression inattendue : {resultat!r}")
            return 1
        print(
            "Réponse Blink : "
            f"code={resultat.get('code')!r}, message={resultat.get('message')!r}"
        )
        if resultat.get("code") != 711:
            print("La suppression n'est pas confirmée par le service.")
            return 1
        print("Suppression confirmée par le service Blink.")
        return 0


def main() -> int:
    try:
        return asyncio.run(executer(arguments()))
    except (KeyboardInterrupt, EOFError):
        print("Opération annulée.")
        return 130
    except Exception as erreur:
        print(f"Échec : {type(erreur).__name__}: {erreur}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
