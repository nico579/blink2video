"""Diagnostic en lecture seule du stockage local d'un Sync Module XR.

Ce point d'entrée sert au petit exécutable d'assistance ``Tester-XR``. Placé à
côté de ``blink2video.exe``, il réutilise la session déjà enregistrée, lit le
homescreen puis le manifeste local, et ouvre un rapport texte anonymisé. Il ne
télécharge ni ne supprime aucun clip et ne change aucun réglage Blink.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import importlib.metadata
import io
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

import runtime


REPORT_NAME = "blink2video-XR-test-report.txt"
TESTER_REVISION = "xr-local-storage-v2"
_REQUIRED_CLIP_FIELDS = frozenset({"id", "camera_name", "created_at", "size"})

def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _presence(value) -> str:
    """Signale seulement la présence d'un identifiant, jamais sa valeur."""
    return "yes" if value not in (None, "") else "no"


def _safe_error(error: BaseException) -> str:
    """Classe et catégorie allowlistée, sans recopier le message arbitraire."""
    message = str(error).casefold()
    if "manifest request" in message:
        category = "manifest request rejected"
    elif "manifest response" in message:
        category = "invalid manifest response"
    elif "pas compatible" in message or "not compatible" in message:
        category = "local storage incompatible"
    elif "pas activ" in message or "not enabled" in message:
        category = "local storage disabled"
    elif "pas actif" in message or "not active" in message:
        category = "local storage inactive"
    elif "occup" in message or "busy" in message:
        category = "module busy"
    elif "auth" in message or "login" in message or "session" in message:
        category = "authentication failure"
    elif "timeout" in message or "timed out" in message:
        category = "timeout"
    else:
        category = "unexpected error"
    fingerprint = hashlib.sha256(
        f"{type(error).__name__}:{str(error)}".encode("utf-8", errors="replace")
    ).hexdigest()[:10]
    return f"{type(error).__name__}: {category} (reference {fingerprint})"


def _flag(mapping: dict, key: str) -> str:
    if key not in mapping:
        return "unknown"
    return "yes" if bool(mapping[key]) else "no"


def _count_list(mapping: dict, key: str) -> int:
    value = mapping.get(key)
    return len(value) if isinstance(value, (list, tuple)) else 0


def _environment_lines() -> list[str]:
    os_name = platform.system() or "unknown"
    os_release = platform.release() or "unknown"
    machine = platform.machine() or "unknown"
    return [
        f"Generated (UTC): {dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}",
        f"Tester revision: {TESTER_REVISION}",
        f"blink2video version: {runtime.VERSION}",
        f"blinkpy version: {_package_version('blinkpy')}",
        f"Python: {platform.python_version()}",
        f"Platform: {os_name} {os_release} ({machine})",
    ]


async def probe_local_manifest(sync) -> dict[str, int]:
    """Sonde directement les deux routes du manifeste, sans recopier leur contenu."""
    request = await sync.poll_local_storage_manifest()
    if not isinstance(request, dict) or request.get("id") in (None, ""):
        raise RuntimeError("manifest request response missing identifier")

    response = await sync.poll_local_storage_manifest(request["id"])
    if not isinstance(response, dict) or response.get("manifest_id") in (None, ""):
        raise RuntimeError("manifest response missing manifest identifier")
    raw_clips = response.get("clips")
    if not isinstance(raw_clips, (list, tuple)):
        raise RuntimeError("manifest response missing clips list")

    clips = [clip for clip in raw_clips if isinstance(clip, dict)]
    camera_references = {
        str(clip["camera_name"])
        for clip in clips
        if clip.get("camera_name") not in (None, "")
    }
    names = getattr(sync, "_names_table", None)
    names = names if isinstance(names, dict) else {}
    return {
        "clips": len(raw_clips),
        "schema_clips": sum(
            1 for clip in clips if _REQUIRED_CLIP_FIELDS.issubset(clip)
        ),
        "mapped_clips": sum(
            1 for clip in clips if clip.get("camera_name") in names
        ),
        "cameras": len(camera_references),
    }


