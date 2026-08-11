import argparse
import asyncio
import contextlib
import datetime as dt
import getpass
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Avant tout import de dépendance : c'est ici qu'un environnement isolé
# est préparé et le programme relancé dedans si nécessaire.
import runtime

runtime.bootstrap()

from aiohttp import ClientSession

from blinkpy.auth import Auth, BlinkTwoFARequiredError
from blinkpy.blinkpy import Blink


CONFIG = Path("blink_auth.json")
OUTPUT = Path("Blink_Clips")
STATE_FILENAME = ".blink_download_state.json"


HUB_LOCK = Path(".blink_hub.lock")


@contextlib.contextmanager
def hub_lock(owner: str, stale_after: int = 600):
    """Réserve le Sync Module le temps d'une opération, entre processus.

    Le module ne traite qu'une commande à la fois et refuse les suivantes avec
    « System is busy ». À l'intérieur d'un même programme un verrou mémoire
    suffit, mais la surveillance, l'interface et la ligne de commande sont trois
    processus distincts : il faut donc une marque sur disque.

    Un verrou oublié après un plantage bloquerait tout, d'où deux garde-fous :
    on ignore une marque dont le processus n'existe plus, et toute marque plus
    vieille que `stale_after` secondes. Mieux vaut un conflit rare qu'un outil
    définitivement bloqué par un fichier."""
    now = time.time()
    existing = None
    try:
        existing = json.loads(HUB_LOCK.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing = None

    if existing:
        age = now - float(existing.get("at") or 0)
        if age < stale_after and _process_alive(int(existing.get("pid") or 0)):
            raise BusyError(
                f"le module est déjà occupé par « {existing.get('owner')} » "
                f"depuis {int(age)} s"
            )

    HUB_LOCK.write_text(
        json.dumps({"owner": owner, "pid": os.getpid(), "at": now}), encoding="utf-8"
    )
    try:
        yield
    finally:
        try:
            current = json.loads(HUB_LOCK.read_text(encoding="utf-8"))
            if int(current.get("pid") or 0) == os.getpid():
                HUB_LOCK.unlink(missing_ok=True)
        except (OSError, json.JSONDecodeError):
            pass


class BusyError(RuntimeError):
    """Le Sync Module est déjà réservé par une autre opération."""


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    result = runtime.lancer(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, errors="replace", check=False,
    )
    return str(pid) in (result.stdout or "")


def load_saved_session() -> dict | None:
    """Charge une session Blink sans jamais réutiliser un mot de passe stocké."""
    if not CONFIG.exists():
        return None

    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"Session illisible ({error}). Une nouvelle connexion est nécessaire.")
        return None

    if not data.get("refresh_token"):
        return None

    # Auth.startup() exige ces clés, même lorsqu'un refresh_token est présent.
    # Une chaîne vide évite de conserver le mot de passe sur disque.
    data["username"] = data.get("username", "")
    data["password"] = ""
    return data


def ask_credentials() -> dict:
    """Demande les identifiants dans le terminal."""
    print("\nConnexion au compte Blink")
    username = input("Adresse e-mail : ").strip()
    password = getpass.getpass("Mot de passe (non affiché) : ")
    return {"username": username, "password": password}


def make_blink(session: ClientSession, login_data: dict) -> Blink:
    """Construit un client Blink avec un flux d'authentification explicite."""
    blink = Blink(session=session)
    blink.auth = Auth(login_data, no_prompt=True, session=session)
    return blink


async def prompt_2fa_code(attempt: int) -> str:
    """Demande le code de vérification dans le terminal."""
    return input("Code Blink : ").strip()


async def finish_login(blink: Blink, ask_code=None) -> bool:
    """Démarre Blink et termine éventuellement la vérification en deux étapes.

    `ask_code` permet de poser la question ailleurs que dans le terminal :
    serve.py y branche un formulaire de navigateur. C'est une coroutine pour
    que l'attente du code n'immobilise pas la boucle d'événements pendant que
    la session Blink reste ouverte."""
    try:
        connected = await blink.start()
    except BlinkTwoFARequiredError:
        ask_code = ask_code or prompt_2fa_code
        print("\nUn code de vérification Blink vient d'être envoyé.")
        for attempt in range(3):
            code = (await ask_code(attempt) or "").strip()
            if code and await blink.send_2fa_code(code):
                return True
            remaining = 2 - attempt
            if remaining:
                print(f"Code refusé. {remaining} tentative(s) restante(s).")
        return False

    return bool(connected)


