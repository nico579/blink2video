*[English](README.md) | **Français***

# blink2video

**Gérez vos caméras Blink depuis un ordinateur, et gardez ce qu'elles filment.**

Blink est pensé pour le téléphone. Son application montre un clip à la fois, ne
conserve aucune archive, et n'a pas d'équivalent sur ordinateur. Les clips vivent
sur une clé USB branchée au module de synchronisation, effacés à mesure qu'elle
se remplit.

blink2video est ce versant manquant. Une interface locale, sur votre machine,
pour regarder les caméras en direct, armer ou désarmer la détection, suivre
l'état de l'installation, et revoir ce qui a été enregistré sur un vrai écran
avec un vrai clavier. Et ce que le téléphone ne sait pas faire du tout : les
clips sont récupérés avant que la rotation ne les efface, horodatés dans l'image,
et assemblés en une vidéo par jour, par semaine ISO et par mois.

Tout tourne sur votre machine. Rien n'est envoyé ailleurs.

## Fonctionnalités

**Regarder et commander**
- Direct de n'importe quelle caméra dans le navigateur, armement ou désarmement
  du système entier ou d'une seule caméra, depuis un vrai écran.
- Batterie, température, signal Wi-Fi et liaison au module pour chaque caméra,
  chaque relevé daté : une caméra hors de portée continue d'annoncer ses
  dernières valeurs connues, et l'interface dit de quand elles datent.
- Modèle, micrologiciel et numéro de série de chaque caméra.

**Conserver**
- Téléchargement incrémental du stockage local du module, avant que la rotation
  n'efface les clips.
- Heure d'enregistrement incrustée dans chaque image, donc conservée par
  n'importe quel lecteur, téléphone ou messagerie.
- Une vidéo par jour, par semaine ISO et par mois, pour chaque caméra.
- Les clips écartés sont mis de côté plutôt que supprimés, et ne sont jamais
  retéléchargés.

**Être prévenu**
- Surveillance continue : caméra ou module hors ligne, batterie qui faiblit,
  détection coupée, système désarmé, ou caméra qui n'a rien enregistré depuis
  deux jours.
- Alertes sur changement uniquement, acquittées par une fenêtre. Mise en
  sourdine par caméra.
- Nouveaux clips récupérés et assemblés automatiquement, puis une notification
  qui ouvre l'interface au clic.

**Tourner partout**
- Bundle autonome pour Windows, Linux et macOS, ffmpeg inclus.
- Depuis les sources, le premier lancement construit son environnement isolé.

## Captures d'écran

*(à venir)*

## Installation