async def inspect_blink(
    blink, select_modules=None, manifest_probe=None,
) -> tuple[int, list[str]]:
    """Inspecte les métadonnées nécessaires, sans exposer leurs valeurs privées."""
    if select_modules is None:
        import blink_models

        select_modules = select_modules or blink_models.select_sync_modules
    manifest_probe = manifest_probe or probe_local_manifest

    homescreen = getattr(blink, "homescreen", None)
    homescreen = homescreen if isinstance(homescreen, dict) else {}
    legacy = getattr(blink, "sync", None) or {}
    legacy_objects = list(legacy.values()) if isinstance(legacy, dict) else []
    legacy_classes = sorted({type(sync).__name__ for sync in legacy_objects})

    lines = [
        "Authentication: OK (saved session)",
        "Homescreen inventory: "
        f"networks={_count_list(homescreen, 'networks')}, "
        f"sync_modules={_count_list(homescreen, 'sync_modules')}, "
        f"cameras={_count_list(homescreen, 'cameras')}, "
        f"owls={_count_list(homescreen, 'owls')}, "
        f"doorbells={_count_list(homescreen, 'doorbells')}",
        f"Legacy blink.sync entries: {len(legacy_objects)}"
        + (f" ({', '.join(legacy_classes)})" if legacy_classes else ""),
    ]

    try:
        modules = list(select_modules(blink, None))
    except Exception as error:
        lines.extend([
            f"Modern module discovery: FAILED ({_safe_error(error)})",
            "Overall result: FAIL - Sync Module discovery failed.",
        ])
        return 1, lines

    lines.append(f"Modern module discovery: {len(modules)} module(s)")
    if not modules:
        lines.append("Overall result: FAIL - no Sync Module was discovered.")
        return 1, lines

    successful = 0
    clips_seen = 0
    has_unusable_clips = False
    for index, (_name, sync) in enumerate(modules, start=1):
        storage = getattr(sync, "_local_storage", None)
        storage = storage if isinstance(storage, dict) else {}
        try:
            active = "yes" if bool(getattr(sync, "local_storage")) else "no"
        except Exception:
            active = "unknown"

        lines.extend([
            "",
            f"Module {index}:",
            f"  class: {type(sync).__name__}",
            f"  network identifier present: {_presence(getattr(sync, 'network_id', None))}",
            f"  Sync Module identifier present: {_presence(getattr(sync, 'sync_id', None))}",
            "  local storage: "
            f"compatible={_flag(storage, 'compatible')}, "
            f"enabled={_flag(storage, 'enabled')}, active={active}",
        ])
        if active == "no":
            lines.append("  manifest API: SKIPPED (local storage is not active)")
            continue
        if _flag(storage, "compatible") == "no":
            lines.append(
                "  compatibility flag: informational; active storage will be probed"
            )
        try:
            # Même verrou inter-processus que le téléchargeur. Le testeur peut
            # ainsi être lancé pendant que l'interface tourne sans envoyer une
            # deuxième commande concurrente au module.
            with runtime.verrou(
                "hub", f"xr-diagnostic-{os.getpid()}", attente=10,
            ):
                stats = await manifest_probe(sync)
        except Exception as error:
            lines.append(f"  manifest API: FAILED ({_safe_error(error)})")
            continue

        successful += 1
        clip_count = int(stats["clips"])
        schema_count = int(stats["schema_clips"])
        mapped_count = int(stats["mapped_clips"])
        clips_seen += clip_count
        has_unusable_clips |= (
            schema_count != clip_count or mapped_count != clip_count
        )
        lines.extend([
            "  manifest API: OK",
            f"  clips reported by API: {clip_count}",
            f"  clips matching expected schema: {schema_count}/{clip_count}",
            f"  clips mapped to known cameras: {mapped_count}/{clip_count}",
            f"  cameras represented: {int(stats['cameras'])}",
        ])

    if successful == len(modules) and clips_seen and not has_unusable_clips:
        lines.append(
            "\nOverall result: PASS_WITH_CLIPS - the manifest API is accessible "
            "and its clips are usable by blink2video."
        )
        return 0, lines
    if successful == len(modules) and clips_seen:
        lines.append(
            "\nOverall result: PARTIAL - the manifest API is accessible, but not "
            "every clip is usable by blink2video yet."
        )
        return 1, lines
    if successful == len(modules):
        lines.append(
            "\nOverall result: PASS_EMPTY - the manifest API is accessible but "
            "currently reports no clips."
        )
        return 0, lines
    if successful:
        lines.append(
            f"\nOverall result: PARTIAL - {successful}/{len(modules)} manifest API "
            "request(s) succeeded."
        )
        return 1, lines
    lines.append("\nOverall result: FAIL - no local storage manifest API was accessible.")
    return 1, lines