async def connect_saved(session: ClientSession):
    """Ouvre une session Blink à partir du fichier enregistré, sans rien demander.

    Distinct de `connect`, qui se rabat sur le terminal quand la session n'est
    plus valable : derrière un serveur il n'y a personne pour répondre, mieux
    vaut renvoyer None et laisser l'appelant proposer une reconnexion."""
    saved = load_saved_session()
    if not saved:
        return None
    blink = make_blink(session, saved)
    if not await finish_login(blink, ask_code=_no_code):
        return None
    save_session(blink)
    return blink


async def _no_code(attempt: int) -> str:
    """Refuse la vérification en deux étapes hors session interactive."""
    return ""


async def login(session: ClientSession, username: str, password: str, ask_code=None):
    """Ouvre une session Blink à partir d'identifiants déjà recueillis.

    Voie d'entrée de serve.py, qui les collecte dans le navigateur. Le mot de
    passe ne sert qu'ici : `save_session` ne l'écrit jamais sur disque."""
    blink = make_blink(session, {"username": username, "password": password})
    if not await finish_login(blink, ask_code):
        return None
    save_session(blink)
    return blink


def save_session(blink: Blink) -> None:
    """Sauvegarde les jetons de session, mais jamais le mot de passe Blink."""
    data = dict(blink.auth.login_attributes)
    data["password"] = ""

    temporary = CONFIG.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temporary.replace(CONFIG)


async def connect(session: ClientSession) -> Blink | None:
    """Ouvre une session Blink, avec reconnexion interactive en dernier recours."""
    saved_session = load_saved_session()

    if saved_session:
        print(f"Réutilisation de la session enregistrée dans {CONFIG}...")
        blink = make_blink(session, saved_session)
        connected = await finish_login(blink)
        if not connected:
            print("La session enregistrée n'est plus valide.")
            blink = make_blink(session, ask_credentials())
            connected = await finish_login(blink)
    else:
        blink = make_blink(session, ask_credentials())
        connected = await finish_login(blink)

    if not connected:
        return None

    save_session(blink)
    return blink


def select_sync_modules(blink: Blink, requested_name: str | None):
    """Sélectionne tous les hubs, ou celui demandé par son nom."""
    modules = list(blink.sync.items())
    if not requested_name:
        return modules

    selected = [
        (name, sync)
        for name, sync in modules
        if name.casefold().strip() == requested_name.casefold().strip()
    ]
    if not selected:
        available = ", ".join(name for name, _ in modules) or "aucun"
        raise ValueError(
            f"Sync Module introuvable : {requested_name!r}. Disponible(s) : {available}."
        )
    return selected


async def read_local_manifest(sync) -> list:
    """Demande au Sync Module la liste à jour de ses clips USB."""
    storage = sync._local_storage  # blinkpy n'expose pas encore d'accesseur public.
    if not storage.get("compatible"):
        raise RuntimeError("ce hub n'est pas compatible avec le stockage local")
    if not storage.get("enabled"):
        raise RuntimeError("le stockage local n'est pas activé sur ce hub")
    if not sync.local_storage:
        raise RuntimeError(
            "le stockage local n'est pas actif (clé USB absente/non reconnue, "
            "ou clips enregistrés dans le cloud)"
        )

    print("  Lecture du manifeste USB du hub...")
    # Le Sync Module ne traite qu'une commande à la fois et répond « System is
    # busy » (code 307) tant qu'il n'a pas fini la précédente : un direct qui
    # vient de se fermer, ou une autre demande de manifeste, suffisent. Ce
    # refus est temporaire, pas une panne, d'où la reprise avec une attente qui
    # s'allonge plutôt qu'un abandon immédiat.
    delays = (3, 8, 15, 25)
    for attempt, delay in enumerate((*delays, None), start=1):
        if await sync.update_local_storage_manifest():
            return list(storage["manifest"])
        if delay is None:
            break
        print(f"  Module occupé, nouvelle tentative dans {delay} s "
              f"({attempt}/{len(delays)})...")
        await asyncio.sleep(delay)

    raise RuntimeError(
        "Blink n'a pas renvoyé le manifeste du stockage local après "
        f"{len(delays) + 1} tentatives (module resté occupé)"
    )


