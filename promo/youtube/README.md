# Présentation YouTube de blink2video

## Vidéos publiées

Chaîne : [Nico — Projets logiciels](https://www.youtube.com/@NicoProjetsLogiciels).

- [Présentation française](https://www.youtube.com/watch?v=V2E4m_SLgb4)
- [English presentation](https://www.youtube.com/watch?v=6rNqLI9K8Tc)

Les deux vidéos ont été vérifiées publiques dans YouTube Studio le 9 septembre
2026. Les descriptions comprennent les liens GitHub et la discussion Reddit.
`publication-status.json` consigne les références de publication.
Les commentaires explicatifs FR/EN ont été publiés sous @NicoProjetsLogiciels
et vérifiés sur les pages vidéo ; leurs liens sont dans `comment-results.json`.
Les sous-titres sont publiés dans chaque langue. Après la validation du numéro
de téléphone par le propriétaire, les deux miniatures personnalisées ont été
enregistrées et vérifiées visuellement depuis YouTube après rechargement.

## Versions française et anglaise

La version française finale est dans `export-v2/`, la version anglaise dans
`export-en/`. Les fichiers `publication-fr.md` et `publication-en.md` contiennent
les titres, descriptions et commentaires explicatifs, également disponibles
en JSON. Les chapitres de ces descriptions sont regroupés pour durer au moins
dix secondes.

`python promo/youtube/package_publication.py` rassemble les deux vidéos,
miniatures, sous-titres et textes dans `publication-ready/`. Ouvrir son
`index.html` pour relire les publications et copier les textes.
Ce dossier contient uniquement les livrables, aucune session de connexion.

La chaîne est destinée à plusieurs projets de développement logiciel ;
`chaine.md` propose sa présentation générale. La connexion Google et le profil
Chrome dédié restent hors du dépôt. L'ouverture de ce navigateur est effectuée
par `open_youtube.ps1`. Les fichiers locaux ne prouvent pas une publication :
seules les URL vidéo et leur visibilité vérifiées sur YouTube la confirment.

Vidéo française de 3 min 10 s, 1920 × 1080, 30 images/s, H.264 et AAC.
Sans voix off : textes incrustés, captures du projet et musique instrumentale
originale synthétisée par le script. Aucun morceau tiers utilisé.

## Livrables

Dans `export-v2/` après génération (la première version reste dans `export/`) :

- `blink2video-youtube-fr.mp4` : vidéo avec musique.
- `miniature-youtube.jpg` : miniature 1280 × 720.
- `blink2video.fr.srt` : sous-titres français séparés.
- `apercu.jpg` : planche des seize séquences.
- `musique-originale.wav` : musique séparée.
- `chapitres.txt` : repères temporels des séquences.

La deuxième version ajoute une introduction sur le besoin d'un historique
Blink sur ordinateur, puis un guide de téléchargement, lancement, connexion
et première validation. Trois séquences expliquent les réglages du quotidien,
les options vidéo, les alertes et la suppression facultative à la source.
Les instructions de lancement sont montrées pour Windows ; les commandes de
préparation propres à Linux et macOS restent dans la documentation liée.

## Refaire le montage

Depuis la racine : `python promo/youtube/render.py` pour le français,
ou `python promo/youtube/render.py --language en` pour l'anglais.
Requiert Pillow, numpy, les polices Segoe UI de Windows et ffmpeg (le script
cherche le binaire déjà embarqué dans le dépôt, puis le PATH).
Les textes et durées sont dans `SCENES`, et la traduction anglaise dans
`english.py`. Les exports sont ignorés par Git.

## Choix éditoriaux

Les fonctionnalités sont issues de `README.fr.md` consulté le 9 septembre 2026.
Il s'agit d'une présentation animée de captures existantes, pas d'un
enregistrement d'interactions ni d'un direct filmé. Les captures affichent
d'anciennes versions 0.10.0 et 0.12.1 ; elles montrent l'interface documentée.
Le direct est déjà flouté dans la capture du dépôt. Son cadrage exclut la ligne
d'identifiants matériels. Aucune lecture de clips privés ou de session Blink.

La musique est créée par synthèse de notes dans le script, sans voix ni modèle
génératif audio. Le montage est conçu pour rester compréhensible sans son.
La vidéo présente les archives sur ordinateur sans prétendre que les échanges
avec Blink fonctionnent hors ligne. Elle ne promet pas de contourner un abonnement.

## Publication proposée

**Titre :** Vos caméras Blink sur PC : direct et archives avec blink2video

**Description :**

Regardez vos caméras Blink sur votre ordinateur et conservez vos enregistrements
avec blink2video, un logiciel gratuit et open source.

Pourquoi blink2video ? Pour voir ses caméras sur ordinateur et garder un
historique des clips avant leur effacement.

Dans cette présentation sans voix off : retrouvez vos clips, filtrez par caméra
et par période, enregistrez le direct à la demande et assemblez vos vidéos par
jour, semaine et mois. Le logiciel récupère les clips du stockage local du
module (USB du Sync Module 2 ou microSD du XR) et du cloud de votre abonnement Blink.

La deuxième partie explique comment télécharger l'archive, lancer le logiciel,
se connecter à Blink et choisir les réglages : dossier, fuseau horaire,
démarrage automatique, fréquence de récupération, horodatage, direct,
archives, alertes et suppression facultative à la source.

Télécharger : https://github.com/nico579/blink2video/releases/latest
Documentation en français : https://github.com/nico579/blink2video/blob/main/README.fr.md
Code source et retours : https://github.com/nico579/blink2video

Windows, Linux et macOS. Usage réel éprouvé sous Windows ; les bundles Linux
et macOS sont construits et vérifiés automatiquement, sans validation sur
caméra réelle à ce jour selon la documentation du projet.
L'ordinateur doit rester allumé pour télécharger et assembler les clips.

Présentation réalisée à partir de captures de l'interface. Musique originale
synthétisée pour cette vidéo. Projet indépendant, sans affiliation avec Blink
ou Amazon.

#Blink #blink2video #OpenSource
