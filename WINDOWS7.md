# Édition legacy Windows 7

Le bundle Windows normal embarque Python 3.12, qui ne fonctionne pas sous
Windows 7 et provoque notamment l'erreur
`api-ms-win-core-path-l1-1-0.dll manquant`. L'édition legacy utilise le
dernier runtime officiel adapté à Windows 7 : **CPython 3.8.10 x64**.

Elle embarque le code actuel de `blinkpy 0.25.9`. Seules les métadonnées de sa
roue sont rétroportées vers Python 3.8, avec les dernières dépendances encore
installables sur cette version. Le code reçu de PyPI est vérifié par son
SHA-256 avant modification.

Le direct propose **WebRTC et MSE**, au choix dans les réglages. Le profil
legacy embarque une pile WebRTC figée pour Python 3.8 et les DLL Windows 7 :
`aiortc 1.9.0`, `PyAV 12.3.0`, `cryptography 42.0.8`, `pyOpenSSL 24.1.0` et
`pylibsrtp 0.10.0`. Ces versions ne remplacent pas celles du bundle moderne.
MSE reste disponible ; aucune modification du TLS du relais Blink n'est
nécessaire pour ce portage.

Le magasin de certificats Mozilla de `certifi` complète celui de Windows 7 :
les connexions Blink restent strictement vérifiées même si les autorités
racines récentes ne sont plus distribuées à cette ancienne installation.

## Construire l'artefact

Le workflow `build-win7.yml` tourne automatiquement à chaque poussée sur
`main`, aux côtés des contrôles des éditions normales. Il peut aussi être
lancé à la main dans GitHub via **Actions → Build Windows 7 (legacy)
→ Run workflow** ; l'artefact téléchargeable s'appelle alors
`blink2video-windows7-x86_64-legacy`.

La même recette est réutilisée (`workflow_call`) par `release.yml` : à chaque
étiquette de release stable `vX.Y.Z`, le zip
`blink2video-windows7-x86_64-legacy.zip` est publié comme asset
supplémentaire de la [dernière release](https://github.com/nico579/blink2video/releases/latest),
au même tag que les trois éditions normales, plutôt que sur un tag ou une
numérotation `experimental.N` séparés. Chaque build est vérifié
automatiquement avant publication (démarrage, ffmpeg, TLS Blink, suite de
tests complète), mais contrairement aux trois autres archives, aucun
`.sha256` n'est publié à côté. Un échec de ce job n'empêche jamais la
publication des trois éditions stables : c'est une édition best-effort, hors
support Microsoft.

Le build exige également que WebRTC soit réellement importable et exécute
dans le bundle un échange H.264 High local : MPEG-TS, ICE/DTLS/SRTP, trois
images décodées et fermeture des connexions. Les imports PE normaux et
différés sont contrôlés contre une liste d'API post-Win7 connues. Ce contrôle
statique ne remplace pas l'exécution sur Windows 7.

La validation manuelle sur une vraie VM Windows 7 SP1 (section suivante)
reste recommandée avant de faire confiance à un build pour un usage réel :
les vérifications automatiques ci-dessus ne la remplacent pas.

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
blink2video.exe smoketest --webrtc --report webrtc.json
blink2video.exe login
blink2video.exe list
blink2video.exe download --from usb
blink2video.exe merge
blink2video.exe serve
```

`--version` doit contenir `Windows 7 legacy`. Vérifier ensuite le 2FA, le
téléchargement USB Gen2 et cloud, le direct, puis `start`, `stop` et
`autostart`. IE11 n'est pas une cible.

Pour le direct WebRTC sous Windows 7, le navigateur testé est
[Supermium 144.0.7559.256 R5](https://github.com/win32ss/supermium/releases/tag/v144-r5),
édition x64. Le 6 septembre 2026, dans une VM VirtualBox Windows 7 SP1,
les caméras Salon, Terrasse1 et jardin ont produit des images décodées et
une lecture qui avance en WebRTC, sans réencodage. Le contrôle MSE sur Salon
a également réussi, avec libération du module après chaque arrêt. Un second
essai en fenêtre normale a confirmé la présentation continue des images
WebRTC sur les trois caméras, avec `requestVideoFrameCallback` et progression
du temps de lecture.

Le serveur conserve le flux H.264 High émis par Blink. Quand le navigateur
ne propose pas directement High mais annonce un décodeur **High 4:4:4
Predictive**, la négociation peut sélectionner ce décodeur : cela ne
transforme pas la vidéo en 4:4:4 et n'installe aucun codec. Le serveur conserve
également les identifiants de charge utile RTP négociés dans la plage 35–63
(41 dans cet essai), au lieu de les remplacer par 112. La plage 64–95 reste
exclue pour éviter les conflits avec RTCP, conformément à la
[RFC 5761, section 4](https://www.rfc-editor.org/rfc/rfc5761.html#section-4).

Cette validation concerne ce navigateur et ces caméras, pas tous les niveaux
H.264 : notamment, les sources 1080p annoncent `640028` (High, niveau 4.0),
alors que l'offre de Supermium contient `f4001f` (High 4:4:4 Predictive,
niveau 3.1). Leur décodage a été constaté ici ; une offre limitée au niveau
3.1 ne garantit pas à elle seule la réception d'un flux de niveau 4.0 sur
un autre navigateur ou matériel.

Sur cette même VM, Firefox ESR 115 avec OpenH264 2.6 ne propose que Baseline
en WebRTC : **conserver MSE avec Firefox**, même si le diagnostic natif High
réussit. Une extension ou un pack de codecs système ne remplace pas les
profils annoncés par sa pile WebRTC. Sans aucun H.264 dans l'offre,
l'interface refuse le démarrage avant de réveiller la caméra et indique
de vérifier OpenH264 ou de sélectionner MSE. MSE reste accessible dans les
réglages, y compris avec Supermium.

Le diagnostic `--webrtc` ne contacte aucune caméra, ne lit aucun jeton Blink
et n'envoie aucune notification. Le JSON reste consultable même si le bundle
n'a pas de console : il doit contenir `ok: true`, `frames_decoded: 3` et
`closed: true`. Tester ensuite une caméra réelle dans le navigateur, puis
sélectionner MSE dans les réglages pour vérifier les deux transports.

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
