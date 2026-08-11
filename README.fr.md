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

## Ce que ça donne

| Récupéré | Assemblé | Trié |
|---|---|---|
| Tous les clips du stockage local du module, conservés avant que la rotation ne les efface. | Une vidéo par jour, par semaine et par mois, l'heure d'enregistrement incrustée dans l'image. | Une page locale pour visionner, écarter, et voir les caméras en direct. |

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

Une commande, un verbe par action.

```bash
blink login          # connexion au compte Blink, vérification en deux étapes gérée
blink list           # ce que contient le module en ce moment
blink download       # récupérer les nouveaux clips dans Blink_Clips/
blink merge          # normaliser et assembler les vidéos
blink all            # télécharger puis assembler
blink review         # ouvrir l'interface web
blink watch --loop   # surveillance continue, notifications, assemblage automatique
```

Les arguments qui suivent le verbe sont transmis au programme correspondant :
`blink review --port 8899` fonctionne, et `blink review --help` affiche les
options de ce programme.

### L'interface web

`blink review` sert une page sur `127.0.0.1:8765` et l'ouvre. Quatre vues :

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
blink review                                   # bouton « Écarter » sur la carte
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

Pour la lancer avec la session sous Windows :

```powershell
Register-ScheduledTask -TaskName "Blink" -Action (New-ScheduledTaskAction `
  -Execute "C:\chemin\vers\blink.exe" -Argument "watch --loop") `
  -Trigger (New-ScheduledTaskTrigger -AtLogOn)
```

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

Les notifications de bureau sont propres à Windows. Ailleurs, la surveillance
écrit dans son journal et sur la sortie standard.

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

## S'appuie sur

[blinkpy](https://github.com/fronzbot/blinkpy) pour l'API Blink, y compris
l'implémentation du flux `immis`, et [ffmpeg](https://ffmpeg.org/) pour tout ce
qui touche à la vidéo. Les notes de protocole de
[BlinkMonitorProtocol](https://github.com/MattTW/BlinkMonitorProtocol) ont été
utiles pour comprendre ce que l'API offre, et ce qu'elle n'offre pas.

Sans lien avec Blink ni Amazon.
