"""Télécharge les nouveaux clips Blink puis reconstruit les vidéos journalières."""

import argparse
import subprocess
import sys
from pathlib import Path


import runtime

BASE_DIR = runtime.app_dir()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Traitement quotidien : téléchargement incrémental puis fusion."
    )
    parser.add_argument("--hub", default="Maison", help="nom du Sync Module")
    parser.add_argument("--camera", help="limiter à une caméra")
    parser.add_argument("--since", type=int, help="limiter aux N derniers jours")
    return parser.parse_args()


def run(command: list[str]) -> int:
    return subprocess.run(command, cwd=BASE_DIR, check=False).returncode


def main() -> int:
    args = parse_args()
    download = runtime.self_command("download", "--hub", args.hub)
    merge = runtime.self_command("merge")

    if args.camera:
        download.extend(["--camera", args.camera])
        merge.extend(["--camera", args.camera])
    if args.since is not None:
        download.extend(["--since", str(args.since)])

    print("=== TÉLÉCHARGEMENT INCRÉMENTAL ===", flush=True)
    download_status = run(download)

    print("\n=== FUSION PAR JOUR ===", flush=True)
    merge_status = run(merge)

    if download_status:
        print("Le téléchargement a signalé au moins une erreur.")
    return download_status or merge_status


if __name__ == "__main__":
    raise SystemExit(main())
