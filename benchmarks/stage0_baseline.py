"""Banc reproductible P-01 à P-12 du rapport d'audit.

Le profil ``quick`` reste entièrement local et prudent pour être rejoué avant
chaque étape. Il conserve le protocole de trois chauffes, vingt mesures courtes
et cinq mesures lourdes, avec des volumes réduits et explicitement enregistrés.
Le profil ``full`` utilise les volumes de référence du rapport et doit être
lancé sur une machine disposant de l'espace nécessaire.

Exemples :

    python -B benchmarks/stage0_baseline.py --profile quick --label run-a
    python -B benchmarks/stage0_baseline.py --profile quick --label run-b
    python -B benchmarks/stage0_baseline.py --compare results/a.json results/b.json
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import hashlib
import http.server
import importlib.metadata
import json
import math
import multiprocessing
import os
import platform
import random
import re
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RESULTS = Path(__file__).resolve().parent / "results"
SEED = 20260813
BASELINE_REVISION = "58f5566"
os.environ["BLINK_BOOTSTRAP"] = "none"
sys.path.insert(0, str(ROOT))

import blink2video as b2v  # noqa: E402 - environnement isolé avant import
import runtime  # noqa: E402 - environnement isolé avant import

try:
    import psutil
except ImportError:  # Le temps reste mesurable sans dépendance optionnelle.
    psutil = None


PROFILES = {
    "safe": {
        # Profil de diagnostic local lorsque la création répétée de processus
        # déclenche l'antivirus. Ces nombres ne satisfont pas le protocole de
        # qualification et les résultats portent donc la mention preliminary.
        "warmups": 1,
        "short_runs": 5,
        "heavy_runs": 3,
        "process_warmups": 0,
        "process_runs": 2,
        "P01_cooldown_s": 0.50,
        "P02_sizes": [100, 1_000, 5_000],
        "P03_size": 5_000,
        "P04_size": 1_000,
        "P05_local": 5_000,
        "P05_cloud": 500,
        "P07_sizes": [1 * 1024**2, 8 * 1024**2],
        "P08_workers": 4,
        "P09_delay_s": 0.02,
        "P10_workers": 4,
    },
    "quick": {
        "warmups": 3,
        "short_runs": 20,
        "heavy_runs": 5,
        "P01_cooldown_s": 0.25,
        "P02_sizes": [100, 1_000, 5_000],
        "P03_size": 5_000,
        "P04_size": 1_000,
        "P05_local": 5_000,
        "P05_cloud": 500,
        "P07_sizes": [1 * 1024**2, 8 * 1024**2],
        "P08_workers": 8,
        "P09_delay_s": 0.02,
        "P10_workers": 8,
    },
    "full": {
        "warmups": 3,
        "short_runs": 20,
        "heavy_runs": 5,
        "P01_cooldown_s": 0.25,
        "P02_sizes": [100, 1_000, 10_000, 100_000],
        "P03_size": 100_000,
        "P04_size": 100_000,
        "P05_local": 100_000,
        "P05_cloud": 5_000,
        # Ce profil est réservé à un hôte dont la capacité a été vérifiée.
        "P07_sizes": [10 * 1024**2, 100 * 1024**2, 1024**3],
        "P08_workers": 20,
        "P09_delay_s": 0.05,
        "P10_workers": 20,
    },
}


class Clip:
    def __init__(self, identifiant, camera, instant, network_id=1):
        self.id = identifiant
        self.name = camera
        self.created_at = instant
        self.network_id = network_id
        self.size = 1


class Sync:
    def __init__(self, sync_id=1):
        self.sync_id = sync_id


def percentile(valeurs, quantile):
    ordonnees = sorted(valeurs)
    if not ordonnees:
        return None
    rang = max(0, min(len(ordonnees) - 1,
                      math.ceil(quantile * len(ordonnees)) - 1))
    return ordonnees[rang]


def bootstrap_median_ci(valeurs, graine, repetitions=2_000):
    if not valeurs:
        return [None, None]
    hasard = random.Random(graine)
    n = len(valeurs)
    medianes = [
        statistics.median(hasard.choices(valeurs, k=n))
        for _ in range(repetitions)
    ]
    return [percentile(medianes, 0.025), percentile(medianes, 0.975)]


def resume(valeurs, graine=SEED):
    valeurs = [float(v) for v in valeurs]
    moyenne = statistics.fmean(valeurs) if valeurs else None
    ecart = statistics.pstdev(valeurs) if len(valeurs) > 1 else 0.0
    return {
        "n": len(valeurs),
        "min": min(valeurs) if valeurs else None,
        "median": statistics.median(valeurs) if valeurs else None,
        "p95": percentile(valeurs, 0.95),
        "max": max(valeurs) if valeurs else None,
        "mean": moyenne,
        "stdev": ecart,
        "cv_percent": (ecart / moyenne * 100) if moyenne else 0.0,
        "median_ci95": bootstrap_median_ci(valeurs, graine),
        "samples": valeurs,
    }


def chronometrer(fonction, warmups, runs):
    for _ in range(warmups):
        fonction()
    mesures = []
    derniere = None
    for _ in range(runs):
        debut = time.perf_counter_ns()
        derniere = fonction()
        mesures.append((time.perf_counter_ns() - debut) / 1_000_000)
    return resume(mesures), derniere


def empreinte(path):
    hachage = hashlib.sha256()
    with Path(path).open("rb") as source:
        for bloc in iter(lambda: source.read(1024 * 1024), b""):
            hachage.update(bloc)
    return hachage.hexdigest()


def git(*arguments):
    resultat = subprocess.run(
        ["git", *arguments], cwd=ROOT, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace", check=False,
    )
    return resultat.stdout.strip() if resultat.returncode == 0 else None


def version_distribution(nom):
    try:
        return importlib.metadata.version(nom)
    except importlib.metadata.PackageNotFoundError:
        return None


def metadata(profile):
    alimentation = None
    if os.name == "nt":
        resultat = subprocess.run(
            ["powercfg", "/getactivescheme"], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", check=False,
        )
        alimentation = resultat.stdout.strip() or None
    return {
        "captured_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "run_id": dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ"),
        "seed": SEED,
        "profile": profile,
        # La révision de référence est une cible documentaire. Les mesures
        # décrivent toujours ce qui a réellement été exécuté : HEAD, état du
        # worktree et empreintes des sources. Un worktree modifié n'est donc
        # jamais présenté comme s'il était exactement la révision 58f5566.
        "reference_revision_requested": BASELINE_REVISION,
        "reference_revision_resolved": git("rev-parse", BASELINE_REVISION),
        "executed_head": git("rev-parse", "HEAD"),
        "executed_branch": git("branch", "--show-current"),
        "executed_tracked_worktree_dirty": bool(
            git("status", "--short", "--untracked-files=no")
        ),
        "executed_production_sha256": {
            nom: empreinte(ROOT / nom)
            for nom in ("blink2video.py", "runtime.py", "serve.py", "merge_daily.py")
        },
        "python": sys.version,
        "implementation": platform.python_implementation(),
        "os": platform.platform(),
        "processor": platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER"),
        "logical_cpu": os.cpu_count(),
        "physical_memory_bytes": (
            int(psutil.virtual_memory().total) if psutil else None
        ),
        "workspace_disk_free_bytes": shutil.disk_usage(ROOT).free,
        "power_plan": alimentation,
        "dependencies": {
            nom: version_distribution(nom)
            for nom in ("aiohttp", "blinkpy", "tzdata", "psutil")
        },
    }


def environnement(home):
    return dict(
        os.environ,
        BLINK_HOME=str(Path(home).resolve()),
        BLINK_BOOTSTRAP="none",
        PYTHONIOENCODING="utf-8",
    )


def port_libre():
    with socket.socket() as prise:
        prise.bind(("127.0.0.1", 0))
        return prise.getsockname()[1]


def mesurer_processus(commande, cwd, env):
    debut = time.perf_counter_ns()
    processus = subprocess.Popen(
        commande, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    pic = 0
    while processus.poll() is None:
        if psutil:
            try:
                courant = psutil.Process(processus.pid)
                famille = [courant, *courant.children(recursive=True)]
                pic = max(pic, sum(p.memory_info().rss for p in famille
                                   if p.is_running()))
            except (psutil.Error, OSError):
                pass
        time.sleep(0.001)
    code = processus.wait()
    return {
        "duration_ms": (time.perf_counter_ns() - debut) / 1_000_000,
        "peak_tree_rss_bytes": pic or None,
        "exit_code": code,
    }


def benchmark_p01(config, racine):
    home, cwd = racine / "home", racine / "cwd"
    home.mkdir()
    cwd.mkdir()
    entree = [sys.executable, "-B", str(ROOT / "blink2video.py")]
    scenarios = {
        "no_args": [],
        "help": ["--help"],
        "version": ["--version"],
        "open_closed": ["open", "--port", str(port_libre())],
        "stop_idle": ["stop"],
    }
    resultats = {}
    cooldown_s = float(config["P01_cooldown_s"])
    processus_crees = 0
    for rang, (nom, arguments) in enumerate(scenarios.items()):
        mesures = []
        process_warmups = config.get("process_warmups", config["warmups"])
        process_runs = config.get("process_runs", config["short_runs"])
        total = process_warmups + process_runs
        for passage in range(total):
            mesure = mesurer_processus(entree + arguments, cwd, environnement(home))
            processus_crees += 1
            if passage >= process_warmups:
                mesures.append(mesure)
            # Hors chrono : laisser refroidir l'antivirus et le chargeur entre
            # deux créations de processus réduit leur influence corrélée.
            if cooldown_s:
                time.sleep(cooldown_s)
        semantique = subprocess.run(
            entree + arguments, cwd=cwd, env=environnement(home),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", check=False, timeout=30,
        )
        processus_crees += 1
        if cooldown_s:
            time.sleep(cooldown_s)
        resultats[nom] = {
            "duration_ms": resume([m["duration_ms"] for m in mesures], SEED + rang),
            "peak_tree_rss_bytes": resume(
                [m["peak_tree_rss_bytes"] for m in mesures
                 if m["peak_tree_rss_bytes"] is not None], SEED + 100 + rang,
            ),
            "exit_codes": sorted({m["exit_code"] for m in mesures}),
            "process_warmups": process_warmups,
            "process_measurements": process_runs,
            "semantic_check_processes": 1,
            "processes_created": total + 1,
            "cooldown_between_creations_s": cooldown_s,
            "cooldown_in_timed_duration": False,
            "correctness": semantique.returncode in ({1} if nom == "open_closed" else {0}),
            "observed": (
                "help_and_exit" if nom == "no_args" and "Verbes :" in semantique.stdout
                else "command_completed"
            ),
        }
    resultats["no_args"]["observed_current_behavior_is_valid"] = True
    resultats["no_args"]["target_no_args_equals_start"] = False
    resultats["no_args"]["correctness"] = False
    return {
        "status": "measured",
        "mode": "real_subprocess",
        "processes_created_total": processus_crees,
        "process_count_formula": (
            f"5 scenarios × ({process_warmups} warmups + {process_runs} measures "
            "+ 1 semantic check)"
        ),
        "scenarios": resultats,
    }


def fabriquer_etat(nombre):
    base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    clips = {}
    for i in range(nombre):
        instant = (base + dt.timedelta(seconds=i)).isoformat()
        clips[f"1:camera-{i % 10}:{instant}"] = {
            "hub": "Test", "camera": f"camera-{i % 10}",
            "created_at": instant, "path": f"camera-{i % 10}/{i}.mp4",
            "bytes": 1, "source": "usb",
        }
    return {"version": 1, "clips": clips}


def benchmark_p02(config, racine):
    scenarios = {}
    for rang, taille in enumerate(config["P02_sizes"]):
        dossier = racine / f"batch-{taille}"
        dossier.mkdir()
        fichier = dossier / b2v.STATE_FILENAME
        etat = fabriquer_etat(taille)
        stats, _ = chronometrer(
            lambda: b2v._ecrire_registre(fichier, etat),
            config["warmups"], config["heavy_runs"],
        )
        relu = json.loads(fichier.read_text(encoding="utf-8"))
        scenarios[str(taille)] = {
            "duration_ms": stats,
            "stored_bytes": fichier.stat().st_size,
            "entries": len(relu.get("clips") or {}),
            "correctness": len(relu.get("clips") or {}) == taille,
        }
    return {"status": "measured", "mode": "real_json_io", "scenarios": scenarios}


def benchmark_p03(config, racine):
    taille = config["P03_size"]
    dossier = racine / "single-update"
    dossier.mkdir()
    fichier = dossier / b2v.STATE_FILENAME
    etat = fabriquer_etat(taille)
    b2v._ecrire_registre(fichier, etat)
    cle = next(iter(etat["clips"]))

    def mutation():
        etat["clips"][cle]["excluded"] = not etat["clips"][cle].get("excluded", False)
        b2v._ecrire_registre(fichier, etat)

    stats, _ = chronometrer(mutation, config["warmups"], config["short_runs"])
    return {
        "status": "measured", "mode": "real_json_io",
        "scenarios": {str(taille): {
            "duration_ms": stats,
            "bytes_rewritten_per_update": fichier.stat().st_size,
            "correctness": isinstance(
                json.loads(fichier.read_text(encoding="utf-8"))["clips"][cle]
                .get("excluded"), bool,
            ),
        }},
    }


def benchmark_p04(config, racine):
    taille = config["P04_size"]
    sortie = racine / "verify"
    sortie.mkdir()
    sync = Sync()
    etat = {"version": 1, "clips": {}}
    cas = []
    base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    reindexes = exclusions = 0
    for i in range(taille):
        ancien = Clip(i, f"camera-{i % 10}", base + dt.timedelta(seconds=i))
        cible = b2v.target_path(sortie, ancien)
        if i % 10 == 0:
            etat["clips"][b2v.state_key(sync, ancien)] = {
                "camera": ancien.name, "created_at": ancien.created_at.isoformat(),
                "path": cible.relative_to(sortie).as_posix(), "excluded": True,
            }
            exclusions += 1
            cas.append((ancien, cible, True))
        else:
            cible.parent.mkdir(parents=True, exist_ok=True)
            cible.write_bytes(b"x")
            b2v.remember_download(etat, sync, "Test", ancien, sortie, cible)
            if i % 10 == 1:
                nouveau = Clip(i + 1_000_000, ancien.name, ancien.created_at)
                cas.append((nouveau, b2v.target_path(sortie, nouveau), True))
                reindexes += 1
            else:
                cas.append((ancien, cible, True))

    def verifier():
        return [b2v.is_downloaded(etat, sync, clip, cible)
                for clip, cible, _ in cas]

    stats, dernier = chronometrer(verifier, config["warmups"], config["short_runs"])
    erreurs = sum(observe is not attendu
                  for observe, (_, _, attendu) in zip(dernier, cas))
    return {
        "status": "measured", "mode": "real_state_and_files",
        "scenarios": {str(taille): {
            "duration_ms": stats, "checks": taille,
            "reindexed": reindexes, "excluded": exclusions,
            "errors": erreurs, "correctness": erreurs == 0,
        }},
    }


def benchmark_p05(config, _racine):
    locaux_n, cloud_n = config["P05_local"], config["P05_cloud"]
    base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    locaux = [
        Clip(i, f"camera-{i % 10}", base + dt.timedelta(seconds=i * 5))
        for i in range(locaux_n)
    ]
    doublons_attendus = cloud_n // 2
    cloud = []
    for i in range(cloud_n):
        if i < doublons_attendus:
            source = locaux[(i * 17) % locaux_n]
            cloud.append(Clip(1_000_000 + i, source.name,
                              source.created_at + dt.timedelta(seconds=i % 3)))
        else:
            cloud.append(Clip(1_000_000 + i, f"nouvelle-{i % 7}",
                              base + dt.timedelta(days=365, seconds=i)))

    stats, dernier = chronometrer(
        lambda: b2v.rapprocher(locaux, cloud),
        config["warmups"], config["heavy_runs"],
    )
    tracemalloc.start()
    b2v.rapprocher(locaux, cloud)
    _, pic = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    inedits, doublons = dernier
    correct = len(doublons) == doublons_attendus
    return {
        "status": "measured", "mode": "real_matching_synthetic_dataset",
        "scenarios": {f"{locaux_n}x{cloud_n}": {
            "duration_ms": stats, "python_heap_peak_bytes": pic,
            "expected_duplicates": doublons_attendus,
            "observed_duplicates": len(doublons),
            "observed_new": len(inedits), "correctness": correct,
        }},
    }


def benchmark_p06(config, _racine):
    base = dt.datetime(2026, 8, 13, tzinfo=dt.timezone.utc)
    def fabriquer_manifest(nombre):
        return [{
            "id": i,
            "device_name": f"camera-{i % 10}",
            "created_at": (base - dt.timedelta(minutes=i)).isoformat(),
            "media": f"/media/{i}",
            "network_id": i % 2,
            "type": "video",
        } for i in range(nombre)]

    def passage(nombre_total, connus):
        appels = []
        entrees = fabriquer_manifest(nombre_total)

        class Blink:
            async def get_videos_metadata(self, **options):
                appels.append(dict(options))
                return entrees

        clips = asyncio.run(b2v.read_cloud_manifest(Blink(), 30))
        ids = {clip.id for clip in clips}
        return {
            "clips": len(clips),
            "new": len(ids - connus),
            "api_calls": len(appels),
            "calls": appels,
            "metadata_bytes_returned": len(json.dumps(entrees).encode("utf-8")),
        }

    scenarios = {}
    # Chaque cas repart du même snapshot de 600 IDs. Le compteur de nouveautés
    # est calculé par le banc ; l'appel et le parsing sont ceux de production.
    # Cela mesure séparément « première passe », puis 0, 1 et 10 nouveautés sans
    # que les répétitions chronométrées modifient l'état du passage suivant.
    connus_600 = set(range(600))
    definitions = (
        ("first_600", 600, set(), 600),
        ("then_0_new", 600, connus_600, 0),
        ("then_1_new", 601, connus_600, 1),
        ("then_10_new", 610, connus_600, 10),
    )
    for nom, total, connus, attendus in definitions:
        stats, dernier = chronometrer(
            lambda total=total, connus=connus: passage(total, connus),
            config["warmups"], config["heavy_runs"],
        )
        appel = dernier["calls"][0] if dernier["calls"] else {}
        scenarios[nom] = {
            "duration_ms": stats,
            "clips_returned": dernier["clips"],
            "expected_new": attendus,
            "observed_new": dernier["new"],
            "api_calls_observed": dernier["api_calls"],
            "api_call_options_observed": dernier["calls"],
            "metadata_bytes_returned": dernier["metadata_bytes_returned"],
            "requested_stop": appel.get("stop"),
            "newness_counter_scope": "benchmark harness over production parser output",
            "correctness": (
                dernier["clips"] == total
                and dernier["new"] == attendus
                and dernier["api_calls"] == 1
            ),
        }
    return {
        "status": "measured",
        "mode": "instrumented_production_parser_fake_blink_no_network",
        "measurement_kind": "instrumented",
        "pagination_observed": False,
        "known_protocol_gap": (
            "production makes one metadata call with stop=20; the fake returns the "
            "whole supplied manifest so parser cost can be measured, not provider pagination"
        ),
        "qualification_correctness": False,
        "scenarios": scenarios,
    }


class _MediaHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_GET(self):
        try:
            taille = int(self.path.rsplit("/", 1)[-1])
        except ValueError:
            self.send_error(400)
            return
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(taille))
        self.end_headers()
        bloc = b"\x00\x00\x00\x18ftypmp42" + b"x" * (64 * 1024 - 12)
        restant = taille
        while restant:
            morceau = bloc[:min(restant, len(bloc))]
            self.wfile.write(morceau)
            restant -= len(morceau)

    def log_message(self, _format, *_args):
        pass


@contextlib.contextmanager
def serveur_media_local():
    serveur = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _MediaHandler)
    thread = threading.Thread(target=serveur.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{serveur.server_address[1]}"
    finally:
        serveur.shutdown()
        serveur.server_close()
        thread.join(timeout=5)


def benchmark_p07(config, racine):
    import aiohttp

    scenarios = {}
    with serveur_media_local() as base_url:
        for rang, taille in enumerate(config["P07_sizes"]):
            cible = racine / f"media-{taille}.mp4"

            async def telecharger():
                async with aiohttp.ClientSession() as session:
                    class Blink:
                        async def do_http_get(self, adresse):
                            return await session.get(adresse)
                    clip = b2v.CloudClip({
                        "id": taille, "device_name": "bench",
                        "created_at": "2026-08-13T12:00:00+00:00",
                        "media": f"{base_url}/bytes/{taille}", "network_id": 1,
                    })
                    return await clip.download_to(Blink(), cible)

            def passage():
                cible.unlink(missing_ok=True)
                return asyncio.run(telecharger())

            stats, dernier = chronometrer(
                passage, config["warmups"], config["heavy_runs"],
            )
            tracemalloc.start()
            passage()
            _, pic = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            debit = (taille / 1024**2) / (stats["median"] / 1000)
            scenarios[str(taille)] = {
                "duration_ms": stats, "throughput_mib_s": debit,
                "python_heap_peak_bytes": pic,
                "stored_bytes": cible.stat().st_size if cible.exists() else 0,
                "correctness": bool(dernier) and cible.stat().st_size == taille,
                "implementation_observed": "response.read_then_write_bytes",
            }
            cible.unlink(missing_ok=True)
    return {"status": "measured", "mode": "real_loopback_http", "scenarios": scenarios}


def _worker_verrou(home, depart, avant_ecriture, liberation, resultats):
    os.environ["BLINK_HOME"] = home
    os.environ["BLINK_BOOTSTRAP"] = "none"
    import pathlib
    import runtime as runtime_enfant
    original = pathlib.Path.write_text

    def ecriture_retardee(path, *args, **kwargs):
        if path.name == ".blink_benchrace.lock":
            try:
                avant_ecriture.wait(timeout=10)
            except threading.BrokenBarrierError:
                pass
        return original(path, *args, **kwargs)

    pathlib.Path.write_text = ecriture_retardee
    debut = time.perf_counter_ns()
    try:
        depart.wait(timeout=10)
        try:
            with runtime_enfant.verrou("benchrace", f"w-{os.getpid()}"):
                resultats.put(("entered", (time.perf_counter_ns() - debut) / 1e6))
                liberation.wait(timeout=10)
        except runtime_enfant.BusyError:
            resultats.put(("busy", (time.perf_counter_ns() - debut) / 1e6))
        except Exception as erreur:
            resultats.put(("error", type(erreur).__name__, str(erreur)))
    finally:
        pathlib.Path.write_text = original


def course_verrou(home, travailleurs):
    contexte = multiprocessing.get_context("spawn")
    depart = contexte.Barrier(travailleurs)
    avant = contexte.Barrier(travailleurs)
    liberation = contexte.Event()
    file = contexte.Queue()
    processus = [contexte.Process(
        target=_worker_verrou,
        args=(str(home), depart, avant, liberation, file),
    ) for _ in range(travailleurs)]
    debut = time.perf_counter_ns()
    for enfant in processus:
        enfant.start()
    messages = []
    try:
        for _ in processus:
            messages.append(file.get(timeout=30))
    finally:
        liberation.set()
        for enfant in processus:
            enfant.join(timeout=10)
            if enfant.is_alive():
                enfant.terminate()
                enfant.join(timeout=5)
    return {
        "duration_ms": (time.perf_counter_ns() - debut) / 1e6,
        "winners": sum(m[0] == "entered" for m in messages),
        "busy": sum(m[0] == "busy" for m in messages),
        "errors": [m for m in messages if m[0] == "error"],
        "wait_ms": [m[1] for m in messages if len(m) == 2],
    }


def benchmark_p08(config, racine):
    home = racine / "locks"
    home.mkdir()
    essais = []
    for _ in range(config["heavy_runs"]):
        (home / ".blink_benchrace.lock").unlink(missing_ok=True)
        essais.append(course_verrou(home, config["P08_workers"]))
    gagnants = [e["winners"] for e in essais]
    attentes = [v for e in essais for v in e["wait_ms"]]
    return {
        "status": "measured", "mode": "real_multiprocess_forced_interleaving",
        "scenarios": {f"{config['P08_workers']}_workers": {
            "duration_ms": resume([e["duration_ms"] for e in essais]),
            "wait_ms": resume(attentes), "winners_per_run": gagnants,
            "errors": [err for e in essais for err in e["errors"]],
            "correctness": all(n == 1 for n in gagnants),
        }},
    }


def benchmark_p09(config, racine):
    delai = config["P09_delay_s"]
    _ = racine  # Le probe n'écrit rien, mais garde la signature commune.

    async def probe(operations):
        origine = time.perf_counter_ns()
        verrous = {}
        chronologie = []

        async def operation(nom, ressource):
            verrou = verrous.setdefault(ressource, asyncio.Lock())
            async with verrou:
                debut = time.perf_counter_ns()
                await asyncio.sleep(delai)
                fin = time.perf_counter_ns()
                chronologie.append({
                    "operation": nom,
                    "resource": ressource,
                    "start_ms": (debut - origine) / 1e6,
                    "end_ms": (fin - origine) / 1e6,
                })

        await asyncio.gather(*(operation(*item) for item in operations))
        chronologie.sort(key=lambda item: item["start_ms"])
        chevauchement_ms = 0.0
        for gauche, droite in zip(chronologie, chronologie[1:]):
            chevauchement_ms += max(
                0.0,
                min(gauche["end_ms"], droite["end_ms"])
                - max(gauche["start_ms"], droite["start_ms"]),
            )
        return {
            "timeline": chronologie,
            "overlap_ms": chevauchement_ms,
            "overlap_observed": chevauchement_ms > 0.0,
        }

    definitions = (
        (
            "same_hub_usb_vs_direct",
            (("usb", "hub-1"), ("direct", "hub-1")),
            False,
        ),
        (
            "two_distinct_usb_hubs",
            (("usb-hub-1", "hub-1"), ("usb-hub-2", "hub-2")),
            True,
        ),
        (
            "usb_vs_cloud",
            (("usb", "hub-1"), ("cloud", "account-cloud")),
            True,
        ),
    )
    scenarios = {}
    for rang, (nom, operations, overlap_attendu) in enumerate(definitions):
        stats, dernier = chronometrer(
            lambda operations=operations: asyncio.run(probe(operations)),
            config["warmups"], config["short_runs"],
        )
        scenarios[nom] = {
            "duration_ms": stats,
            "synthetic_operation_delay_ms": delai * 1000,
            "operations": [list(item) for item in operations],
            "timeline_last_run": dernier["timeline"],
            "overlap_ms_last_run": dernier["overlap_ms"],
            "overlap_expected": overlap_attendu,
            "overlap_observed": dernier["overlap_observed"],
            "correctness": dernier["overlap_observed"] is overlap_attendu,
            "seed_offset": SEED + rang,
        }
    return {
        "status": "measured",
        "mode": "synthetic_resource_scheduler_probe",
        "measurement_kind": "simulation",
        "latency_scope": (
            "synthetic lock/overlap probe only; not blink2video production latency"
        ),
        "scenarios": scenarios,
    }


def course_auth(config_path, travailleurs):
    barriere = threading.Barrier(travailleurs)
    erreurs = []
    original = Path.replace

    def remplacement_synchrone(path, cible):
        if path == config_path.with_suffix(".tmp"):
            try:
                barriere.wait(timeout=5)
            except threading.BrokenBarrierError:
                pass
        return original(path, cible)

    def worker(numero):
        blink = SimpleNamespace(auth=SimpleNamespace(login_attributes={
            "refresh_token": f"faux-{numero}", "username": "x", "password": "secret",
        }))
        try:
            b2v.save_session(blink)
        except Exception as erreur:
            erreurs.append(type(erreur).__name__)

    debut = time.perf_counter_ns()
    with mock.patch.object(b2v, "CONFIG", config_path), \
         mock.patch.object(Path, "replace", remplacement_synchrone):
        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(travailleurs)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
    valide = False
    try:
        contenu = json.loads(config_path.read_text(encoding="utf-8"))
        valide = bool(contenu.get("refresh_token")) and contenu.get("password") == ""
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return {
        "duration_ms": (time.perf_counter_ns() - debut) / 1e6,
        "errors": erreurs, "valid_final_session": valide,
    }


def benchmark_p10(config, racine):
    cible = racine / "blink_auth.json"
    compteur = 0

    def serialiser():
        nonlocal compteur
        compteur += 1
        blink = SimpleNamespace(auth=SimpleNamespace(login_attributes={
            "refresh_token": f"faux-{compteur}", "username": "x",
            "password": "jamais-sur-disque",
        }))
        with mock.patch.object(b2v, "CONFIG", cible):
            b2v.save_session(blink)
            return b2v.load_saved_session()

    stats, derniere = chronometrer(
        serialiser, config["warmups"], config["short_runs"],
    )
    courses = [course_auth(cible, config["P10_workers"])
               for _ in range(config["heavy_runs"])]
    return {
        "status": "measured", "mode": "real_file_io_fake_tokens",
        "scenarios": {
            "serial_refresh": {
                "duration_ms": stats,
                "correctness": derniere.get("password") == "",
            },
            f"{config['P10_workers']}_concurrent_writers": {
                "duration_ms": resume([c["duration_ms"] for c in courses]),
                "errors_per_run": [len(c["errors"]) for c in courses],
                "valid_final_session": [c["valid_final_session"] for c in courses],
                "correctness": all(not c["errors"] and c["valid_final_session"]
                                   for c in courses),
            },
        },
    }


def arbre_processus():
    enfant_code = "import time; time.sleep(60)"
    parent_code = (
        "import subprocess,sys,time; "
        f"p=subprocess.Popen([sys.executable,'-c',{enfant_code!r}]); "
        "print(p.pid,flush=True); time.sleep(60)"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_code], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8",
        creationflags=runtime.SANS_FENETRE,
        start_new_session=(os.name != "nt"),
    )
    ligne = parent.stdout.readline().strip()
    return parent, int(ligne)


def benchmark_p11(config, _racine):
    essais = []
    for _ in range(config["heavy_runs"]):
        parent, enfant_pid = arbre_processus()
        debut = time.perf_counter_ns()
        try:
            runtime.arreter_processus(parent.pid, avec_descendance=True)
            try:
                parent.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            time.sleep(0.1)
            survivants = [pid for pid in (parent.pid, enfant_pid)
                          if runtime.processus_vivant(pid)]
        finally:
            if parent.poll() is None:
                runtime.arreter_processus(parent.pid, avec_descendance=True)
                try:
                    parent.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    parent.kill()
            if runtime.processus_vivant(enfant_pid):
                runtime.arreter_processus(enfant_pid, avec_descendance=True)
        essais.append({
            "duration_ms": (time.perf_counter_ns() - debut) / 1e6,
            "survivors": survivants,
        })
    return {
        "status": "measured", "mode": "real_local_process_tree",
        "scenarios": {"depth_2_cooperative": {
            "duration_ms": resume([e["duration_ms"] for e in essais]),
            "survivors_per_run": [len(e["survivors"]) for e in essais],
            "correctness": all(not e["survivors"] for e in essais),
        }},
    }


def benchmark_p12(config, racine, p01):
    home, cwd = racine / "home-p12", racine / "cwd-p12"
    home.mkdir()
    cwd.mkdir()
    entree = [sys.executable, "-B", str(ROOT / "blink2video.py")]
    sans = subprocess.run(
        entree, cwd=cwd, env=environnement(home), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", check=False, timeout=30,
    )
    aide_start = subprocess.run(
        [*entree, "start", "--help"], cwd=cwd, env=environnement(home),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", check=False, timeout=30,
    )
    return {
        "status": "partial",
        "mode": "current_cli_characterization",
        "scenarios": {
            "no_args": {
                "duration_ms": p01["scenarios"]["no_args"]["duration_ms"],
                "observed": "help_and_exit" if "Verbes :" in sans.stdout else "other",
                "target_no_args_equals_start": False,
                "correctness_current_behavior": sans.returncode == 0,
                "correctness": False,
            },
            "start_help": {
                "exit_code": aide_start.returncode,
                "correctness": aide_start.returncode == 0,
            },
        },
        "unavailable": {
            "valid_session_preflight": "structured preflight absent",
            "missing_session_web_login": "onboarding server absent",
            "expired_session_web_login": "onboarding server absent",
            "two_factor_web_login": "onboarding server absent",
            "browser_fallback": "onboarding server absent",
            "mini_smoke_before_workers": "startup preflight absent",
            "time_to_ready": "target lifecycle absent",
        },
    }


def markdown(resultat):
    lignes = [
        "# Baseline de performance — étape 0",
        "",
        f"Date : {resultat['metadata']['captured_at']}",
        f"Profil : `{resultat['metadata']['profile']}`",
        f"Validité : `{resultat['validity']}`",
        f"Révision exécutée : `{resultat['metadata']['executed_head']}`",
        f"Révision documentaire visée : `{resultat['metadata']['reference_revision_resolved']}`",
        f"Worktree de production modifié : "
        f"`{resultat['metadata']['executed_tracked_worktree_dirty']}`",
        f"Graine : `{resultat['metadata']['seed']}`",
        "",
        "| ID | Statut | Mode | Scénarios mesurés | Médianes (ms) | Exactitude |",
        "|---|---|---|---:|---|---|",
    ]
    for identifiant, benchmark in resultat["benchmarks"].items():
        scenarios = benchmark.get("scenarios") or {}
        medianes = []
        for nom, scenario in scenarios.items():
            duree = scenario.get("duration_ms")
            if isinstance(duree, dict) and duree.get("median") is not None:
                medianes.append(f"{nom}={duree['median']:.3f}")
        exactitudes = [s.get("correctness") for s in scenarios.values()
                       if "correctness" in s]
        exact = ("oui" if exactitudes and all(exactitudes)
                 else "non" if exactitudes else "n/a")
        lignes.append(
            f"| {identifiant} | {benchmark['status']} | {benchmark['mode']} | "
            f"{len(scenarios)} | {'; '.join(medianes) or '—'} | {exact} |"
        )
    lignes += [
        "",
        "Les valeurs détaillées, échantillons, p95, coefficients de variation et",
        "intervalles de confiance bootstrap à 95 % se trouvent dans le JSON brut.",
        "Une exactitude « non » décrit la baseline défectueuse ; elle invaliderait",
        "toute revendication de gain pour une version candidate.",
        "",
        "P-12 est volontairement partiel : les branches d'onboarding demandées",
        "n'existent pas encore et sont marquées indisponibles, jamais mesurées à zéro.",
    ]
    if resultat.get("safety_notes"):
        lignes += ["", "## Écarts et sécurité", ""]
        lignes += [f"- {note}" for note in resultat["safety_notes"]]
    return "\n".join(lignes) + "\n"


def collecter(profile, only=None):
    config = PROFILES[profile]
    tous = {f"P-{numero:02d}" for numero in range(1, 13)}
    selection = set(only or tous)
    resultat = {
        "schema_version": 2,
        "kind": "blink2video-stage0-baseline",
        "validity": "candidate" if selection == tous else "partial",
        "metadata": metadata(profile),
        "protocol": {
            "warmups": config["warmups"],
            "short_runs": config["short_runs"],
            "heavy_runs": config["heavy_runs"],
            "process_warmups": config.get("process_warmups", config["warmups"]),
            "process_runs": config.get("process_runs", config["short_runs"]),
            "P01_cooldown_s": config["P01_cooldown_s"],
            "clock": "time.perf_counter_ns",
            "median_ci": "bootstrap 95%, 2000 resamples",
            "internet": "disabled; loopback only",
        },
        "safety_notes": [],
        "benchmarks": {},
    }
    if profile == "safe":
        resultat["kind"] = "blink2video-stage0-preliminary-baseline"
        resultat["validity"] = "preliminary"
        resultat["safety_notes"].append(
            "Profil safe non qualifiant : P-01 est limité à deux échantillons "
            "sans chauffe après saturation de MsMpEng pendant la campagne conforme."
        )
        resultat["safety_notes"].append(
            "La première campagne quick est conservée séparément avec le statut "
            "invalid : ses temps contaminés par l'antivirus sont inutilisables."
        )
    if profile == "quick":
        resultat["safety_notes"].append(
            "P-07 est borné à 8 Mio : le flux 1 Gio est réservé au profil full; "
            "la capacité mémoire et disque doit être contrôlée avant ce profil."
        )
        resultat["safety_notes"].append(
            "P-02 à P-05 utilisent les volumes quick consignés dans chaque scénario; "
            "aucune extrapolation vers 100 000 entrées n'est revendiquée."
        )

    with tempfile.TemporaryDirectory(prefix="blink_benchmark_") as temporaire:
        racine = Path(temporaire)
        ancien_home, ancien_cwd = os.environ.get("BLINK_HOME"), Path.cwd()
        home_global, cwd_global = racine / "global-home", racine / "global-cwd"
        home_global.mkdir()
        cwd_global.mkdir()
        os.environ["BLINK_HOME"] = str(home_global)
        os.chdir(cwd_global)
        try:
            fonctions = [
                ("P-01", benchmark_p01), ("P-02", benchmark_p02),
                ("P-03", benchmark_p03), ("P-04", benchmark_p04),
                ("P-05", benchmark_p05), ("P-06", benchmark_p06),
                ("P-07", benchmark_p07), ("P-08", benchmark_p08),
                ("P-09", benchmark_p09), ("P-10", benchmark_p10),
                ("P-11", benchmark_p11),
            ]
            for identifiant, fonction in fonctions:
                if identifiant not in selection:
                    resultat["benchmarks"][identifiant] = {
                        "status": "not_run", "mode": "excluded_by_operator",
                    }
                    continue
                print(f"{identifiant}...", flush=True)
                sous_dossier = racine / identifiant.lower()
                sous_dossier.mkdir()
                try:
                    resultat["benchmarks"][identifiant] = fonction(config, sous_dossier)
                except Exception as erreur:
                    resultat["benchmarks"][identifiant] = {
                        "status": "error", "mode": "not_completed",
                        "error": f"{type(erreur).__name__}: {erreur}",
                    }
            if "P-12" in selection and resultat["benchmarks"]["P-01"].get("status") == "measured":
                p12_racine = racine / "p12"
                p12_racine.mkdir()
                resultat["benchmarks"]["P-12"] = benchmark_p12(
                    config, p12_racine, resultat["benchmarks"]["P-01"],
                )
            else:
                resultat["benchmarks"]["P-12"] = {
                    "status": "not_run", "mode": "excluded_or_requires_p01",
                }
        finally:
            os.chdir(ancien_cwd)
            if ancien_home is None:
                os.environ.pop("BLINK_HOME", None)
            else:
                os.environ["BLINK_HOME"] = ancien_home
    statuts_invalides = {"partial", "not_run", "error"}
    tous_qualifiants = all(
        benchmark.get("status") not in statuts_invalides
        and all(
            scenario.get("correctness") is not False
            for scenario in (benchmark.get("scenarios") or {}).values()
        )
        for benchmark in resultat["benchmarks"].values()
    )
    if selection != tous:
        resultat["validity"] = "partial"
    elif profile == "safe":
        resultat["validity"] = "preliminary"
    elif not tous_qualifiants:
        resultat["validity"] = "invalid"
    else:
        resultat["validity"] = "valid"
    return resultat


def ecrire_resultat(resultat, label, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    label_sain = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip(".-") or "run"
    horodatage = resultat["metadata"]["run_id"]
    base = output_dir / (
        f"stage0-baseline-{horodatage}-{resultat['metadata']['profile']}-{label_sain}"
    )
    # `run_id` contient un point avant les microsecondes : `with_suffix()`
    # prendrait tout ce qui suit pour une extension et supprimerait le profil
    # ainsi que le libellé. Construire le nom complet les conserve.
    json_path = Path(f"{base}.json")
    md_path = Path(f"{base}.md")
    # Mode exclusif : une collision, même improbable, est signalée plutôt que
    # d'écraser silencieusement une campagne antérieure.
    with json_path.open("x", encoding="utf-8") as sortie:
        sortie.write(json.dumps(resultat, indent=2, ensure_ascii=False) + "\n")
    try:
        with md_path.open("x", encoding="utf-8") as sortie:
            sortie.write(markdown(resultat))
    except Exception:
        json_path.unlink(missing_ok=True)
        raise
    return json_path, md_path


def valeurs_medianes(document):
    valeurs = {}
    for identifiant, benchmark in document.get("benchmarks", {}).items():
        for scenario, donnees in (benchmark.get("scenarios") or {}).items():
            mesure = donnees.get("duration_ms")
            if isinstance(mesure, dict) and mesure.get("median") is not None:
                valeurs[f"{identifiant}/{scenario}"] = float(mesure["median"])
    return valeurs


def comparer(paths, mode, regression_median_percent, improvement_percent):
    gauche = json.loads(Path(paths[0]).read_text(encoding="utf-8"))
    droite = json.loads(Path(paths[1]).read_text(encoding="utf-8"))
    incompatibilites = []
    for champ in ("schema_version",):
        if gauche.get(champ) != droite.get(champ):
            incompatibilites.append(champ)
    if gauche.get("metadata", {}).get("profile") != droite.get("metadata", {}).get("profile"):
        incompatibilites.append("profile")
    if gauche.get("protocol") != droite.get("protocol"):
        incompatibilites.append("protocol")
    meta_gauche = gauche.get("metadata", {})
    meta_droite = droite.get("metadata", {})
    for champ in (
        "implementation", "python", "os", "processor", "logical_cpu",
        "power_plan", "dependencies",
    ):
        if meta_gauche.get(champ) != meta_droite.get(champ):
            incompatibilites.append(f"environment.{champ}")
    if mode == "stability" and (
        meta_gauche.get("executed_production_sha256")
        != meta_droite.get("executed_production_sha256")
    ):
        incompatibilites.append("production_sha256")
    validites_permises = {"valid", "protocol_deviation"}
    if (
        gauche.get("validity") not in validites_permises
        or droite.get("validity") not in validites_permises
    ):
        incompatibilites.append("validity")
    a, b = valeurs_medianes(gauche), valeurs_medianes(droite)
    if set(a) != set(b):
        incompatibilites.append("scenarios")
    if incompatibilites:
        print("Comparaison refusée : " + ", ".join(sorted(set(incompatibilites))))
        return 2
    print("scenario\tbaseline_ms\tcandidate_ms\tdelta_percent\tverdict")
    echecs = 0
    for cle in sorted(a):
        delta = ((b[cle] - a[cle]) / a[cle] * 100) if a[cle] else 0.0
        if mode == "stability":
            accepte = abs(delta) <= regression_median_percent
            verdict = "stable" if accepte else "unstable"
        else:
            accepte = delta <= -improvement_percent
            verdict = "improved" if accepte else "improvement_not_proven"
        echecs += not accepte
        print(f"{cle}\t{a[cle]:.6f}\t{b[cle]:.6f}\t{delta:+.2f}\t{verdict}")
    return 1 if echecs else 0


def main():
    parseur = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parseur.add_argument("--profile", choices=tuple(PROFILES), default="quick")
    parseur.add_argument("--label", default="run-a")
    parseur.add_argument("--output-dir", type=Path, default=RESULTS)
    parseur.add_argument(
        "--only", metavar="P-01,P-02,...",
        help="n'exécuter que ces IDs; les autres restent not_run dans le JSON",
    )
    parseur.add_argument("--compare", nargs=2, metavar=("BASELINE", "CANDIDATE"))
    parseur.add_argument("--compare-mode", choices=("stability", "candidate"),
                         default="stability")
    parseur.add_argument("--max-median-regression-percent", type=float, default=5.0)
    parseur.add_argument("--min-improvement-percent", type=float, default=5.0)
    args = parseur.parse_args()
    if args.compare:
        return comparer(
            args.compare, args.compare_mode,
            args.max_median_regression_percent, args.min_improvement_percent,
        )
    selection = None
    if args.only:
        selection = [element.strip().upper() for element in args.only.split(",")
                     if element.strip()]
        inconnus = sorted(set(selection) - {f"P-{n:02d}" for n in range(1, 13)})
        if inconnus:
            parseur.error("ID inconnu : " + ", ".join(inconnus))
    resultat = collecter(args.profile, selection)
    json_path, md_path = ecrire_resultat(resultat, args.label, args.output_dir)
    print(f"JSON : {json_path}")
    print(f"Markdown : {md_path}")
    if resultat["validity"] != "valid":
        print(f"Campagne non qualifiante : {resultat['validity']}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