def clip_datetime_utc(clip) -> dt.datetime:
    """Normalise l'horodatage Blink en UTC."""
    created = clip.created_at
    if created.tzinfo is None:
        return created.replace(tzinfo=dt.timezone.utc)
    return created.astimezone(dt.timezone.utc)


def filter_clips(clips: list, camera: str | None, since_days: int | None) -> list:
    """Applique les filtres demandés, puis trie du plus ancien au plus récent."""
    selected = clips
    if camera:
        selected = [clip for clip in selected if clip.name.casefold() == camera.casefold()]
    if since_days is not None:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=since_days)
        selected = [clip for clip in selected if clip_datetime_utc(clip) >= cutoff]
    return sorted(selected, key=clip_datetime_utc)


def reported_bytes(value) -> int:
    """Convertit en octets la taille du manifeste Blink, exprimée en Kio."""
    try:
        return int(value) * 1024
    except (TypeError, ValueError):
        return 0


def human_size(size: int) -> str:
    """Affiche une taille lisible."""
    amount = float(size)
    for unit in ("o", "Kio", "Mio", "Gio"):
        if amount < 1024 or unit == "Gio":
            return f"{amount:.0f} {unit}" if unit == "o" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{size} o"


def safe_name(value: str) -> str:
    """Produit un composant de chemin sûr à partir du nom d'une caméra."""
    cleaned = re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE).strip("._")
    return cleaned or "camera"


def target_path(output: Path, clip) -> Path:
    """Construit un nom stable et unique pour un clip local."""
    created = clip_datetime_utc(clip)
    camera = safe_name(clip.name)
    month = created.strftime("%Y-%m")
    filename = f"{created:%Y-%m-%d_%H-%M-%SZ}_{camera}_{clip.id}.mp4"
    return output / camera / month / filename


async def download_clip(blink: Blink, clip, target: Path, overwrite: bool) -> str:
    """Prépare puis télécharge un clip, sans jamais le supprimer du hub."""
    if target.exists() and target.stat().st_size > 0 and not overwrite:
        return "skipped"

    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    partial.unlink(missing_ok=True)

    try:
        prepared = await clip.prepare_download(blink)
        if not prepared or not await clip.download_video(blink, str(partial)):
            return "failed"
        if not partial.exists() or partial.stat().st_size == 0:
            return "failed"
        partial.replace(target)
        return "downloaded"
    finally:
        partial.unlink(missing_ok=True)


def load_download_state(output: Path) -> dict:
    """Charge le registre incrémental placé dans le dossier de destination."""
    state_file = output / STATE_FILENAME
    if not state_file.exists():
        return {"version": 1, "clips": {}}
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        if state.get("version") != 1 or not isinstance(state.get("clips"), dict):
            raise ValueError("format inconnu")
        return state
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"  ! État incrémental illisible ({error}); les fichiers existants seront vérifiés.")
        return {"version": 1, "clips": {}}


def save_download_state(output: Path, state: dict) -> None:
    """Enregistre atomiquement le registre incrémental."""
    output.mkdir(parents=True, exist_ok=True)
    state_file = output / STATE_FILENAME
    temporary = state_file.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(state_file)