async def collect_report(session_factory=None, connector=None) -> tuple[int, str]:
    """Construit le rapport complet à partir de la seule session sauvegardée."""
    header = [
        "blink2video XR local-storage diagnostic",
        "=======================================",
        *_environment_lines(),
        "",
        "Safety: read-only account/module inventory and local manifest requests.",
        "No clip is downloaded or deleted and no camera setting is changed.",
        "Privacy: no password, token, serial number, camera/network name, media URL,",
        "raw Blink response or raw API identifier is included in this report.",
        "",
    ]

    if session_factory is None or connector is None:
        import blink_auth

        session_factory = session_factory or blink_auth.session_http_temporaire
        connector = connector or blink_auth.connect_saved

    try:
        # blinkpy écrit quelques informations destinées au terminal (dont le
        # chemin de session). Elles ne doivent jamais se retrouver dans le
        # rapport transmissible ; on les absorbe entièrement.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            async with session_factory() as session:
                blink = await connector(session)
                if blink is None:
                    return 2, "\n".join(header + [
                        "Authentication: MISSING OR EXPIRED",
                        "Overall result: LOGIN REQUIRED - open blink2video, sign in, then run",
                        "Tester-XR again from the same folder as blink2video.",
                    ]) + "\n"
                code, details = await inspect_blink(blink)
    except Exception as error:
        return 1, "\n".join(header + [
            f"Diagnostic execution: FAILED ({_safe_error(error)})",
            "Overall result: FAIL - the diagnostic could not complete.",
        ]) + "\n"
    return code, "\n".join(header + details) + "\n"


def report_path() -> Path:
    """Place le fichier à côté du testeur, là où l'utilisateur le retrouvera."""
    if runtime.frozen():
        return Path(sys.executable).resolve().parent / REPORT_NAME
    return runtime.app_dir() / REPORT_NAME


def write_report(path: Path, content: str) -> None:
    """Écriture atomique : jamais de rapport tronqué à transmettre."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.stem}.{os.getpid()}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_report_with_fallback(path: Path, content: str) -> Path:
    """Replie le rapport dans le dossier temporaire si l'installation est en lecture seule."""
    try:
        write_report(path, content)
        return path
    except OSError:
        fallback = Path(tempfile.gettempdir()) / REPORT_NAME
        if fallback.resolve() == path.resolve():
            raise
        write_report(fallback, content)
        return fallback


def _show_message(message: str, title: str = "Blink XR test") -> None:
    if os.name == "nt":
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, message, title, 0x40)
    else:
        print(f"{title}: {message}")


def _open_report(path: Path) -> None:
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def main() -> int:
    # Crochet réservé à la validation automatisée du binaire construit. Il ne
    # change pas le diagnostic, seulement les fenêtres et l'ouverture du bloc-
    # notes, impossibles sur un runner CI sans bureau interactif.
    headless = os.environ.get("BLINK_XR_TEST_NO_UI") == "1"
    if not headless:
        _show_message(
            "Keep Tester-XR.exe in the same folder as blink2video.exe.\n\n"
            "This read-only test checks the XR microSD manifest using the saved "
            "Blink session.\n\n"
            "It can take a few minutes. Click OK to start; the report will open "
            "automatically when finished."
        )
    path = report_path()
    try:
        # Le binaire PyInstaller contient déjà ses dépendances. Depuis les
        # sources, conserver le bootstrap ordinaire de blink2video.
        if not runtime.frozen():
            runtime.bootstrap()
        code, report = asyncio.run(collect_report())
        path = write_report_with_fallback(path, report)
    except Exception as error:
        fallback = "\n".join([
            "blink2video XR local-storage diagnostic",
            "=======================================",
            *_environment_lines(),
            "",
            f"Tester failure: {_safe_error(error)}",
            "Overall result: FAIL - the tester itself could not complete.",
        ]) + "\n"
        try:
            path = write_report_with_fallback(path, fallback)
        except Exception as write_error:
            _show_message(
                "The tester could not create its report.\n\n"
                + _safe_error(write_error),
                title="Blink XR test failed",
            )
            return 1
        code = 1

    if headless:
        return code
    _show_message(
        "The test is complete. The report will now open.\n\n"
        "You may paste its contents into the Reddit reply; the report contains "
        "only anonymized statuses and counts."
    )
    try:
        _open_report(path)
    except Exception:
        _show_message(
            "The report was created but could not be opened automatically.\n\n"
            f"File: {path}",
            title="Blink XR test report",
        )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
