#!/usr/bin/env python3
"""deploy.py — Déploiement unifié blink2video.

Même esprit que deploy.py de lidar2map/gpxsolar (source unique de version,
un seul point d'entrée pour push + tag + suivi du build), mais sans leur
mécanique de clone temporaire + mapping de fichiers + patch cloud/local
sans rebuild : ces deux-là servent des bundles PyInstaller de plusieurs
centaines de Mo à quelques Go, où reconstruire à chaque correctif coûte
cher. blink2video pèse ~120 Mo et son build complet (3 OS, via
release.yml) prend 15-20 min sur les runners gratuits GitHub - jamais
assez lent cette session pour justifier un second mécanisme de patch à
maintenir en plus. Ce dossier de travail EST le dépôt git (pas une copie
séparée à synchroniser) : deploy.py commit et pousse directement ici.

Usage :
  python deploy.py -m "mon correctif"                # push seul, pas de release
  python deploy.py -m "..." --new-tag                # push + tag = v<VERSION> (runtime.py) + suit le build
  python deploy.py -m "..." --new-tag v0.9.0          # accepté seulement si ça égale v<VERSION>
  python deploy.py -m "..." --dry-run                 # affiche le diff, ne commit ni ne pousse

Prérequis : git, gh (authentifié : gh auth status).
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import NoReturn

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

# === CONFIG ===================================================================

REPO = "nico579/blink2video"
VERSION_FILE = "runtime.py"
SRC = Path(__file__).resolve().parent

# === COLOR / IO HELPERS (repris tels quels de lidar2map/deploy.py) ==========

_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
if os.name == "nt" and _USE_COLOR:
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        h = kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        kernel32.GetConsoleMode(h, ctypes.byref(mode))
        kernel32.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:
        _USE_COLOR = False

_COLORS = {"cyan": "\033[36m", "yellow": "\033[33m", "red": "\033[31m", "green": "\033[32m"}


def cprint(msg: str, color: str = "") -> None:
    if _USE_COLOR and color in _COLORS:
        print(f"{_COLORS[color]}{msg}\033[0m")
    else:
        print(msg)


def fail(msg: str) -> NoReturn:
    cprint(f"\nERREUR : {msg}", "red")
    sys.exit(1)


# === SHELL HELPERS =============================================================

def run(cmd, check=True, capture=False, timeout=120):
    try:
        result = subprocess.run(
            cmd, cwd=str(SRC), check=False, text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        fail(f"{' '.join(cmd)} a dépassé le timeout ({timeout}s).")
    if check and result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        fail(f"{' '.join(cmd)} a échoué (code {result.returncode})" + (f"\n{err}" if err else ""))
    return result


def git(*args, check=True, capture=False):
    return run(["git", *args], check=check, capture=capture)


def gh_json(*args):
    res = run(["gh", *args], capture=True)
    return json.loads(res.stdout)


def read_code_version() -> str:
    """Lit la constante VERSION de runtime.py : SOURCE UNIQUE de la version.

    Le tag de release en est dérivé (v<VERSION>) plutôt que saisi une
    deuxième fois : sans ça, tag et constante peuvent diverger (c'est
    exactement le bug déjà vécu sur lidar2map, un tag pointant une version
    que la constante n'annonçait pas encore - même précaution ici avant
    qu'il ne se reproduise sur ce projet)."""
    txt = (SRC / VERSION_FILE).read_text(encoding="utf-8")
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', txt, re.M)
    if not m:
        fail(f"constante VERSION introuvable dans {VERSION_FILE}")
    return m.group(1)


# === PRE-FLIGHT (spécifique à blink2video : pas dans les twins) =============

def preflight() -> None:
    """Suite de tests + cohérence des README, avant tout commit.

    Absent des deploy.py de lidar2map/gpxsolar (pas la même discipline de
    test dans ces projets-là) ; ajouté ici parce que c'est exactement ce
    qui a été fait à la main avant chaque commit tout au long de cette
    session - un oubli ici committerait une régression déjà détectable
    localement en quelques secondes."""
    cprint("==> Suite de tests (python -m unittest discover)", "cyan")
    res = run([sys.executable, "-m", "unittest", "discover", "-p", "test_*.py"],
             check=False, capture=True, timeout=300)
    if res.returncode != 0:
        print((res.stdout or "") + (res.stderr or ""))
        fail("suite de tests en échec - corrige avant de pousser.")
    cprint("    OK", "green")

    cprint("==> Cohérence des README (python docs.py --check)", "cyan")
    res = run([sys.executable, "docs.py", "--check"], check=False, capture=True)
    if res.returncode != 0:
        print((res.stdout or "") + (res.stderr or ""))
        fail("README désynchronisés - lance python docs.py pour les régénérer.")
    cprint("    OK", "green")


# === PUSH + TAG ================================================================

def compute_diff() -> list:
    cprint("\n==> Modifications :", "cyan")
    git("add", "-A")
    status = git("status", "--short", capture=True).stdout.strip()
    if not status:
        cprint("    Aucun changement. Rien à pousser.", "yellow")
        return []
    for line in status.splitlines():
        print(f"    {line}")
    print()
    git("diff", "--cached", "--stat")
    changed = git("diff", "--cached", "--name-only", capture=True).stdout.strip().splitlines()
    return [c.strip() for c in changed if c.strip()]


def commit_and_push(message: str, new_tag: str) -> None:
    cprint("\n==> Commit", "cyan")
    git("commit", "-m", message)
    cprint("\n==> Push origin main", "cyan")
    git("push", "origin", "main")
    if new_tag:
        cprint(f"\n==> Tag {new_tag}", "cyan")
        git("tag", new_tag)
        git("push", "origin", new_tag)


def watch_release(tag: str) -> None:
    """Le tag poussé déclenche déjà release.yml tout seul (on: push: tags:
    v*) : contrairement au patch cloud de lidar2map/gpxsolar, rien à
    invoquer explicitement ici, seulement à retrouver le run et attendre
    qu'il finisse."""
    cprint(f"\n==> {tag} poussé -> release.yml se déclenche (build 3 OS, ~15-20 min)", "cyan")
    run_id = None
    for _ in range(6):
        time.sleep(5)
        runs = gh_json("run", "list", "--repo", REPO, "--workflow", "release.yml",
                       "--limit", "1", "--json", "databaseId")
        if runs:
            run_id = runs[0]["databaseId"]
            break
    if not run_id:
        cprint("    Run introuvable automatiquement - vérifie l'onglet Actions.", "yellow")
        return
    print(f"    Run : https://github.com/{REPO}/actions/runs/{run_id}")

    cprint("==> Surveillance du run", "cyan")
    res = run(["gh", "run", "watch", str(run_id), "--repo", REPO,
              "--exit-status", "--interval", "20"], check=False, timeout=1800)
    if res.returncode != 0:
        fail(f"le run release.yml a échoué : gh run view {run_id} --repo {REPO} --log-failed")

    cprint(f"\n==> OK. {tag} publiée.", "green")
    print(f"    Release : https://github.com/{REPO}/releases/tag/{tag}")


# === MAIN ======================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="deploy.py",
        description="Déploiement unifié blink2video - push + tag + suivi du build.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Voir le docstring en tête du fichier pour les exemples.",
    )
    parser.add_argument("-m", "--message", required=True, help="message de commit")
    parser.add_argument("--new-tag", nargs="?", const="AUTO", default="",
                        help="pousse aussi un tag -> déclenche release.yml. Sans "
                             "valeur : dérivé de VERSION (v<VERSION>). Avec une "
                             "valeur vX.Y.Z : acceptée seulement si elle égale "
                             "v<VERSION>, sinon refusée.")
    parser.add_argument("--dry-run", action="store_true",
                        help="affiche le diff sans commit ni push")
    parser.add_argument("--skip-tests", action="store_true",
                        help="saute la suite de tests et docs.py --check (déconseillé)")
    args = parser.parse_args()

    if args.new_tag:
        want = f"v{read_code_version()}"
        if args.new_tag == "AUTO":
            args.new_tag = want
        elif args.new_tag != want:
            fail(f"--new-tag {args.new_tag} != {want} (constante VERSION dans "
                 f"{VERSION_FILE}). Bumpe VERSION, puis repasse --new-tag sans "
                 f"valeur (le tag est dérivé).")

    if not args.skip_tests and not args.dry_run:
        preflight()

    changed = compute_diff()
    if not changed:
        if args.new_tag:
            cprint(f"\n==> Aucun changement à pousser ; tag {args.new_tag} sur le HEAD courant.", "cyan")
            git("tag", args.new_tag)
            git("push", "origin", args.new_tag)
            watch_release(args.new_tag)
        return 0

    if args.dry_run:
        cprint("\n==> --dry-run : pas de commit ni de push.", "yellow")
        return 0

    commit_and_push(args.message, args.new_tag)

    if args.new_tag:
        watch_release(args.new_tag)
    else:
        cprint("\n==> Poussé sur main (pas de tag -> pas de release).", "green")

    return 0


if __name__ == "__main__":
    sys.exit(main())
