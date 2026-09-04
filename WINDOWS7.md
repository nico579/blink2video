# Édition expérimentale Windows 7

Le bundle Windows normal embarque Python 3.12, qui ne fonctionne pas sous
Windows 7 et provoque notamment l'erreur
`api-ms-win-core-path-l1-1-0.dll manquant`. L'édition expérimentale utilise le
dernier runtime officiel adapté à Windows 7 : **CPython 3.8.10 x64**.

Elle embarque le code actuel de `blinkpy 0.25.9`. Seules les métadonnées de sa
roue sont rétroportées vers Python 3.8, avec les dernières dépendances encore
installables sur cette version. Le code reçu de PyPI est vérifié par son
SHA-256 avant modification.

Le direct utilise toujours MSE sur cette édition : `aiortc` (WebRTC) n'est pas
empaqueté pour Python 3.8, `blink_webrtc` reste donc indisponible et
l'application bascule automatiquement sur MSE, même si « webrtc » est choisi
dans les paramètres.

Le magasin de certificats Mozilla de `certifi` complète celui de Windows 7 :
les connexions Blink restent strictement vérifiées même si les autorités
racines récentes ne sont plus distribuées à cette ancienne installation.

## Construire l'artefact

Dans GitHub, ouvrir **Actions → Build Windows 7 (experimental) → Run workflow**.
Le résultat téléchargeable s'appelle
`blink2video-windows7-x86_64-experimental`. Ce workflow est aussi exécuté à
chaque évolution de `main`, parallèlement aux contrôles des éditions normales.

La [préversion experimental.1](https://github.com/nico579/blink2video/releases/tag/v0.10.0-win7-experimental.1)
correspond au code de la v0.10.0, construite et vérifiée automatiquement
(démarrage, ffmpeg, TLS Blink, suite de tests complète). Son SHA-256 est
`4B9331AE25B429BBC83865AA6E18E0613514FA28B9F25DBC52C17154F358E6AB`.
La validation manuelle sur une vraie VM Windows 7 SP1 (démarrage, connexion
Blink, 2FA, lecture des clips) reste à faire avant de la considérer aussi
éprouvée que l'experimental.3 précédente.

En local, la construction exige Windows x64 et l'interpréteur **CPython 3.8.10
officiel de python.org** :

```powershell
python build.py --win7 --propre
```

Les environnements et sorties restent séparés du build normal dans
`build_venv_win7`, `build-win7` et `dist-win7`. Les deux éditions partagent les
mêmes sources applicatives dans `main` ; seule cette enveloppe de construction
legacy est distincte.

## Préparer la VM

1. Installer Windows 7 SP1 64 bits, 2 CPU, 4 Go de RAM, réseau NAT, puis créer
   un snapshot propre.
2. Installer les mises à jour Microsoft nécessaires, au minimum KB2533623. Si
   une erreur UCRT apparaît, installer aussi KB2999226 et le redistribuable
   Visual C++ 2015–2019 x64 officiel, puis redémarrer.
3. Ne pas installer Python dans la VM : le test doit prouver que le bundle est
   réellement autonome.
4. Copier puis extraire l'archive dans `C:\blink7` (ne pas l'exécuter depuis le
   ZIP ni depuis un dossier partagé VirtualBox).
5. Ne jamais télécharger une DLL isolée depuis un site tiers.

## Essai progressif

Depuis `cmd.exe`, dans `C:\blink7\blink2video` :

```bat
blink2video.exe --version
blink2video.exe --help
blink2video.exe smoketest
blink2video.exe login
blink2video.exe list
blink2video.exe download --from usb
blink2video.exe merge
blink2video.exe serve
```

`--version` doit contenir `Windows 7 experimental`. Vérifier ensuite le 2FA, le
téléchargement USB Gen2 et cloud, le direct, puis `start`, `stop` et
`autostart`. Pour l'interface, utiliser Firefox ESR 115 ou Chromium 109 ; IE11
n'est pas une cible.

La notification de bureau repose actuellement sur l'API de toast Windows 10 :
son absence sous Windows 7 n'empêche ni les téléchargements ni les vidéos.

## Limites de sécurité et de maintenance

Windows 7, Python 3.8 et plusieurs dernières dépendances compatibles 3.8 sont
hors maintenance. Cette édition est donc **legacy / best effort**, à ne pas
exposer directement à Internet. La mise à jour automatique est désactivée :
elle installerait sinon l'archive Windows standard fondée sur Python 3.12 et
casserait immédiatement le démarrage sous Windows 7.

Après `login`, le dossier contient des jetons d'accès au compte Blink. Ne pas
publier la VM ni ses fichiers, et revenir au snapshot propre après les essais.