Téléchargez l'archive correspondant à votre système depuis la
[dernière version publiée](https://github.com/nico579/blink2video/releases/latest),
décompressez-la, et lancez `blink` depuis un terminal. Rien d'autre n'est
nécessaire : ffmpeg voyage dans le bundle.

Depuis les sources, avec Python 3.11 ou plus récent :

```bash
git clone https://github.com/nico579/blink2video
cd blink2video
python blink.py login
```

Le premier lancement crée un environnement isolé dans `~/.blink/venv`, y
installe les trois dépendances, et s'y relance. `--bootstrap=pip` installe dans
l'environnement courant, `--bootstrap=none` laisse la gestion des dépendances à
votre charge.

## Utilisation



<!-- verbes:début -->
Une commande, un verbe par action. `blink <verbe> --help` donne les options de chacun.

```bash
blink login       # se connecter au compte Blink, vérification en deux étapes gérée
blink list        # ce que contient le module de synchronisation en ce moment
blink download    # récupérer les nouveaux clips avant que la rotation ne les efface
blink merge       # normaliser, horodater et assembler jour, semaine et mois
blink all         # tout : contrôler l'état, télécharger, assembler
blink serve       # servir seulement l'interface web : visionnage, tri, direct, armement
blink watch       # contrôler l'état de l'installation et alerter s'il se dégrade
blink autostart   # lancer un verbe à l'ouverture de session, « all --serve --loop 10 » par défaut
blink smoketest   # vérifier que l'installation fonctionne sur cette machine
```
<!-- verbes:fin -->
Les arguments qui suivent le verbe sont transmis au programme correspondant :
`blink serve --port 8899` fonctionne, et `blink serve --help` affiche les
options de ce programme.

### L'interface web

`blink serve` sert une page sur `127.0.0.1:8765` et l'ouvre. Quatre vues :

- **Direct** : une tuile par caméra avec sa dernière vignette, l'armement au
  niveau du système et de chaque caméra, la batterie, la température, le signal,
  et la date de chaque relevé.
- **Clips** : tous les clips du plus récent au plus ancien, avec une image
  d'aperçu, et un bouton pour écarter ce qui n'a pas d'intérêt.
- **Journalières, Hebdomadaires, Mensuelles** : les vidéos assemblées, avec
  leurs durées.

Le bouton Actualiser télécharge les nouveaux clips et reconstruit les vidéos, en
affichant l'avancement.

### Écarter un clip

La détection se déclenche sur une ombre, un oiseau, un nuage qui passe. Écarter
retire le clip de toutes les vidéos assemblées :

```bash
blink serve                                   # bouton « Écarter » sur la carte
blink merge --exclude Blink_Clips/jardin/2026-08/2026-08-10_14-05-04Z_jardin.mp4
```

Le brut est déplacé dans `Blink_Excluded/` plutôt que supprimé, une marque est
posée pour qu'il ne soit plus jamais retéléchargé, et la journée, la semaine et
le mois sont reconstruits sans lui. `--include` défait le tout.

### Surveillance

```bash
blink watch --loop
```

Contrôle toutes les dix minutes. Une caméra qui passe hors ligne, une batterie
qui n'est plus bonne, une détection coupée, ou une caméra qui n'a rien
enregistré depuis deux jours ouvrent une fenêtre à acquitter. Les nouveaux clips
sont récupérés, les vidéos reconstruites, et une notification le signale, avec
un clic qui ouvre l'interface.

Les alertes ne se déclenchent que sur un changement : une caméra que vous
laissez sciemment hors ligne ne prévient qu'une fois. `--ignore "Portail"` la
met en sourdine définitivement.

Pour qu'elle démarre avec votre session, voir
[Lancer la surveillance avec la session](#lancer-la-surveillance-avec-la-session).

## Vérifier son installation

```bash
blink smoketest
```

Produit une vraie vidéo horodatée que vous pouvez ouvrir, fait apparaître une
vraie notification, et dit ce qu'il en est de ffmpeg, de la police, de la
session Blink et du démarrage automatique. Le contrôle de l'horodatage ne se
contente pas de vérifier que le filtre existe : il normalise un clip noir et
compte les pixels allumés, seule preuve qu'une heure est bien écrite.

## Lancer la surveillance avec la session

Une commande suffit, elle emploie le mécanisme propre à votre système :

```bash
blink autostart on        # installer
blink autostart           # savoir où l'on en est
blink autostart off       # retirer
```

Ajoutez `--dry-run` pour voir ce qui serait fait sans rien modifier. Aucun droit
d'administrateur n'est nécessaire.

Si vous préférez le faire vous-même, voici ce que la commande met en place.

### Windows

Un raccourci dans le dossier de démarrage. Le planificateur de tâches
conviendrait aussi, mais son dossier racine demande une élévation :

```powershell
Register-ScheduledTask -TaskName "blink2video" -Action (New-ScheduledTaskAction `
  -Execute "C:\path	olink.exe" -Argument "watch --loop") `
  -Trigger (New-ScheduledTaskTrigger -AtLogOn)
```

Si la commande renvoie « Accès refusé », passez par le dossier de démarrage,
qui ne demande aucun droit :

```powershell
$s = (New-Object -ComObject WScript.Shell).CreateShortcut(
  "$([Environment]::GetFolderPath('Startup'))link2video.lnk")
$s.TargetPath = "C:\path	olink.exe"; $s.Arguments = "watch --loop"
$s.WorkingDirectory = "C:\path	o"; $s.Save()
```

L'un ou l'autre, jamais les deux : deux lanceurs donnent deux surveillances,
et chaque notification en double.

### macOS

Un agent de lancement, chargé à l'ouverture de session :

```bash
mkdir -p ~/Library/LaunchAgents
cat > ~/Library/LaunchAgents/com.nico579.blink2video.plist <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.nico579.blink2video</string>
  <key>ProgramArguments</key>
  <array><string>/path/to/blink</string><string>watch</string><string>--loop</string></array>
  <key>WorkingDirectory</key><string>/path/to</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
PLIST
launchctl load ~/Library/LaunchAgents/com.nico579.blink2video.plist
```

`launchctl unload` sur le même fichier l'arrête. `KeepAlive` relance la
surveillance si elle venait à s'interrompre.

### Linux

Un service utilisateur systemd :

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/blink2video.service <<'UNIT'
[Unit]
Description=blink2video watcher

[Service]
ExecStart=/path/to/blink watch --loop
WorkingDirectory=/path/to
Restart=on-failure

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now blink2video
```

`journalctl --user -u blink2video -f` permet de la suivre. Les notifications
de bureau reposent sur `notify-send`, et la fenêtre d'acquittement sur
`zenity` ; sans eux, la surveillance écrit dans `watch.log` et continue.

## Toutes les options

`blink <verbe> --help` fait toujours foi ; ce tableau récapitule.

Le partage entre verbes et options suit une règle simple : un **verbe** est ce
que le programme fait, une **option** la manière dont il le fait. Une option qui
détournerait la commande de son objet, comme installer un démarrage automatique
au lieu de surveiller, est un verbe.

**Racine** : `login`, `list`, `download`. Sans verbe, l'aide s'affiche.

| Option | Effet |
|---|---|
| `--hub NOM` | module de synchronisation à utiliser |
| `--camera NOM` | ne garder que cette caméra |
| `--since JOURS` | ne garder que les clips des N derniers jours |
| `--output DOSSIER` | destination des clips bruts (défaut `Blink_Clips`) |
| `--overwrite` | remplacer les fichiers existants de taille différente |

**`blink merge`** : normalisation et assemblage.

| Option | Effet |
|---|---|
| `--exclude CLIP…` | écarter des clips : le brut part dans `Blink_Excluded`, le segment est effacé, le clip n'est plus retéléchargé |
| `--include CLIP…` | annuler une exclusion : le brut revient et le clip est re-normalisé |
| `--date AAAA-MM-JJ` | limiter à une journée |
| `--camera NOM` | limiter à une caméra |
| `--force` | tout reconstruire même si rien n'a changé |
| `--no-periods` | ne pas reconstruire les agrégats hebdomadaires et mensuels |
| `--preset NOM` | preset libx264, d'`ultrafast` à `veryslow` (défaut `veryfast`) |
| `--crf N` | qualité, 0 à 51, plus bas est meilleur (défaut 21) |
| `--font FICHIER` | police .ttf pour l'horodatage |
| `--timezone ZONE` | fuseau de l'horodatage (défaut `Europe/Paris`) |
| `--input`, `--output`, `--normalized-output`, `--excluded-output`, `--weekly-output`, `--monthly-output` | emplacements de chaque dossier |

**`blink serve`** : interface web.

| Option | Effet |
|---|---|
| `--port N` | port d'écoute (défaut 8765) |
| `--no-browser` | ne pas ouvrir le navigateur |
| `--hub NOM` | module de synchronisation |
| `--thumbs DOSSIER` | cache des vignettes, jetable |
| `--timezone ZONE` | fuseau d'affichage |
| les mêmes options de dossiers que `merge` | |

**`blink watch`** : contrôle de l'état, une fois.

| Option | Effet |
|---|---|
| `--ignore CAMERA…` | mettre une caméra en sourdine, plus aucune alerte |
| `--unignore CAMERA…` | lever la sourdine |
| `--test` | déclencher une notification de vérification |
| `--notify popup\|mail\|both` | canal d'alerte (défaut : boîte de dialogue) |
| `--dry-run` | montrer sans agir |

**`blink loop [verbes…]`** : répéter, en nommant quoi. Sans argument :
`watch download merge serve`.

| Option | Effet |
|---|---|
| `--interval MINUTES` | délai entre deux tours (défaut 10) |
| `--once` | un seul tour puis s'arrêter |
| `--port N` | port de l'interface, si `serve` est demandé |
| `--notify`, `--dry-run`, `--timezone` | comme pour `watch` |

**`blink autostart on\|off\|status [verbe…]`** : lancer un verbe à l'ouverture
de session, par le mécanisme du système. Sans verbe, `loop`. `--dry-run` montre
sans agir.

**`blink all`** : téléchargement puis assemblage. `--hub`, `--camera`, `--since`.

**`blink smoketest`** : contrôle de l'installation. `--keep` conserve le dossier
de travail, `--timezone` choisit le fuseau de la vidéo de démonstration.

**Variables d'environnement**

| Variable | Effet |
|---|---|
| `BLINK_HOME` | dossier des données, à défaut celui de l'exécutable |
| `BLINK_BOOTSTRAP` | `auto`, `pip` ou `none` : gestion de l'environnement Python |

## Où vont les fichiers

```
Blink_Clips/       clips bruts, tels que le module les a enregistrés
Blink_Normalized/  les mêmes avec l'heure incrustée, en miroir de l'arborescence
Blink_Excluded/    les clips écartés, conservés plutôt que supprimés
Blink_Daily/       une vidéo par caméra et par jour
Blink_Weekly/      une par semaine ISO
Blink_Monthly/     une par mois
```

À côté de l'exécutable, ou dans le dossier désigné par la variable
d'environnement `BLINK_HOME`.

## Comment ça marche

Le clip normalisé est le pivot. Chaque clip est ré-encodé une fois, l'heure
inscrite dans l'image, puis conservé définitivement. Les vidéos journalières,
hebdomadaires et mensuelles ne sont ensuite que des copies de flux de ces
segments : aucun ré-encodage, aucune perte de génération, quelques secondes
chacune.

C'est ce qui rend l'ajout d'un clip peu coûteux. Un nouvel arrivant ne ré-encode
que lui-même, puis le jour, la semaine et le mois sont réassemblés par copie.
L'encodage tourne à peu près au temps réel : un clip d'une minute coûte environ
une minute, une seule fois.

L'heure est incrustée dans l'image plutôt qu'ajoutée en piste de sous-titres,
parce que les pistes de sous-titres sont ignorées par les téléphones et les
messageries, et que ces vidéos sont faites pour être regardées n'importe où.

La conversion de fuseau horaire est faite côté Python et ffmpeg tourne avec
`TZ=UTC0` : aucune partie de la chaîne ne dépend d'une base de fuseaux horaires
fournie par le système.

Un clip est identifié par sa caméra et son instant d'enregistrement, jamais par
l'identifiant que lui attribue le module : redémarrer celui-ci renumérote tout,
et les clips déjà récupérés reviendraient comme neufs.

## Limites

L'outil ne voit que les clips présents sur le stockage local du module. Les
enregistrements qui ne vivent que dans le cloud lui échappent.

Le direct fonctionne sur les caméras que Blink diffuse par son protocole
`immis`. Une caméra hors de portée du module accepte la demande mais n'envoie
jamais d'image, et l'interface le dit.

Sous Linux, le binaire ffmpeg livré par imageio-ffmpeg est compilé sans
libfreetype : il ne sait pas incruster l'horodatage. Installez celui de votre
distribution, qui en est capable : `sudo apt install ffmpeg`. L'outil essaie
chaque ffmpeg qu'il trouve et retient le premier qui sait écrire du texte.
Windows et macOS n'ont besoin de rien.

Les notifications de bureau empruntent les outils de chaque système : la
fenêtre native sous Windows, osascript sous macOS, notify-send et zenity sous
Linux. À défaut, la surveillance écrit dans `watch.log` et continue.

Rien ne tourne pendant que l'ordinateur est éteint. Une caméra qui tombe la nuit
est signalée à l'ouverture de session suivante.

Blink n'expose aucun moyen de redémarrer un module de synchronisation. Quand il
se bloque, il faut le débrancher.

## Construction

```bash
python build.py
```

Crée un environnement de construction isolé, y installe les dépendances et
PyInstaller, et produit `dist/blink/`. Environ 110 Mo, dont la plus grande part
est ffmpeg.

PyInstaller n'est pas un compilateur croisé, et le binaire ffmpeg est propre à
chaque plateforme : chaque système exige sa propre construction. Le workflow de
publication s'en charge sur les runners GitHub, pour Windows, Linux et macOS.

## État du projet et usage responsable

blink2video est un projet indépendant, éprouvé au quotidien sous Windows 10 sur
une installation réelle : un module de synchronisation, quatre caméras, le
direct, l'armement et l'archive. Les exécutables Linux et macOS sont construits
et leur chaîne vidéo est vérifiée automatiquement sur les runners GitHub, mais
ils n'ont jamais tourné face à du vrai matériel Blink, et les notifications de
bureau n'y ont pas été vues à l'écran. Les retours et rapports reproductibles
sont les bienvenus dans les issues GitHub.

Filmez de manière responsable. En France, la CNIL admet qu'on surveille son
propre domicile, mais pas la voie publique, ni l'entrée des habitations
voisines, ni un espace partagé sans information des personnes. Cet outil ne fait
que conserver ce que vos caméras enregistrent déjà : il ne change rien à ce que
vous avez le droit de filmer, mais il rend cette question plus concrète en
constituant une archive durable là où l'application mobile ne gardait que
quelques jours.

L'archive produite est une donnée personnelle : elle contient des images de
votre domicile, les horaires de présence qui s'en déduisent, et le fichier de
session donne accès à votre compte Amazon. Ces fichiers restent sur votre
machine et ne sont jamais envoyés ailleurs, mais ils méritent le même soin que
vos autres données sensibles. Le `.gitignore` du dépôt les exclut tous.

## Licence, auteur et crédits

Le code est distribué sous GNU General Public License v3.0 ; voir
[LICENSE](LICENSE). Toute redistribution modifiée doit fournir le code source
correspondant sous la même licence. Ce choix est aussi celui qu'impose la
cohérence : le bundle Linux embarque une compilation GPL de ffmpeg, seule à
disposer de libfreetype et donc capable d'incruster l'horodatage.

Conçu et architecturé par Nicolas Martin ([@nico579](https://github.com/nico579)).
Code développé avec l'assistance de Claude (Anthropic) comme outil de
développement.

[blinkpy](https://github.com/fronzbot/blinkpy) fournit l'accès à l'API Blink,
y compris l'implémentation du protocole `immis` sans laquelle le direct serait
hors de portée, et [ffmpeg](https://ffmpeg.org/) fait tout le travail vidéo. Les
notes de rétro-ingénierie de
[BlinkMonitorProtocol](https://github.com/MattTW/BlinkMonitorProtocol) ont servi
à comprendre ce que l'API offre, et surtout ce qu'elle n'offre pas.

Sans lien avec Blink ni Amazon. Blink est une marque d'Amazon.
