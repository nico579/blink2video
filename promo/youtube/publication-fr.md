# Vos caméras Blink sur PC : direct, archives et installation de blink2video

## Description

Regardez vos caméras Blink sur ordinateur et gardez un historique de leurs enregistrements avec blink2video, un logiciel gratuit et open source.

Télécharger : https://github.com/nico579/blink2video/releases/latest
Guide en français : https://github.com/nico579/blink2video/blob/main/README.fr.md
Code source et retours : https://github.com/nico579/blink2video
Discussion et retours de la communauté (en anglais) : https://www.reddit.com/r/blinkcameras/comments/1vsm4bc/blink2video_local_dashboard_for_blink_cameras_no/

POURQUOI CET OUTIL ?
Pour retrouver vos clips sur ordinateur, les récupérer avant leur effacement et les réunir en vidéos faciles à parcourir, par caméra, par jour, par semaine et par mois.

CE QUE MONTRE LA VIDÉO
• Direct dans le navigateur et enregistrement à la demande.
• Clips filtrés par caméra et par période.
• Téléchargement incrémental depuis le stockage du module : USB du Sync Module 2 ou microSD du Sync Module XR, et depuis le cloud de votre abonnement Blink.
• Date et heure incrustées, archives automatiques et clips sans intérêt mis de côté.
• Installation, première connexion et options des réglages.

INSTALLER ET DÉMARRER
1. Téléchargez l'archive adaptée à votre système et décompressez tous ses fichiers dans un dossier. Les bundles publiés incluent ffmpeg ; Python n'est pas nécessaire.
2. Sous Windows, lancez blink2video.exe. Sous Linux ou macOS, lancez ./blink2video après la préparation indiquée dans le guide.
3. Dans le navigateur, connectez-vous à Blink et saisissez le code de vérification. Au premier lancement, choisissez le dossier des données et le fuseau horaire, puis cliquez sur Appliquer. Le téléchargement commence après cette validation.

LES RÉGLAGES
L'engrenage ouvre les options : démarrage à l'ouverture de session, actualisation de la page, dossier des données, port, récupération automatique et fréquences locale/cloud, horodatage, fuseau horaire, direct WebRTC/MSE, archives quotidiennes/hebdomadaires/mensuelles, alertes par caméra et arrêt.
La suppression à la source après téléchargement est facultative, par caméra. Elle efface le clip du stockage source ; elle est distincte du bouton Écarter, qui conserve l'original à part.

CHAPITRES
00:00 Pourquoi blink2video ?
00:24 Caméras, clips, filtres et direct
00:58 Stockage du module et cloud Blink
01:08 Archives et horodatage
01:28 Télécharger et installer
01:43 Lancer et se connecter
01:58 Valider le premier démarrage
02:12 Réglages du quotidien
02:28 Réglages vidéo et archives
02:44 Alertes, suppression facultative et arrêt
03:00 Découvrir le projet

À SAVOIR
L'ordinateur doit rester allumé et blink2video en fonctionnement pour télécharger, assembler et surveiller. Les archives sont sur votre machine ; l'accès au compte et aux caméras utilise les services Blink. L'outil ne remplace pas un abonnement pour accéder à ses clips cloud.
Bundles Windows, Linux et macOS Apple Silicon. Selon la documentation, l'usage sur caméras réelles est éprouvé sous Windows ; Linux et macOS disposent de vérifications automatisées.

Présentation réalisée avec des captures animées de l'interface et une musique originale, sans voix off ni voix IA.
Projet indépendant de Nicolas Martin, sans affiliation avec Blink ou Amazon. Logiciel sous licence GNU GPL v3.0.

#Blink #blink2video #OpenSource

## Commentaire à publier

Pour démarrer avec blink2video, voici le guide pratique.

LE BUT
Voir vos caméras Blink sur ordinateur et conserver une archive des clips. Le logiciel peut récupérer les nouveaux enregistrements du stockage local du module (USB du Sync Module 2 ou microSD du XR) et du cloud de votre abonnement Blink, puis les assembler par caméra et par jour, semaine ou mois. Les clips déjà récupérés ne sont pas retéléchargés.

INSTALLATION ET LANCEMENT
• Téléchargez la dernière version : https://github.com/nico579/blink2video/releases/latest
• Choisissez votre système et décompressez l'archive complète. Les bundles incluent ffmpeg ; pas besoin d'installer Python.
• Windows 10/11 x64 : ouvrez blink2video.exe.
• Linux x64 ou macOS Apple Silicon : préparez le lancement selon le guide, puis exécutez ./blink2video dans le dossier extrait.
• Connectez-vous à votre compte Blink dans le navigateur, puis saisissez le code envoyé par Blink. Le logiciel conserve un jeton de session, jamais votre mot de passe.
• Au tout premier lancement, vérifiez le dossier des données et le fuseau horaire, puis cliquez sur Appliquer : aucun clip n'est téléchargé avant cette étape.
• Pour rouvrir l'interface, utilisez la commande blink2video open ou le menu de l'icône de notification lorsqu'elle est disponible.

QUOI FAIRE ENSUITE ?
L'onglet Direct permet de voir une caméra, d'armer la détection et d'enregistrer un direct à la demande. Les clips et les enregistrements du direct sont consultables avec des filtres par caméra et période. Les vues Journalières, Hebdomadaires et Mensuelles regroupent les vidéos assemblées.

LES OPTIONS DANS L'ENGRENAGE
• Quotidien : démarrage à l'ouverture de session, actualisation automatique, dossier des données et port du serveur.
• Récupération : activation du téléchargement et cadences distinctes pour le stockage local et le cloud.
• Vidéos : horodatage dans l'image, fuseau horaire, lecture du direct WebRTC ou MSE, archives journalières, hebdomadaires et mensuelles activables séparément.
• Contrôle : alertes et sourdine par caméra, suppression facultative à la source après téléchargement, bouton d'arrêt.
Validez vos changements avec Appliquer.

Écarter un clip le met de côté et conserve son original. La suppression automatique à la source est une autre option : elle efface les clips du module ou du cloud après leur récupération. Activez-la uniquement si c'est le comportement souhaité.

L'ordinateur doit rester allumé et le logiciel actif pour ces tâches. Les fichiers restent sur votre ordinateur ; la connexion et les caméras dépendent des services Blink. blink2video ne donne pas accès à des clips cloud sans les droits nécessaires sur votre compte.

Guide complet et commandes Linux/macOS :
https://github.com/nico579/blink2video/blob/main/README.fr.md
Code source et signalement d'un problème :
https://github.com/nico579/blink2video
Discussion et retours de la communauté (en anglais) : https://www.reddit.com/r/blinkcameras/comments/1vsm4bc/blink2video_local_dashboard_for_blink_cameras_no/

Projet indépendant, gratuit, sous GNU GPL v3.0, sans affiliation avec Blink ou Amazon. Cette présentation utilise des captures animées et de la musique originale, sans voix IA. Les chapitres de la description permettent d'aller directement à l'installation ou aux réglages.