def state_key(sync, clip) -> str:
    """Identifie un enregistrement par ce qui ne bouge pas : la caméra et l'instant.

    L'identifiant numérique du manifeste ne convient pas, bien qu'il soit
    tentant. Il est stable d'une lecture à l'autre, mais le Sync Module
    réindexe son stockage de temps en temps et renumérote tout : le même
    enregistrement réapparaît alors sous un nouvel identifiant, donc comme un
    clip inédit. Il est retéléchargé, et une exclusion posée sur l'ancien
    identifiant ne s'y applique plus.

    Une caméra ne peut pas commencer deux enregistrements dans la même seconde :
    le couple caméra + instant est donc une identité fiable."""
    created = clip_datetime_utc(clip).isoformat()
    return f"{sync.sync_id}:{safe_name(clip.name)}:{created}"


def remember_download(state: dict, sync, hub_name: str, clip, output: Path, target: Path) -> None:
    """Marque un clip comme acquis uniquement lorsque son fichier existe."""
    state["clips"][state_key(sync, clip)] = {
        "hub": hub_name,
        "camera": clip.name,
        "created_at": clip_datetime_utc(clip).isoformat(),
        "path": target.relative_to(output).as_posix(),
        "bytes": target.stat().st_size,
    }


def is_downloaded(state: dict, sync, clip, target: Path) -> bool:
    """Un clip est acquis si le registre et le fichier non vide sont présents.

    Exception : un clip marqué « exclu » compte comme acquis même sans fichier.
    C'est une pierre tombale, posée par `merge_daily.py --exclude`, qui dit
    « écarté volontairement, ne pas rapatrier » ; sans elle, supprimer le
    fichier ne ferait que provoquer un nouveau téléchargement. Même principe
    que le fichier d'archive de yt-dlp (--download-archive, hérité de
    youtube-dl) : on retient l'identifiant, pas la présence du média."""
    entry = state["clips"].get(state_key(sync, clip))
    if isinstance(entry, dict) and entry.get("excluded"):
        return True
    return (
        entry is not None
        and target.exists()
        and target.stat().st_size > 0
    )


def print_clip_summary(clips: list) -> None:
    """Affiche un résumé du manifeste, sans URL ni donnée sensible."""
    total_size = sum(reported_bytes(clip.size) for clip in clips)
    print(f"  {len(clips)} clip(s), volume annoncé : environ {human_size(total_size)}")
    if clips:
        first = clip_datetime_utc(clips[0]).astimezone()
        last = clip_datetime_utc(clips[-1]).astimezone()
        print(f"  Période : {first:%Y-%m-%d %H:%M:%S %Z} -> {last:%Y-%m-%d %H:%M:%S %Z}")
        cameras = sorted({clip.name for clip in clips}, key=str.casefold)
        print(f"  Caméra(s) : {', '.join(cameras)}")


def parse_args() -> argparse.Namespace:
    programme = Path(sys.argv[0]).stem or "blink"
    parser = argparse.ArgumentParser(
        prog=programme,
        # Les verbes vont dans la description, pas dans un groupe d'arguments :
        # les déclarer à argparse en ferait de faux positionnels, qui
        # pollueraient la ligne d'usage et fausseraient l'analyse.
        description=(
            "Gestion des caméras Blink depuis un ordinateur : direct, "
            "armement, archive horodatée.\n\nVerbes :\n"
            + "".join(f"  {nom:11} {verbe.fr}\n"
                      for nom, verbe in runtime.VERBES.items())
            + "\n  <verbe> --help donne les options de chacun."
        ),
        # Les exemples suivent l'ordre dans lequel on rencontre les verbes :
        # se connecter, regarder ce qu'il y a, récupérer, assembler, visionner,
        # puis automatiser. C'est un parcours, pas un catalogue.
        epilog="Premiers pas :\n" + "\n".join(
            f"  {programme} {commande:<20} {intention}"
            for commande, intention in (
                ("login", "se connecter une fois"),
                ("list", "voir ce que contient le module"),
                ("download", "récupérer les clips"),
                ("merge", "assembler les vidéos"),
                ("review", "ouvrir l'interface"),
                ("autostart on", "surveiller à chaque session"),
            )
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=tuple(runtime.VERBES),
        # Pas de commande par défaut : sans argument, on affiche l'aide plutôt
        # que d'ouvrir une connexion au compte Blink. Une commande lancée sans
        # rien ne doit pas partir sur le réseau à l'insu de celui qui la tape.
        default=None,
        # L'aide détaillée de chaque verbe est imprimée sous l'aide standard,
        # en une seule liste : séparer les verbes traités ici de ceux qui sont
        # délégués n'apprend rien à l'utilisateur et laisse croire que les
        # premiers n'existent pas.
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--hub", help="nom du Sync Module à utiliser")
    parser.add_argument("--camera", help="ne garder que cette caméra")
    parser.add_argument(
        "--since",
        type=int,
        metavar="JOURS",
        help="ne garder que les clips des N derniers jours",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT,
        help=f"dossier de destination (défaut : {OUTPUT})",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="remplacer les fichiers existants de taille différente",
    )
    args = parser.parse_args()
    if args.command is None:
        # Sans commande, l'aide plutôt qu'une connexion au compte : une
        # commande tapée sans argument ne doit pas partir sur le réseau.
        parser.print_help()
        raise SystemExit(0)
    if args.since is not None and args.since < 0:
        parser.error("--since doit être positif ou nul")
    return args


async def main(args: argparse.Namespace) -> int:
    async with ClientSession() as session:
        blink = await connect(session)
        if blink is None:
            print("\nÉchec de la connexion Blink.")
            return 1

        print("\nConnexion Blink réussie.")
        print(f"Session sauvegardée dans : {CONFIG.resolve()}")
        print("\n=== SYNC MODULES ===")
        if not blink.sync:
            print("Aucun Sync Module trouvé sur ce compte.")
            return 1
        for name, sync in blink.sync.items():
            print(f"- {name} (ID {sync.sync_id}, réseau {sync.network_id})")

        if args.command == "login":
            return 0

        try:
            modules = select_sync_modules(blink, args.hub)
        except ValueError as error:
            print(f"\nErreur : {error}")
            return 2

        had_error = False
        for name, sync in modules:
            print(f"\n=== STOCKAGE LOCAL : {name} ===")
            try:
                clips = await read_local_manifest(sync)
            except RuntimeError as error:
                print(f"  Indisponible : {error}.")
                had_error = True
                continue

            clips = filter_clips(clips, args.camera, args.since)
            print_clip_summary(clips)

            if args.command != "download" or not clips:
                continue

            output = args.output.resolve()
            print(f"  Destination : {output}")
            state = load_download_state(output)
            pending = []
            adopted = 0
            for clip in clips:
                target = target_path(output, clip)
                if is_downloaded(state, sync, clip, target) and not args.overwrite:
                    continue
                if target.exists() and target.stat().st_size > 0 and not args.overwrite:
                    remember_download(state, sync, name, clip, output, target)
                    adopted += 1
                    continue
                pending.append(clip)

            save_download_state(output, state)
            already_downloaded = len(clips) - len(pending) - adopted
            print(
                f"  Incrémental : {len(pending)} nouveau(x), "
                f"{already_downloaded + adopted} déjà acquis."
            )
            if not pending:
                continue

            downloaded = skipped = failed = 0
            for position, clip in enumerate(pending, start=1):
                target = target_path(output, clip)
                print(f"  [{position}/{len(pending)}] {target.name}")
                try:
                    result = await download_clip(blink, clip, target, args.overwrite)
                except Exception as error:  # Continuer avec les autres clips.
                    print(f"    Échec : {type(error).__name__}: {error}")
                    result = "failed"

                if result == "downloaded":
                    downloaded += 1
                    remember_download(state, sync, name, clip, output, target)
                    save_download_state(output, state)
                elif result == "skipped":
                    skipped += 1
                    if target.exists() and target.stat().st_size > 0:
                        remember_download(state, sync, name, clip, output, target)
                        save_download_state(output, state)
                else:
                    failed += 1
                    print("    Échec du téléchargement après plusieurs tentatives.")

            print(
                f"  Terminé : {downloaded} téléchargé(s), "
                f"{skipped} déjà présent(s), {failed} échec(s)."
            )
            had_error = had_error or failed > 0

        return 1 if had_error else 0


# Point d'entrée unique. Les autres programmes gardent leur propre fichier et
# leur propre analyse d'arguments : on ne fait que les appeler, sans rien
# déplacer. Fusionner les quatre en un seul fichier donnerait un script de
# quatre mille lignes, moins lisible et impossible à éprouver par morceaux.
#
# C'est la forme des commandes à verbe, celle de git ou de docker : un nom à
# retenir, un verbe pour l'action. Chaque verbe reçoit tels quels les arguments
# qui le suivent, donc « blink.py review --port 8899 » revient exactement à
# « blink serve --port 8899 ».
# Les verbes, leur programme et leur description vivent dans runtime.VERBES :
# une seule table, lue ici pour l'aide et la délégation, par self_command pour
# la relance, et par docs.py pour les README.
DELEGUES = runtime.DELEGUES


def deleguer(verbe: str, arguments: list) -> int:
    """Passe la main au programme d'un verbe, dans le même processus.

    L'import est fait ici et pas en tête de fichier : ces modules importent
    eux-mêmes blink.py, et surtout ils tirent ffmpeg ou aiohttp derrière eux.
    Une simple demande de manifeste n'a pas à payer ce chargement."""
    import importlib

    module = importlib.import_module(DELEGUES[verbe])
    sys.argv = [f"{DELEGUES[verbe]}.py", *arguments]
    return int(module.main() or 0)


def executer(groupes: list) -> int:
    """Exécute les verbes cités, ensemble.

    Un seul verbe est traité dans ce processus, ce qui garde la sortie et le
    code de retour directs. Plusieurs sont lancés côte à côte et attendus : ils
    s'arrêtent ensemble, faute de quoi un Ctrl+C laisserait derrière lui des
    programmes sans personne pour les arrêter."""
    if len(groupes) == 1:
        verbe, *arguments = groupes[0]
        if verbe in DELEGUES:
            return deleguer(verbe, arguments)
        sys.argv = ["blink", verbe, *arguments]
        return asyncio.run(main(parse_args()))

    lances = []
    for verbe, *arguments in groupes:
        lances.append((verbe, runtime.demarrer(
            runtime.self_command(verbe, *arguments), cwd=str(runtime.app_dir()),
            creationflags=runtime.flags_enfant())))
        print(f"Lancé : {verbe} {' '.join(arguments)}".rstrip())

    # Surveillés ensemble plutôt qu'attendus l'un après l'autre : un verbe qui
    # meurt à la première seconde doit se voir tout de suite, et non à la fin
    # d'une boucle qui tournera des jours. Les autres continuent, l'interface
    # qui tombe n'étant pas une raison d'arrêter la surveillance.
    pire = 0
    annonces = set()
    try:
        while any(processus.poll() is None for _, processus in lances):
            for rang, (verbe, processus) in enumerate(lances):
                code = processus.poll()
                if code is None or rang in annonces:
                    continue
                annonces.add(rang)
                pire = max(pire, abs(code))
                print(f"Arrêté : {verbe}"
                      + (f" (code {code})" if code else " (fin normale)"))
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nArrêt.")
    finally:
        for _, processus in lances:
            if processus.poll() is None:
                processus.terminate()
    return pire


if __name__ == "__main__":
    try:
        # « autostart » vient nécessairement en tête : il n'exécute rien, il
        # ordonnance ce qui suit. Les autres verbes se citent dans n'importe
        # quel ordre, chacun suivi de ses options.
        if len(sys.argv) > 1 and sys.argv[1] == "autostart":
            raise SystemExit(deleguer("autostart", sys.argv[2:]))
        if len(sys.argv) > 1 and sys.argv[1] in runtime.VERBES:
            raise SystemExit(executer(runtime.decouper_verbes(sys.argv[1:])))
        raise SystemExit(asyncio.run(main(parse_args())))
    except ValueError as erreur:
        print(f"{erreur}. Verbes : {', '.join(runtime.VERBES)}")
        raise SystemExit(2)
    except (KeyboardInterrupt, EOFError):
        print("\nConnexion annulée.")
        raise SystemExit(130)
