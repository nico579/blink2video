// Jeton anti-CSRF (voir Handler.jeton_valide côté serveur, 28.60) : posé sur
// toute requête qui modifie quelque chose. Fait une fois ici, avant tout
// autre script, pour qu'aucun fetch() plus bas n'ait à s'en soucier.
const BLINK_TOKEN = "__TOKEN__";
const _fetchNatif = window.fetch;
window.fetch = (entree, options) => {
  options = options || {};
  const headers = new Headers(options.headers || {});
  headers.set("X-Blink-Token", BLINK_TOKEN);
  options = { ...options, headers };
  return _fetchNatif(entree, options);
};

// Toute valeur reçue de Blink ou lue dans un nom de fichier est une donnée,
// jamais du balisage. Les quelques vues rendues par gabarits HTML passent
// toutes par cette fonction ; les actions utilisent des data-* et une
// délégation d'événements, aucun nom n'est réinjecté dans du JavaScript.
const h = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[char]);
const avecJeton = (url) => `${url}${url.includes("?") ? "&" : "?"}token=${encodeURIComponent(BLINK_TOKEN)}`;

// Si le serveur a redémarré hors des trois transitions suivies plus bas
// (réinstallation manuelle pendant que l'onglet restait ouvert, crash...),
// le jeton posé par cette page est périmé : toute route protégée retombe
// sur le send_error() par défaut de http.server, qui répond en HTML
// (DOCTYPE), jamais en JSON. .json() y échoue avec un SyntaxError précis,
// jamais levé par une vraie erreur JSON (send_json() reste toujours du
// JSON valide, même à 500). La seule récupération sensée est un
// rechargement complet, qui obtient une page et un jeton frais (signalé
// par cutthin sur Reddit, 2026-08-31).
async function lireJSON(reponse) {
  try {
    return await reponse.json();
  } catch (erreur) {
    if (erreur instanceof SyntaxError) location.reload();
    throw erreur;
  }
}

let data = { clips: [], cameras: [], days: [] };
let videos = { daily: [], weekly: [], monthly: [] };
const $ = (id) => document.getElementById(id);

// ── i18n ─────────────────────────────────────────────────────────────────
// Même pattern que gui/app.js de lidar2map : dico inline par locale + attribut
// data-i18n sur les nœuds statiques, t()/tf() appelés directement dans le
// texte généré en JS. Zéro dépendance. Le FR en dur dans le HTML reste le
// repli si une clé manque : pas de page cassée. Détection : navigator.language
// au premier chargement ; override manuel persisté en localStorage (page web
// ordinaire servie par serve.py, pas de webview packagée à contourner ici).
const I18N = {
  fr: {
    "view.live": "Direct", "view.clips": "Clips", "view.daily": "Journalières",
    "view.weekly": "Hebdomadaires", "view.monthly": "Mensuelles",
    "filter.allcameras": "toutes caméras",
    "btn.refresh": "↻ Actualiser", "btn.reglages": "⚙ Réglages…", "btn.reglages.title": "Réglages",
    "update.installing": "Installer {version}",
    "update.title": "Version {version} publiée. Le téléchargement, l'arrêt et la relance sont automatiques.",
    "update.updating": "Mise à jour…",
    "update.progress": "Mise à jour vers {version} : téléchargement, puis relance…",
    "passages.updated": "actualisé {heure}",
    "passages.new.one": " · {n} nouveau clip, cliquez sur Actualiser",
    "passages.new.many": " · {n} nouveaux clips, cliquez sur Actualiser",
    "auth.title": "Connexion Blink", "auth.title.2fa": "Vérification en deux étapes",
    "auth.hint": "Le mot de passe sert uniquement à ouvrir la session ; seuls les jetons sont enregistrés, jamais le mot de passe.",
    "auth.hint.2fa": "Blink vient d'envoyer un code. Saisissez-le pour terminer la connexion.",
    "auth.email": "Adresse e-mail", "auth.password": "Mot de passe",
    "auth.show": "Afficher", "auth.hide": "Masquer",
    "auth.show.aria": "Afficher le mot de passe", "auth.hide.aria": "Masquer le mot de passe",
    "auth.code": "Code reçu par SMS ou e-mail",
    "auth.cancel": "Annuler", "auth.ok": "Se connecter", "auth.validate": "Valider",
    "auth.connecting": "Connexion en cours…", "auth.failed": "Échec de la connexion.",
    "reglages.title": "Réglages",
    "reglages.initial.hint": "Vérifiez notamment le dossier des données et le fuseau horaire. Aucun clip ne sera téléchargé avant que vous ayez appliqué ces réglages.",
    "reglages.autostart": "Démarrage de la surveillance à l'ouverture de session",
    "reglages.autostart.title": "Démarre le serveur web et le traitement des clips à l'ouverture de session, en arrière-plan — n'ouvre pas cette page toute seule",
    "reglages.auto": "Actualisation automatique de la page",
    "reglages.auto.title": "Recharger la liste dès que des clips arrivent",
    "reglages.showOut": "Voir les clips écartés",
    "reglages.serveur": "Port du serveur",
    "reglages.storageDir": "Dossier des données",
    "reglages.storageDir.placeholder": "C:/chemin/vers/le/dossier",
    "reglages.storageDir.hint": "Ne déplace pas les clips ni la session Blink déjà présents à l'ancien emplacement : à faire vous-même si vous changez ce chemin. Vide = emplacement par défaut, celui de l'exécutable.",
    "reglages.storageDir.browse": "Parcourir…",
    "reglages.storageDir.browse.unavailable": "Sélecteur de dossier indisponible sur cette machine : saisissez le chemin directement.",
    "reglages.cadence": "Cadence de lecture des caméras",
    "reglages.usb": "Stockage local (minutes)", "reglages.cloud": "Cloud (minutes)",
    "reglages.video": "Vidéo", "reglages.timestamp": "Incruster la date et l'heure dans l'image",
    "reglages.timezone": "Fuseau horaire",
    "reglages.archivage": "Création des vidéos temporelles par caméra",
    "reglages.downloadAuto": "Télécharger les clips automatiquement",
    "reglages.downloadAuto.hint": "Décochée, aucun clip n'est plus récupéré ni stocké : utile pour ne garder que le direct. Les cadences ci-dessous n'ont alors plus d'effet.",
    "reglages.mergeJour": "Quotidienne",
    "reglages.mergeSemaine": "Hebdomadaire", "reglages.mergeMois": "Mensuelle",
    "reglages.archivage.hint": "Hebdomadaire et mensuelle sont assemblées à partir de la quotidienne : décocher « Quotidienne » désactive aussi les deux autres.",
    "reglages.alertes": "Mise en sourdine des alertes",
    "reglages.suppressionAuto": "Suppression automatique après téléchargement",
    "reglages.hint": "Les réglages ne prennent effet qu'au redémarrage : « Appliquer » enregistre et redémarre. Changer le port redirige cette page vers la nouvelle adresse.",
    "reglages.apply": "Appliquer", "reglages.restarting": "Redémarrage…",
    "reglages.restarting.settings": "Redémarrage avec les nouveaux réglages…",
    "reglages.portchange": "Port changé : redirection vers {url} dès l'arrêt confirmé…",
    "reglages.stop": "Arrêter la surveillance des caméras", "reglages.close": "Fermer",
    "reglages.error.cadence": "Les cadences doivent valoir au moins 1 minute.",
    "reglages.error.port": "Le port doit être compris entre 1 et 65535.",
    "reglages.error.timezone": "Le fuseau horaire ne peut pas être vide.",
    "stop.stopping": "Arrêt…",
    "stop.stopped": "Arrêt demandé. Cette page va devenir indisponible.",
    "stop.failed": "L'arrêt n'a pas abouti ; blink2video répond toujours.",
    "reglages.restartFailed": "Le redémarrage n'a pas commencé. Vérifiez les journaux puis réessayez.",
    "sourdine.loading": "Chargement…", "sourdine.unavailable": "Liste des caméras indisponible.",
    "sourdine.none": "Aucune caméra connue pour l'instant.",
    "suppressionAuto.loading": "Chargement…",
    "suppressionAuto.unavailable": "Liste des caméras indisponible.",
    "suppressionAuto.none": "Aucune caméra connue pour l'instant.",
    "suppressionAuto.legacy": "Des anciens réglages ambigus ont été désactivés. Réactivez les caméras voulues.",
    "suppressionAuto.hint": "Une fois un clip téléchargé avec succès, il est supprimé de sa source (stockage local USB/microSD ou cloud de l'abonnement selon la caméra).",
    "phase.inventory_clips": "Inventaire des clips à télécharger",
    "phase.download_clips": "Téléchargement des clips",
    "phase.prepare_clips": "Préparation des clips",
    "phase.assemble_videos": "Assemblage des vidéos",
    "phase.update_download": "Téléchargement de la mise à jour ({mo} Mo)",
    "phase.update_install": "Installation de la mise à jour",
    "phase.step_download": "Téléchargement", "phase.step_merge": "Fusion",
    "phase.cloud_section": "Cloud de l'abonnement",
    "phase.usb_section": "Stockage local : {hub}",
    "live.querying": "Interrogation du système Blink…",
    "live.count": "{n} caméra(s) · {m} armée(s)",
    "system.armed": "Système armé", "system.disarmed": "Système désarmé",
    "camera.offline": "HORS LIGNE", "camera.noeffect": "sans effet, système désarmé",
    "camera.detection.on": "Détection active", "camera.detection.off": "Détection coupée",
    "camera.wake": "Réveiller", "camera.waking": "Réveil…",
    "camera.wake.title": "Réveille la caméra maintenant (prend une photo). Consomme un peu de batterie, jusqu'à 2 minutes.",
    "camera.battery": "batterie {v}", "camera.wifi": "Wi-Fi {v} dBm",
    "camera.lfr": "liaison module {v}", "camera.measured.at": "relevé à {v}",
    "camera.measured.on": "relevé du {v}", "camera.firmware": "micrologiciel {v}",
    "camera.noclips": "aucun clip récupéré", "camera.clipssource": "clips : {v}",
    "camera.none": "—",
    "watch.live": "Voir en direct", "watch.retry": "Réessayer", "watch.stop": "Arrêter",
    "watch.waking": "Réveil de la caméra…", "watch.waking.seconds": "Réveil de la caméra… {s} s",
    "watch.waking.slow": "Réveil de la caméra… {s} s (une caméra sur batterie est plus lente)",
    "watch.waking.mse": "Réveil de la caméra… (MSE)", "watch.reconnecting": "Reconnexion…",
    "live.fullscreen.title": "Agrandir en plein écran",
    "live.fullscreen.title.exit": "Quitter le plein écran",
    "watch.noimage": "Aucune image reçue. La caméra n'a pas répondu.",
    "watch.refused": "Le flux a été refusé par le serveur.",
    "watch.refused.code": "Le flux a été refusé par le serveur ({code}).",
    "watch.refused.retry": "Flux refusé. Un direct précédent finit peut-être de se fermer : réessayez dans quelques secondes.",
    "watch.codec.unsupported": "Codec non supporté par ce navigateur : {codec}",
    "command.sending": "Envoi de la commande…",
    "clips.none.filtered": "Aucun clip ne correspond à ce filtre.",
    "clips.none.ever": "Aucun clip récupéré pour l'instant.<br>Le téléchargement tourne déjà en arrière-plan (clé USB toutes les 10 min, cloud toutes les minutes) : les clips apparaîtront ici sans rien faire. Vérifiez qu'une clé USB est branchée sur le module : sans elle, les enregistrements ne vont que dans le cloud de l'abonnement Blink, que cet outil ne lit pas.",
    "clips.window": "{m}/{total} clips",
    "range.title": "Période",
    "range.today": "Aujourd'hui (24 h)", "range.week": "Cette semaine (7 j)",
    "range.month": "Ce mois-ci", "range.2months": "2 derniers mois",
    "range.all": "Tout l'historique", "range.custom": "Période personnalisée",
    "range.custom.depuis": "depuis {v}", "range.custom.jusqua": "jusqu'au {v}",
    "range.custom.hint": "Ou une plage précise, à l'heure près :",
    "filtre.button": "🔍 Filtre", "filtre.button.title": "Filtrer",
    "filtre.title": "Filtre", "filtre.camera": "Caméra",
    "range.from": "Du", "range.to": "au", "range.apply": "Filtrer",
    "videos.count": "{n} vidéo(s) · {duree} au total",
    "videos.none": "Aucune vidéo assemblée. Lancez une actualisation.",
    "videos.download": "Télécharger",
    "clip.resume": "Reprendre", "clip.discard": "Écarter",
    "clip.discard.title": "Retirer ce clip des vidéos assemblées (quotidienne, hebdomadaire, mensuelle). La copie téléchargée reste sur le disque.",
    "clip.resume.title": "Réinclure ce clip dans les prochains assemblages.",
    "clip.deleteSource": "Supprimer",
    "clip.deleteSource.pending": "Suppression…",
    "clip.deleteSource.title": "Supprimer ce clip de sa source (stockage local ou cloud de l'abonnement). La copie déjà téléchargée ici n'est pas touchée. Peut prendre jusqu'à une minute pour le stockage local.",
    "selection.apply": "✓ Appliquer ({n})",
    "selection.confirm.suppression": "{n} clip(s) vont être supprimés de leur source (stockage local USB/microSD ou cloud de l'abonnement). Les copies déjà téléchargées ne sont pas touchées. Continuer ?",
    "selection.partial": "{n} suppression(s) ont échoué ou n'ont rien trouvé à supprimer (déjà retiré ailleurs). Le reste de la sélection a été appliqué.",
    "refresh.starting": "Démarrage…", "refresh.errors": "Terminé avec des erreurs",
    "refresh.disconnected": "\\nConnexion interrompue.\\n",
  },
  en: {
    "view.live": "Live", "view.clips": "Clips", "view.daily": "Daily",
    "view.weekly": "Weekly", "view.monthly": "Monthly",
    "filter.allcameras": "all cameras",
    "btn.refresh": "↻ Refresh", "btn.reglages": "⚙ Settings…", "btn.reglages.title": "Settings",
    "update.installing": "Install {version}",
    "update.title": "Version {version} published. Download, stop and restart are automatic.",
    "update.updating": "Updating…",
    "update.progress": "Updating to {version}: downloading, then restarting…",
    "passages.updated": "updated {heure}",
    "passages.new.one": " · {n} new clip, click Refresh",
    "passages.new.many": " · {n} new clips, click Refresh",
    "auth.title": "Blink login", "auth.title.2fa": "Two-step verification",
    "auth.hint": "The password is only used to open the session; only the tokens are stored, never the password.",
    "auth.hint.2fa": "Blink just sent a code. Enter it to finish logging in.",
    "auth.email": "Email address", "auth.password": "Password",
    "auth.show": "Show", "auth.hide": "Hide",
    "auth.show.aria": "Show password", "auth.hide.aria": "Hide password",
    "auth.code": "Code received by SMS or email",
    "auth.cancel": "Cancel", "auth.ok": "Log in", "auth.validate": "Confirm",
    "auth.connecting": "Signing in…", "auth.failed": "Login failed.",
    "reglages.title": "Settings",
    "reglages.initial.hint": "Check the data folder and time zone in particular. No clip will be downloaded until you apply these settings.",
    "reglages.autostart": "Start monitoring at login",
    "reglages.autostart.title": "Starts the web server and clip processing at login, in the background — does not open this page by itself",
    "reglages.auto": "Automatic page refresh",
    "reglages.auto.title": "Reload the list as soon as clips arrive",
    "reglages.showOut": "Show discarded clips",
    "reglages.serveur": "Server port",
    "reglages.storageDir": "Data folder",
    "reglages.storageDir.placeholder": "C:/path/to/the/folder",
    "reglages.storageDir.hint": "Does not move clips or the Blink session already present at the old location: do it yourself if you change this path. Empty = default location, next to the executable.",
    "reglages.storageDir.browse": "Browse…",
    "reglages.storageDir.browse.unavailable": "Folder picker unavailable on this machine: type the path directly.",
    "reglages.cadence": "Camera polling interval",
    "reglages.usb": "Local storage (minutes)", "reglages.cloud": "Cloud (minutes)",
    "reglages.video": "Video", "reglages.timestamp": "Burn the date and time into the image",
    "reglages.timezone": "Time zone",
    "reglages.archivage": "Per-camera time-based video creation",
    "reglages.downloadAuto": "Download clips automatically",
    "reglages.downloadAuto.hint": "Unchecked, no clip is fetched or stored anymore: useful to keep only the live view. The cadences below then have no effect.",
    "reglages.mergeJour": "Daily",
    "reglages.mergeSemaine": "Weekly", "reglages.mergeMois": "Monthly",
    "reglages.archivage.hint": "Weekly and Monthly are assembled from the Daily: unchecking \u201cDaily\u201d also disables the other two.",
    "reglages.alertes": "Mute alerts",
    "reglages.suppressionAuto": "Automatic deletion after download",
    "reglages.hint": "Settings only take effect on restart: \u201cApply\u201d saves and restarts. Changing the port redirects this page to the new address.",
    "reglages.apply": "Apply", "reglages.restarting": "Restarting…",
    "reglages.restarting.settings": "Restarting with the new settings…",
    "reglages.portchange": "Port changed: redirecting to {url} once the shutdown is confirmed…",
    "reglages.stop": "Stop camera monitoring", "reglages.close": "Close",
    "reglages.error.cadence": "Intervals must be at least 1 minute.",
    "reglages.error.port": "The port must be between 1 and 65535.",
    "reglages.error.timezone": "The time zone cannot be empty.",
    "stop.stopping": "Stopping…",
    "stop.stopped": "Shutdown requested. This page will become unavailable.",
    "stop.failed": "Shutdown did not complete; blink2video is still responding.",
    "reglages.restartFailed": "Restart did not begin. Check the logs and try again.",
    "sourdine.loading": "Loading…", "sourdine.unavailable": "Camera list unavailable.",
    "sourdine.none": "No known camera yet.",
    "suppressionAuto.loading": "Loading…",
    "suppressionAuto.unavailable": "Camera list unavailable.",
    "suppressionAuto.none": "No known camera yet.",
    "suppressionAuto.legacy": "Ambiguous legacy settings were disabled. Re-enable the intended cameras.",
    "suppressionAuto.hint": "Once a clip is successfully downloaded, it is deleted from its source (local USB/microSD storage or subscription cloud, depending on the camera).",
    "phase.inventory_clips": "Finding clips to download",
    "phase.download_clips": "Downloading clips",
    "phase.prepare_clips": "Preparing clips",
    "phase.assemble_videos": "Assembling videos",
    "phase.update_download": "Downloading the update ({mo} MB)",
    "phase.update_install": "Installing the update",
    "phase.step_download": "Downloading", "phase.step_merge": "Merging",
    "phase.cloud_section": "Subscription cloud",
    "phase.usb_section": "Local storage: {hub}",
    "live.querying": "Querying the Blink system…",
    "live.count": "{n} camera(s) · {m} armed",
    "system.armed": "System armed", "system.disarmed": "System disarmed",
    "camera.offline": "OFFLINE", "camera.noeffect": "no effect, system disarmed",
    "camera.detection.on": "Detection on", "camera.detection.off": "Detection off",
    "camera.wake": "Wake", "camera.waking": "Waking…",
    "camera.wake.title": "Wakes the camera now (takes a photo). Uses a bit of battery, up to 2 minutes.",
    "camera.battery": "battery {v}", "camera.wifi": "Wi-Fi {v} dBm",
    "camera.lfr": "module link {v}", "camera.measured.at": "measured at {v}",
    "camera.measured.on": "measured on {v}", "camera.firmware": "firmware {v}",
    "camera.noclips": "no clip retrieved", "camera.clipssource": "clips: {v}",
    "camera.none": "—",
    "watch.live": "View live", "watch.retry": "Retry", "watch.stop": "Stop",
    "watch.waking": "Waking the camera…", "watch.waking.seconds": "Waking the camera… {s} s",
    "watch.waking.slow": "Waking the camera… {s} s (a battery camera is slower)",
    "watch.waking.mse": "Waking the camera… (MSE)", "watch.reconnecting": "Reconnecting…",
    "live.fullscreen.title": "Expand fullscreen",
    "live.fullscreen.title.exit": "Exit fullscreen",
    "watch.noimage": "No image received. The camera did not respond.",
    "watch.refused": "The stream was refused by the server.",
    "watch.refused.code": "The stream was refused by the server ({code}).",
    "watch.refused.retry": "Stream refused. A previous live view may still be closing: try again in a few seconds.",
    "watch.codec.unsupported": "Codec not supported by this browser: {codec}",
    "command.sending": "Sending command…",
    "clips.none.filtered": "No clip matches this filter.",
    "clips.none.ever": "No clip retrieved yet.<br>Download is already running in the background (USB every 10 min, cloud every minute): clips will appear here on their own. Check that a USB drive is plugged into the module: without it, recordings only go to the Blink subscription cloud, which this tool does not read.",
    "clips.window": "{m}/{total} clips",
    "range.title": "Period",
    "range.today": "Today (24h)", "range.week": "This week (7d)",
    "range.month": "This month", "range.2months": "Last 2 months",
    "range.all": "All history", "range.custom": "Custom period",
    "range.custom.depuis": "from {v}", "range.custom.jusqua": "until {v}",
    "range.custom.hint": "Or a precise range, down to the hour:",
    "filtre.button": "🔍 Filter", "filtre.button.title": "Filter",
    "filtre.title": "Filter", "filtre.camera": "Camera",
    "range.from": "From", "range.to": "to", "range.apply": "Filter",
    "videos.count": "{n} video(s) · {duree} total",
    "videos.none": "No assembled video. Run a refresh.",
    "videos.download": "Download",
    "clip.resume": "Resume", "clip.discard": "Discard",
    "clip.discard.title": "Remove this clip from the assembled videos (daily, weekly, monthly). The downloaded copy stays on disk.",
    "clip.resume.title": "Include this clip in future assemblies again.",
    "clip.deleteSource": "Delete",
    "clip.deleteSource.pending": "Deleting…",
    "clip.deleteSource.title": "Delete this clip from its source (local storage or subscription cloud). The copy already downloaded here is not affected. Can take up to a minute for local storage.",
    "selection.apply": "✓ Apply ({n})",
    "selection.confirm.suppression": "{n} clip(s) will be deleted from their source (local USB/microSD storage or subscription cloud). Copies already downloaded here are not affected. Continue?",
    "selection.partial": "{n} deletion(s) failed or found nothing to delete (already removed elsewhere). The rest of the selection was applied.",
    "refresh.starting": "Starting…", "refresh.errors": "Finished with errors",
    "refresh.disconnected": "\\nConnection lost.\\n",
  },
};
let _lang = "fr";
function t(k) { return (I18N[_lang] && I18N[_lang][k]) || I18N.fr[k] || k; }
function tf(k, v) {
  let s = t(k);
  for (const p in (v || {})) s = s.split("{" + p + "}").join(v[p]);
  return s;
}
function detectLang() {
  return (navigator.language || "en").toLowerCase().startsWith("fr") ? "fr" : "en";
}
function applyI18n() {
  document.documentElement.lang = _lang;
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    const v = t(el.dataset.i18n); if (v) el.textContent = v;
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
    const v = t(el.dataset.i18nPlaceholder); if (v) el.placeholder = v;
  });
  document.querySelectorAll("[data-i18n-title]").forEach((el) => {
    const v = t(el.dataset.i18nTitle); if (v) el.title = v;
  });
  document.querySelectorAll("[data-lang-btn]").forEach((b) =>
    b.classList.toggle("active", b.dataset.langBtn === _lang));
}
function setLang(code, persist) {
  _lang = code === "en" ? "en" : "fr";
  applyI18n();
  // Le bouton « afficher/masquer » le mot de passe suit son propre état
  // (masqué ou non), qu'applyI18n ne connaît pas : ré-appliqué ici plutôt
  // que par data-i18n, qui écraserait « Masquer » par « Afficher » si le
  // mot de passe était déjà visible au moment du changement de langue.
  const pass = $("pass");
  if (pass) {
    const masque = pass.type === "password";
    $("passToggle").textContent = t(masque ? "auth.show" : "auth.hide");
    $("passToggle").setAttribute("aria-label", t(masque ? "auth.show.aria" : "auth.hide.aria"));
  }
  // Rendu par JS plutôt que par data-i18n : reconstruire pour que la langue
  // s'applique immédiatement, sans attendre le prochain événement qui
  // déclencherait normalement ce rendu. fill() gère un tableau vide sans
  // problème (l'option « tout » reste posée), donc pas de garde ici.
  if (typeof fill === "function") {
    fill($("camera"), data.cameras || [], t("filter.allcameras"),
         (nom) => [nom, (data.models || {})[nom]].filter(Boolean).join(" · "));
  }
  if (typeof render === "function" && data.clips) render();
  if (typeof renderLive === "function" && system) renderLive();
  // Un direct actif gèle la grille (voir renderLive()) : le bouton plein
  // écran déjà posé survit donc au changement de langue sans se refaire,
  // et doit être retraduit ici plutôt que de rester dans l'ancienne langue.
  if (typeof syncExpandButtons === "function") syncExpandButtons();
  // #sourdineListe porte data-i18n="sourdine.loading" en repli HTML :
  // applyI18n() vient d'écraser ses cases à cocher réelles par ce texte de
  // chargement si le panneau est ouvert pendant la bascule de langue.
  // Reconstruire immédiatement plutôt que de laisser la liste figée ainsi
  // jusqu'à la prochaine ouverture du panneau.
  if (typeof chargerSourdine === "function" && $("reglages")?.open) chargerSourdine();
  if (typeof chargerSuppressionAuto === "function" && $("reglages")?.open) chargerSuppressionAuto();
  if (persist) localStorage.setItem("lang", _lang);
  // Envoyé à chaque appel, pas seulement un choix explicite (persist) :
  // le menu du systray (tray.py) lit cette valeur pour s'afficher dans la
  // même langue que la page, y compris quand elle vient de detectLang()
  // et n'a jamais été choisie à la main. Best-effort, jamais bloquant : une
  // page ouverte hors-ligne ou un onglet d'arrière-plan ne doit pas faire
  // échouer l'affichage.
  fetch("/api/lang", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ lang: _lang }) }).catch(() => {});
}

function fill(select, values, all, label) {
  const kept = select.value;
  select.replaceChildren();
  const optionToutes = document.createElement("option");
  optionToutes.value = "";
  optionToutes.textContent = all;
  select.append(optionToutes);
  for (const value of values) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label ? label(value) : value;
    select.append(option);
  }
  if (values.includes(kept)) select.value = kept;
}

function visible() {
  return data.clips.filter((c) =>
    (!$("camera").value || c.camera === $("camera").value) &&
    ($("showOut").checked || !c.excluded));
}

function duration(seconds) {
  const s = Math.round(seconds);
  const parts = [Math.floor(s / 3600), Math.floor(s / 60) % 60, s % 60];
  return (parts[0] ? parts : parts.slice(1))
    .map((v, i) => (i ? String(v).padStart(2, "0") : v)).join(":");
}

function render() {
  heuresDePassage();
  const kind = $("view").value;
  const clips = kind === "clips";
  $("outLabel").hidden = !clips;
  // Même périmètre que la caméra avant la refonte : Clips, Journalières,
  // Hebdomadaires, Mensuelles s'y filtrent toutes, seul Direct n'en a pas
  // l'usage. La période, elle, ne vaut que pour Clips (/api/clips) : le
  // reste ne la lit jamais.
  $("filtreButton").hidden = kind === "live";
  $("periodeSection").hidden = !clips;
  $("filtreResume").textContent = clips ? resumeFiltre() : "";
  // Le décompte n'a de sens que pour Clips ; renderClips() le repose à
  // chaque rendu, mais quitter cette vue doit l'effacer, pas le laisser
  // périmé derrière une autre vue.
  if (!clips) $("filtreCompte").textContent = "";
  if (kind === "live") return renderLive();
  return clips ? renderClips() : renderVideos(kind);
}

// --- direct et armement ----------------------------------------------------
let system = null;

async function loadSystem(force) {
  if (system && !force) return renderLive();
  // La vue peut avoir changé entre le déclenchement de cet appel et sa
  // résolution (bascule rapide vers Clips, ou déclenchement précoce au
  // chargement avant que la vue par défaut ne soit posée) : ne toucher au
  // DOM que si Direct est encore affiché, sinon la réponse tardive
  // écraserait une liste de clips déjà à l'écran sans que le menu déroulant
  // ne le laisse deviner.
  if ($("view").value === "live") {
    $("list").innerHTML = `<p class="empty">${t("live.querying")}</p>`;
    $("count").textContent = "";
  }
  try {
    system = await lireJSON(await fetch("/api/system"));
  } catch (error) {
    system = { error: String(error) };
  }
  if ($("view").value === "live") renderLive();
}

// Un calcul lancé par les boucles de fond, hors de cette page : le
// téléchargement et l'assemblage publient leur avancement dans un fichier, seul
// moyen pour la page d'apprendre que la machine travaille. Tant qu'il tourne,
// le bouton reste inactif : un second calcul attendrait le même verrou, sans
// rien avancer.
let travailEnCours = false;
let travailVisible = false;
let actualisationLocale = false;

// Le serveur ne connaît jamais la langue affichée (choix propre à chaque
// onglet, en localStorage) : un libellé de phase arrive donc toujours en
// français, accompagné d'une clé stable quand une traduction existe. Clé
// absente ou inconnue de ce dictionnaire : le texte reçu reste affiché tel
// quel plutôt qu'une chaîne vide, qui masquerait un travail réellement en
// cours (ex. bug vécu en vrai : « Téléchargement des clips » figé en
// français quelle que soit la langue choisie).
function libellePhase(cle, texteBrut, valeurs) {
  if (!cle || !((I18N[_lang] && I18N[_lang][cle]) || I18N.fr[cle])) return texteBrut;
  return valeurs ? tf(cle, valeurs) : t(cle);
}

function montrerTravail(travail) {
  if (actualisationLocale) return;    // notre propre barre parle déjà
  const visible = !!(travail && travail.quoi);
  if (!visible) {
    if (travailVisible) {
      $("work").classList.remove("on");
      rechargerEnArrierePlan();
    }
    travailEnCours = false;
    travailVisible = false;
    $("refresh").disabled = false;
    return;
  }
  const termine = !!travail.termine;
  const actif = travail.actif === undefined ? !termine : !!travail.actif;
  const etaitActif = travailEnCours;
  travailEnCours = actif;
  travailVisible = true;
  $("refresh").disabled = actif;
  $("work").classList.add("on");
  const total = travail.total || 0;
  const fait = travail.fait || 0;
  const quoi = travail.cle === "phase.update_download"
    ? libellePhase(travail.cle, travail.quoi, { mo: Math.round(total) })
    : libellePhase(travail.cle, travail.quoi);
  if (total) {
    $("bar").max = total;
    $("bar").value = fait;
    const courant = fait >= total ? total : fait + 1;
    $("phase").textContent =
      `${quoi} ${courant}/${total} (${Math.round((fait / total) * 100)} %)`;
  } else {
    $("bar").removeAttribute("value");
    $("phase").textContent = quoi;
  }
  // Le N/N retenu dix secondes est visible, mais n'est plus un verrou : un
  // nouveau clic reste possible. Si la page avait vu le travail actif, sa fin
  // déclenche exactement le même rafraîchissement qu'une disparition directe.
  if (termine && etaitActif && !actif) rechargerEnArrierePlan();
}

// Une version publiée plus récente que celle qui tourne : le bouton apparaît,
// et il fait tout, du téléchargement à la relance. Pendant l'opération le
// serveur s'arrête et revient : la page attend son retour, puis se recharge.
function montrerMaj(neuve) {
  const bouton = $("update");
  bouton.hidden = !(neuve && neuve.version);
  if (bouton.hidden || bouton.dataset.encours) return;
  bouton.textContent = tf("update.installing", { version: neuve.version });
  bouton.title = tf("update.title", { version: neuve.version });
}

$("update").onclick = async () => {
  const bouton = $("update");
  bouton.dataset.encours = "1";
  bouton.disabled = true;
  bouton.textContent = t("update.updating");
  const reponse = await fetch("/api/update", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: "{}" });
  const resultat = await lireJSON(reponse);
  if (resultat.error) {
    alert(resultat.error);
    bouton.disabled = false;
    delete bouton.dataset.encours;
    return;
  }
  $("phase").textContent = tf("update.progress", { version: resultat.version });
  $("bar").removeAttribute("value");
  $("work").classList.add("on");
  $("refresh").disabled = true;
  // Le serveur va disparaître puis revenir sous sa nouvelle version. On teste
  // sa présence, et c'est son retour qui sert de fin de course.
  let parti = false;
  const attente = setInterval(async () => {
    try {
      await fetch("/api/status", { cache: "no-store" });
      if (parti) location.reload();
    } catch (erreur) {
      parti = true;      // il s'est arrêté : la relance suit
    }
  }, 2000);
  setTimeout(() => clearInterval(attente), 900000);
};

let dernierRechargementAuto = 0;

// Rechargement déclenché par l'arrière-plan (nouveau clip détecté, ou fin
// d'un calcul) plutôt que par un clic explicite : jamais plus d'une fois
// toutes les 60 secondes, le rythme normal de veille de veiller(). Pendant
// un gros lot en cours de traitement, chaque nouveau clip détecté ou chaque
// bascule de travailEnCours pouvait sinon redéclencher un rechargement
// complet de la grille (toutes les vignettes vidéo détruites et
// reconstruites), perçu comme un clignotement (constaté en réel, 2026-08-27).
function rechargerEnArrierePlan() {
  const maintenant = Date.now();
  if (maintenant - dernierRechargementAuto < 60000) return;
  dernierRechargementAuto = maintenant;
  load();
}

async function heuresDePassage() {
  let etat = {};
  try {
    etat = await lireJSON(await fetch("/api/passages"));
  } catch (erreur) { return; }
  montrerMaj(etat.maj);
  const vus = etat.passages || {};
  // Des clips sont arrivés depuis que la page a été chargée : on le dit, et
  // c'est à vous de cliquer sur Actualiser. La liste ne se réorganise pas sous
  // les yeux de qui est en train de la lire.
  //
  // Face à total_known (le vrai total du registre, jamais borné par le
  // filtre actif), jamais data.clips.length : celui-ci ne compte que ce que
  // le filtre courant affiche (une caméra, une période étroite…), pas tout
  // ce qui est connu. Comparer le total réel à un sous-ensemble filtré
  // annonçait des centaines de « nouveaux » clips qui n'avaient rien de
  // nouveau (constaté en réel, 2026-08-27).
  const arrives = (data && data.total_known !== undefined)
    ? Math.max(0, (etat.clips || 0) - data.total_known) : 0;
  // Une seule heure, la plus récente des trois. Le détail du verbe le plus en
  // retard alourdissait la ligne pour un cas rare.
  const dates = ["watch", "download", "merge"].filter((cle) => vus[cle]);
  if (!dates.length) return;

  const instant = (cle) => new Date(vus[cle]).getTime();
  const plusRecent = dates.reduce((a, b) => (instant(a) > instant(b) ? a : b));
  // Choix mémorisé d'un affichage à l'autre : une préférence qu'il faudrait
  // recocher à chaque ouverture n'en serait pas une. Pendant un calcul, on ne
  // recharge pas : la liste changerait sous les yeux à chaque vidéo assemblée.
  if (arrives && $("auto").checked && !travailEnCours) {
    rechargerEnArrierePlan();
    return;
  }
  const nouveaux = arrives
    ? tf(arrives > 1 ? "passages.new.many" : "passages.new.one", { n: arrives })
    : "";
  $("passages").textContent =
    tf("passages.updated", { heure: vus[plusRecent].slice(11, 16) }) + nouveaux;
}

async function etatDuTravail() {
  try {
    const etat = await lireJSON(await fetch("/api/travail", { cache: "no-store" }));
    montrerTravail(etat.travail);
  } catch (erreur) { /* le prochain passage réessaiera */ }
}

function renderLive() {
  // Reconstruire la grille remplace tout son HTML, direct en cours compris :
  // la balise <video> et son AbortController survivraient, orphelins, sous
  // un DOM tout neuf qui ne les référence plus (vu en vrai : un clip qui
  // arrive en tâche de fond suffit à déclencher ce rafraîchissement pendant
  // qu'un direct tourne, qui semble alors s'arrêter sans jamais reprendre).
  // Un direct actif gèle donc la grille jusqu'à ce qu'il s'arrête.
  if (Object.keys(MSE_ABORT).length) return;
  if (!system) return loadSystem(false);
  if (system.error) {
    $("list").innerHTML = `<p class="empty">${h(system.error)}</p>`;
    return;
  }
  const cameras = system.systems.reduce((n, s) => n + s.cameras.length, 0);
  const armed = system.systems.reduce(
    (n, s) => n + s.cameras.filter((c) => c.armed).length, 0);
  $("count").textContent = tf("live.count", { n: cameras, m: armed });

  $("list").innerHTML = system.systems.map((s) => `
    <h2>
      ${h(s.name)}
      <span class="sub tiny">${h([s.module,
        s.module_firmware ? tf("camera.firmware", { v: s.module_firmware }) : null,
        s.module_serial].filter(Boolean).join(" · "))}</span>
      <button class="act ${s.armed ? "in" : "out"}"
              data-action="arm" data-scope="system" data-name="${h(s.key)}"
              data-armed="${!s.armed}">
        ${h(s.armed ? t("system.armed") : t("system.disarmed"))}
      </button>
    </h2>
    <div class="grid wide">${s.cameras.map((c) => cameraCard(c, s.armed)).join("")}</div>
  `).join("");
}

function cameraCard(c, systemArmed) {
  // Une mesure vieille de plus d'une heure est datée, et une caméra hors
  // ligne est signalée comme telle : sa dernière température connue peut
  // remonter à plusieurs semaines.
  const vieille = c.age_seconds !== null && c.age_seconds > 3600;
  const num = (v) => v !== null && v !== undefined;
  const releve = [
    c.battery ? tf("camera.battery", { v: c.battery }) + (num(c.battery_signal) ? ` (${c.battery_signal})` : "") : null,
    num(c.temperature) ? `${c.temperature.toFixed(1).replace(".", ",")} °C` : null,
    num(c.wifi) ? tf("camera.wifi", { v: c.wifi }) : null,
    num(c.lfr) ? tf("camera.lfr", { v: c.lfr }) : null,
  ].filter(Boolean).join(" · ");
  const date = c.measured_at
    ? (c.measured_at.includes("à") ? tf("camera.measured.on", { v: c.measured_at })
                                   : tf("camera.measured.at", { v: c.measured_at }))
    : null;
  const details = [
    c.offline ? t("camera.offline") : null,
    releve || null,
    date,
    c.armed && !systemArmed ? t("camera.noeffect") : null,
  ].filter(Boolean).join(" · ");
  return `<div class="card ${c.offline ? "out" : ""}">
    <div class="live" id="live-${cssId(c.key)}">${repos(c.key, t("watch.live"))}</div>
    <div class="meta">
      <div>
        <div class="time">${h(c.name)}</div>
        <div class="sub">${h(details || t("camera.none"))}</div>
        <div class="sub tiny">${h([c.model,
          c.firmware ? tf("camera.firmware", { v: c.firmware }) : null, c.serial,
          c.clips_source ? tf("camera.clipssource", { v: c.clips_source }) : t("camera.noclips"),
        ].filter(Boolean).join(" · "))}</div>
      </div>
      <button class="act ${c.armed ? "in" : "out"}"
              data-action="arm" data-scope="camera" data-name="${h(c.key)}"
              data-armed="${!c.armed}">
        ${h(c.armed ? t("camera.detection.on") : t("camera.detection.off"))}
      </button>
      <button class="act grouped" title="${h(t("camera.wake.title"))}"
              data-action="wake" data-name="${h(c.key)}">${h(t("camera.wake"))}</button>
    </div>
  </div>`;
}

const cssId = (name) => name.replace(/[^\w-]/g, "_");

// Un direct qui échoue doit rendre son bouton d'origine : laisser « Arrêter »
// laisserait croire qu'un flux tourne, et il n'y aurait plus aucun moyen de
// relancer. Retirer la balise <img> ferme au passage la connexion restée
// ouverte côté serveur.
function failWatch(name, message) {
  const box = $("live-" + cssId(name));
  box.innerHTML = repos(name, t("watch.retry")) + `<p class="hint overlay">${h(message)}</p>`;
}

// Ni <img> (MJPEG) ni la balise <video> du direct MSE ne portent l'attribut
// controls (un scrubber n'aurait aucun sens sur un flux sans fin) : le
// plein écran ne peut donc pas venir gratuitement du navigateur comme pour
// les clips enregistrés. Bouton dédié plutôt qu'un clic sur toute la case :
// essayé d'abord, jugé peu explicite à l'usage.
function toggleFullscreen(name) {
  if (document.fullscreenElement || document.webkitFullscreenElement) {
    (document.exitFullscreen || document.webkitExitFullscreen).call(document);
    return;
  }
  const box = $("live-" + cssId(name));
  (box.requestFullscreen || box.webkitRequestFullscreen).call(box);
}

function stopWatch(name) {
  // La case peut avoir disparu sous nos pieds (actualisation de la vue
  // pendant le direct) : la remise au repos est cosmétique, mais couper les
  // flux ci-dessous ne doit jamais en dépendre.
  const box = $("live-" + cssId(name));
  if (box) box.innerHTML = repos(name, t("watch.live"));
  const controller = MSE_ABORT[name];
  if (controller) { controller.abort(); delete MSE_ABORT[name]; }
}

// --- MSE/fMP4 : remux sans réencodage, <video> décodé par le navigateur ---
// Contrairement à <img>, un fetch() ne s'arrête pas tout seul quand on jette
// la balise : il faut son propre AbortController, gardé ici par caméra pour
// que stopWatch() puisse le couper.
const MSE_ABORT = {};
// Blink referme parfois la session en cours de route, sans rapport avec ce
// projet (vu en vrai : entre quelques images et ~1 Mo transmis, puis la
// connexion vers son relais s'interrompt en plein paquet - cause identifiée
// côté blinkpy, voir blink_engine.py). Une reprise manuelle marche presque
// toujours : on l'automatise. Le compteur d'échecs ne grimpe que sur une
// reprise qui n'aura livré aucune image ; dès qu'une image arrive, il
// retombe à zéro, pour ne pas abandonner un direct qui fonctionne juste par
// à-coups. MSE_BUDGET_TOTAL_MS borne quand même la durée totale : un onglet
// oublié ouvert ne doit pas relancer la caméra indéfiniment. Le délai entre
// deux tentatives n'est pas cosmétique : Blink n'accepte qu'une seule
// session de direct par compte à la fois et met du temps à libérer la
// précédente côté serveur ; une reprise trop rapide se heurte à cette
// session pas encore relâchée, pas à un vrai problème.
const MSE_MAX_ECHECS_A_VIDE = 5;
const MSE_DELAI_RECONNEXION_MS = 3000;
const MSE_BUDGET_TOTAL_MS = 10 * 60 * 1000;

function attendreOuAbandon(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(new DOMException("Aborted", "AbortError")); return; }
    const id = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      clearTimeout(id);
      reject(new DOMException("Aborted", "AbortError"));
    }, { once: true });
  });
}

// Un cycle connexion -> flux -> fin. Renvoie si au moins une image est
// arrivée (utilisé par watchMse pour décider de réessayer ou d'abandonner).
async function connecterMse(name, video, signal, texteAttente, t0) {
  const box = $("live-" + cssId(name));
  if (box && !$("hint-" + cssId(name))) {
    box.insertAdjacentHTML(
      "beforeend",
      `<p class="hint overlay" id="hint-${cssId(name)}">${texteAttente}</p>`
    );
  }
  const hint = $("hint-" + cssId(name));

  const mediaSource = new MediaSource();
  const url = URL.createObjectURL(mediaSource);
  video.src = url;
  let recu = false;
  try {
    await new Promise((resolve, reject) => {
      mediaSource.addEventListener("sourceopen", async () => {
        try {
          const response = await fetch(`/live-mse/${encodeURIComponent(name)}`,
                                        { signal });
          if (!response.ok) {
            let message = tf("watch.refused.code", { code: response.status });
            try {
              const info = await lireJSON(await fetch("/api/live-error"));
              if (info.camera === name && info.message) message = info.message;
            } catch (error) { /* on garde le message générique */ }
            throw new Error(message);
          }
          const codec = response.headers.get("X-Codec") || "avc1.42E01E";
          const mimeType = `video/mp4; codecs="${codec}"`;
          if (!MediaSource.isTypeSupported(mimeType)) {
            throw new Error(tf("watch.codec.unsupported", { codec }));
          }
          const sourceBuffer = mediaSource.addSourceBuffer(mimeType);
          sourceBuffer.mode = "sequence";
          const reader = response.body.getReader();
          for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            await new Promise((res, rej) => {
              sourceBuffer.addEventListener("updateend", res, { once: true });
              sourceBuffer.addEventListener("error", rej, { once: true });
              sourceBuffer.appendBuffer(value);
            });
            if (!recu) {
              recu = true;
              if (hint) hint.remove();
              if (window.__mseMetric == null) window.__mseMetric = performance.now() - t0;
              // L'attribut autoplay seul ne suffit pas toujours à démarrer
              // la lecture sur une balise <video> dont on change juste le
              // src : on le force dès qu'assez de données sont arrivées.
              video.play().catch(() => {});
            }
          }
          if (mediaSource.readyState === "open") mediaSource.endOfStream();
          resolve();
        } catch (error) {
          reject(error);
        }
      }, { once: true });
    });
  } finally {
    URL.revokeObjectURL(url);
  }
  return recu;
}

async function watchMse(name) {
  const box = $("live-" + cssId(name));
  box.innerHTML =
    `<video autoplay muted playsinline></video>
     <button class="watch stop" data-i18n="watch.stop"
             data-action="stop-live" data-name="${h(name)}">${h(t("watch.stop"))}</button>
     ${expandBtn(name)}`;
  const video = box.querySelector("video");
  const t0 = performance.now();
  window.__mseMetric = null;

  const controller = new AbortController();
  MSE_ABORT[name] = controller;

  let echecsAVide = 0;
  let derniereErreur = null;
  while (echecsAVide < MSE_MAX_ECHECS_A_VIDE
         && performance.now() - t0 < MSE_BUDGET_TOTAL_MS) {
    const texte = echecsAVide === 0 && derniereErreur === null
      ? t("watch.waking.mse") : t("watch.reconnecting");
    try {
      const recu = await connecterMse(name, video, controller.signal, texte, t0);
      derniereErreur = null;
      echecsAVide = recu ? 0 : echecsAVide + 1;
    } catch (error) {
      if (error.name === "AbortError") { delete MSE_ABORT[name]; return; }
      derniereErreur = error;
      echecsAVide++;
    }
    if (controller.signal.aborted || echecsAVide >= MSE_MAX_ECHECS_A_VIDE) break;
    try {
      await attendreOuAbandon(MSE_DELAI_RECONNEXION_MS, controller.signal);
    } catch (error) {
      break;  // arrêt demandé pendant l'attente
    }
  }
  delete MSE_ABORT[name];
  if (controller.signal.aborted) return;
  if (derniereErreur) {
    failWatch(name, String(derniereErreur.message || derniereErreur));
  } else {
    // Budget total écoulé pendant que ça fonctionnait : pas un échec, on
    // ramène juste au repos plutôt que d'afficher une erreur trompeuse.
    stopWatch(name);
  }
}

// L'état de repos d'un cadre : la dernière image connue de la caméra, et le
// bouton par-dessus. Arrêter un direct ramène ici, donc la vignette revient au
// lieu de laisser un rectangle noir jusqu'au rechargement de la page.
function repos(name, libelle) {
  return `<img class="still" src="${h(avecJeton(`/camthumb/${encodeURIComponent(name)}`))}" alt="">
     <button class="watch" data-action="watch-live" data-name="${h(name)}">${h(libelle)}</button>
     ${expandBtn(name)}`;
}

// Factorisé : posé à la fois ici (repos, y compris l'état d'échec qui
// réutilise repos()) et dans watchMse() une fois le flux lancé, pour rester
// visible dans tous les états plutôt que d'apparaître seulement en cours de
// lecture.
function expandBtn(name) {
  // Icône seule, jamais de texte : un bouton neuf n'est jamais encore
  // l'élément plein écran courant, donc l'état "entrer" est toujours le bon
  // à la création. syncExpandButtons() corrige l'icône/le libellé ensuite,
  // au changement de langue comme au passage en/hors plein écran.
  return `<button class="watch expand" data-action="fullscreen" data-name="${h(name)}"
                   title="${h(t("live.fullscreen.title"))}"
                   aria-label="${h(t("live.fullscreen.title"))}">⛶</button>`;
}

function syncExpandButtons() {
  const actif = document.fullscreenElement || document.webkitFullscreenElement || null;
  document.querySelectorAll(".watch.expand").forEach((b) => {
    const estActif = b.closest(".live") === actif;
    b.textContent = estActif ? "×" : "⛶";
    const libelle = t(estActif ? "live.fullscreen.title.exit" : "live.fullscreen.title");
    b.title = libelle;
    b.setAttribute("aria-label", libelle);
  });
}
document.addEventListener("fullscreenchange", syncExpandButtons);
document.addEventListener("webkitfullscreenchange", syncExpandButtons);

async function setArmed(scope, name, armed) {
  $("count").textContent = t("command.sending");
  const answer = await fetch("/api/arm", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scope, name, armed }),
  });
  const result = await lireJSON(answer);
  if (result.error) { alert(result.error); return loadSystem(true); }
  system = result;
  renderLive();
}

// Contrairement a setArmed(), peut prendre jusqu'a 2 minutes (voir
// reveiller_camera cote serveur) : le bouton se desactive pendant l'attente
// plutot que de laisser croire qu'un second clic accelererait quoi que ce
// soit. Le try/finally, pas juste le chemin normal, couvre aussi le cas ou
// renderLive() est gele (direct actif ailleurs, voir 28.20) et ne
// remplace donc jamais ce bouton par un neuf.
async function reveillerCamera(name, bouton) {
  const libelle = bouton.textContent;
  bouton.disabled = true;
  bouton.textContent = t("camera.waking");
  try {
    const answer = await fetch("/api/reveiller", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    const result = await lireJSON(answer);
    if (result.error) { alert(result.error); return loadSystem(true); }
    system = result;
    renderLive();
  } finally {
    bouton.disabled = false;
    bouton.textContent = libelle;
  }
}

function renderClips() {
  const clips = visible();
  // Pas de décompte ici : les clips sont sous les yeux, et trois nombres de
  // plus en haut de page ne disent rien qu'on cherchait.
  $("count").textContent = "";

  // À côté du résumé du filtre plutôt que dans la liste : un texte qui décrit
  // ce qui est affiché reste avec le reste de ce qui décrit le filtre, pas
  // mélangé aux résultats eux-mêmes qui défilent.
  $("filtreCompte").textContent = data.filtered
    ? tf("clips.window", { m: data.clips.length, total: data.total_known })
    : "";

  if (!clips.length) {
    // Distinguer « rien ne correspond au filtre » de « rien n'a jamais été
    // récupéré » : dans le second cas, la cause la plus fréquente est l'absence
    // de clé USB sur le module, les enregistrements partant alors dans le cloud
    // de l'abonnement Blink, que cet outil ne lit pas. « Lancez blink2video
    // download » n'a plus sa place ici : avec la composition par défaut
    // (start), le téléchargement tourne déjà en arrière-plan, et le dire
    // dessus n'aidait qu'à confondre un premier utilisateur pile au moment
    // où la page est encore vide (vu en vrai, essai à froid).
    $("list").innerHTML = data.clips.length
      ? `<p class="empty">${t("clips.none.filtered")}</p>`
      : `<p class="empty">${t("clips.none.ever")}</p>`;
    return;
  }
  const days = [...new Set(clips.map((c) => c.day))];
  $("list").innerHTML = days.map((day) => `
    <h2>${h(day)}</h2>
    <div class="grid">${clips.filter((c) => c.day === day).map(card).join("")}</div>
  `).join("");
}

function renderVideos(kind) {
  const items = (videos[kind] || [])
    .filter((v) => !$("camera").value || v.camera === $("camera").value);
  const total = items.reduce((sum, v) => sum + v.duration, 0);
  $("count").textContent = items.length
    ? tf("videos.count", { n: items.length, duree: duration(total) })
    : "";
  if (!items.length) {
    $("list").innerHTML = `<p class="empty">${t("videos.none")}</p>`;
    return;
  }
  const cameras = [...new Set(items.map((v) => v.camera))];
  $("list").innerHTML = cameras.map((camera) => `
    <h2>${h(camera)}</h2>
    <div class="grid wide">
      ${items.filter((v) => v.camera === camera).map(videoCard).join("")}
    </div>
  `).join("");
}

function videoCard(v) {
  const url = `${v.kind}/${encodeURI(v.path)}`;
  const media = avecJeton(`/media/${url}`);
  const poster = avecJeton(`/thumb/${url}`);
  return `<div class="card">
    <video preload="none" controls playsinline
           poster="${h(poster)}" src="${h(media)}"></video>
    <div class="meta">
      <div>
        <div class="time">${h(v.label)}</div>
        <div class="sub">${h(duration(v.duration))}</div>
      </div>
      <a class="act" href="${h(media)}" download>${h(t("videos.download"))}</a>
    </div>
  </div>`;
}

function card(c) {
  const [an, mois, jour] = c.day.split("-");
  const ligne = [c.camera, duration(c.duration), `${jour}/${mois}/${an}`, c.time,
                 c.model].filter(Boolean).join(" · ");
  return `<div class="card ${c.excluded ? "out" : ""}">
    <video preload="none" controls playsinline
           poster="${h(avecJeton(`/thumb/clip/${encodeURI(c.identity)}`))}"
           src="${h(avecJeton(`/media/clip/${encodeURI(c.identity)}`))}"></video>
    <div class="meta">
      <div class="time line">${h(ligne)}</div>
      <label class="act" title="${h(t("clip.discard.title"))}">
        <input type="checkbox" ${c.excludedStaged ? "checked" : ""}
               data-action="stage-exclusion" data-identity="${h(c.identity)}">
        ${h(t("clip.discard"))}
      </label>
      ${c.sourceDeleted || (data.suppressionAuto || []).includes(c.cameraKey) ? "" : `
      <label class="act" title="${h(t("clip.deleteSource.title"))}">
        <input type="checkbox" ${c.supprimerStaged ? "checked" : ""}
               data-action="stage-suppression" data-identity="${h(c.identity)}">
        ${h(t("clip.deleteSource"))}
      </label>`}
    </div>
  </div>`;
}

// Le filtre (caméra + période) survit d'une visite à l'autre : quelqu'un qui
// ne veut voir que les clips de la semaine ne doit pas refaire ce choix à
// chaque ouverture de la page. Défaut « tout l'historique » tant que rien
// n'a jamais été choisi (constaté en réel, 2026-08-27).
const CLE_FILTRE = "blink2video.filtre";

function sauvegarderFiltre() {
  try {
    localStorage.setItem(CLE_FILTRE, JSON.stringify(
      { camera: $("camera").value, plage: plageClips }));
  } catch (erreur) { /* stockage indisponible (navigation privée…) : tant pis */ }
}

function restaurerFiltre() {
  try {
    const brut = localStorage.getItem(CLE_FILTRE);
    return brut ? JSON.parse(brut) : null;
  } catch (erreur) {
    return null;
  }
}

const _filtrePersiste = restaurerFiltre();
let plageClips = _filtrePersiste?.plage || { preset: "all" };
// Choix en cours dans le panneau, appliqué seulement au clic sur Filtrer -
// tant qu'aucun préréglage ni plage personnalisée n'a été retouché dans
// cette ouverture du panneau, Filtrer ne fait que reprendre plageClips.
let plageEnAttente = null;
// La caméra restaurée ne peut être posée qu'une fois la vraie liste connue
// (premier fill(), voir load()) : un <select> vide ignore toute valeur qu'on
// lui donne avant d'avoir ses <option>.
let _filtreCameraAppliquee = false;

function paramsPourPlage() {
  const params = new URLSearchParams();
  if (plageClips.preset === "all") params.set("all", "1");
  else if (plageClips.preset) params.set("preset", plageClips.preset);
  else {
    if (plageClips.depuis) params.set("depuis", plageClips.depuis);
    if (plageClips.jusqua) params.set("jusqua", plageClips.jusqua);
  }
  const s = params.toString();
  return s ? `?${s}` : "";
}

// Deux load() peuvent se chevaucher (clics rapprochés entre préréglages, ou
// un load() de fond pendant qu'un autre tourne encore) : sans annulation de
// celui d'avant, une requête plus lente mais lancée plus tôt (souvent celle
// du chargement initial, sur « ce mois-ci ») pouvait répondre après une plus
// rapide et réappliquer des données périmées par-dessus - la sélection
// paraissait alors bloquée sur ce mois-ci, ou la grille rendait deux fois
// coup sur coup (clignotement). AbortController est le mécanisme standard
// pour ça, pas une invention locale (constaté en réel, 2026-08-27).
let __chargementEnCours = null;

async function load() {
  __chargementEnCours?.abort();
  const controleur = new AbortController();
  __chargementEnCours = controleur;
  let answer, videoAnswer;
  try {
    [answer, videoAnswer] = await Promise.all([
      fetch(`/api/clips${paramsPourPlage()}`, { signal: controleur.signal }),
      fetch("/api/videos", { signal: controleur.signal }),
    ]);
  } catch (erreur) {
    if (erreur.name === "AbortError") return; // une requête plus récente a pris le relais
    throw erreur;
  }
  data = await lireJSON(answer);
  videos = await lireJSON(videoAnswer);
  if (data.error) { $("log").style.display = "block"; $("log").textContent = data.error; return; }
  // État de la sélection : à zéro à chaque rechargement, il reflète l'état
  // réel qu'on vient de relire, pas une intention encore en attente.
  for (const c of data.clips || []) {
    c.excludedStaged = c.excluded;
    c.supprimerStaged = false;
  }
  // Le modèle accompagne le nom ici, une fois, plutôt que sur chaque vignette.
  fill($("camera"), data.cameras, t("filter.allcameras"),
       (nom) => [nom, (data.models || {})[nom]].filter(Boolean).join(" · "));
  // La caméra restaurée ne peut être posée qu'une fois ; les fois suivantes,
  // fill() a déjà de quoi préserver seul la sélection en cours (voir sa
  // propre note sur `kept`).
  if (!_filtreCameraAppliquee) {
    _filtreCameraAppliquee = true;
    if (_filtrePersiste?.camera && data.cameras.includes(_filtrePersiste.camera)) {
      $("camera").value = _filtrePersiste.camera;
    }
  }
  render();
  majBoutonAppliquer();
}

// Un préréglage suffit à retrouver un incident récent (aujourd'hui, cette
// semaine…) ; la plage personnalisée, elle, vise une période précise à
// l'heure près, pour ne pas défiler tout l'historique d'une caméra toujours
// armée (signalé sur Reddit, 2026-08-27). Ni l'un ni l'autre ne s'applique
// tant que Filtrer n'a pas été cliqué : choisir un préréglage puis se
// raviser pour une caméra ne doit pas déjà avoir rechargé la grille une
// première fois pour rien.
function choisirPreset(nom) {
  plageEnAttente = { preset: nom };
  $("rangeFrom").value = ""; $("rangeTo").value = "";
  for (const bouton of document.querySelectorAll("#filtre .presets button")) {
    bouton.classList.toggle("primary", bouton.dataset.preset === nom);
  }
}

function choisirPlagePersonnalisee() {
  const depuis = $("rangeFrom").value, jusqua = $("rangeTo").value;
  plageEnAttente = (depuis || jusqua) ? { depuis, jusqua } : null;
  for (const bouton of document.querySelectorAll("#filtre .presets button")) {
    bouton.classList.remove("primary");
  }
}

function ouvrirFiltre() {
  plageEnAttente = null;
  for (const bouton of document.querySelectorAll("#filtre .presets button")) {
    bouton.classList.toggle("primary", bouton.dataset.preset === plageClips.preset);
  }
  $("rangeFrom").value = plageClips.depuis || "";
  $("rangeTo").value = plageClips.jusqua || "";
  $("filtre").showModal();
}

async function appliquerFiltre() {
  if (plageEnAttente) plageClips = plageEnAttente;
  sauvegarderFiltre();
  $("filtre").close();
  // Pas de message de chargement intermédiaire : /api/clips répond en
  // quelques millisecondes en local, trop vite pour se lire comme un
  // chargement - seulement comme un clignotement de toute la grille à
  // chaque changement de filtre (constaté en réel, 2026-08-27).
  await load();
}

// Une plage personnalisée affiche les dates réellement choisies plutôt
// qu'un « Période personnalisée » générique : c'est justement pour cibler
// une période précise qu'on l'a choisie, autant la voir sans rouvrir le
// panneau.
function libellePlagePersonnalisee() {
  const formater = (v) => {
    if (!v) return null;
    const [date, heure] = v.split("T");
    const [, mois, jour] = date.split("-");
    return `${jour}/${mois} ${heure || "00:00"}`;
  };
  const depuis = formater(plageClips.depuis);
  const jusqua = formater(plageClips.jusqua);
  if (depuis && jusqua) return `${depuis} → ${jusqua}`;
  if (depuis) return tf("range.custom.depuis", { v: depuis });
  if (jusqua) return tf("range.custom.jusqua", { v: jusqua });
  return t("range.custom");
}

// Résumé affiché dans l'en-tête à côté du bouton Filtre, pour savoir ce qui
// est actif sans rouvrir le panneau. La caméra y figure toujours, y compris
// le défaut silencieux « toutes caméras » - même logique que la période,
// déjà affichée même sur son propre défaut (« tout l'historique ») : les
// deux disent la vérité sur ce qui est montré, jamais seulement ce qui
// restreint quelque chose.
function resumeFiltre() {
  const morceaux = [];
  const option = $("camera").selectedOptions[0];
  morceaux.push(option ? option.textContent : t("filter.allcameras"));
  if ($("view").value === "clips") {
    morceaux.push(plageClips.preset ? t(`range.${plageClips.preset}`) : libellePlagePersonnalisee());
  }
  return morceaux.join(" · ");
}

// Écarter et Supprimer sont des cases, pas des actions immédiates : coup par
// coup, la suppression USB paierait à chaque clic le délai de régénération
// du manifeste par le Sync Module (jusqu'à une minute, voir AUDIT 28.73/75).
// Le bouton Appliquer traite tout le lot en un seul appel, une seule lecture
// de manifeste par Sync Module concerné plutôt qu'une par clip.
function stagerExclusion(identity, coche) {
  const clip = data.clips.find((c) => c.identity === identity);
  if (clip) clip.excludedStaged = coche;
  majBoutonAppliquer();
}

function stagerSuppression(identity, coche) {
  const clip = data.clips.find((c) => c.identity === identity);
  if (clip) clip.supprimerStaged = coche;
  majBoutonAppliquer();
}

function calculerSelection() {
  const exclure = [], inclure = [], supprimer = [];
  for (const c of data.clips || []) {
    if (!!c.excludedStaged !== !!c.excluded) (c.excludedStaged ? exclure : inclure).push(c.identity);
    if (c.supprimerStaged && !c.sourceDeleted) supprimer.push(c.identity);
  }
  return { exclure, inclure, supprimer };
}

function majBoutonAppliquer() {
  const bouton = $("applyButton");
  if (!bouton) return;
  const { exclure, inclure, supprimer } = calculerSelection();
  const n = exclure.length + inclure.length + supprimer.length;
  bouton.hidden = n === 0;
  bouton.textContent = tf("selection.apply", { n });
}

async function appliquerSelection() {
  const { exclure, inclure, supprimer } = calculerSelection();
  if (!exclure.length && !inclure.length && !supprimer.length) return;
  if (supprimer.length && !confirm(tf("selection.confirm.suppression", { n: supprimer.length }))) return;
  const bouton = $("applyButton");
  bouton.disabled = true;
  bouton.textContent = t("clip.deleteSource.pending");
  try {
    const reponse = await fetch("/api/appliquer-selection", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ exclure, inclure, supprimer }) });
    const resultat = await lireJSON(reponse);
    if (resultat.error) { alert(resultat.error); return; }
    // Écarter/Réintégrer : mise à jour optimiste, comme l'ancien toggle()
    // (AUDIT 28.33/28.75). Le registre s'écrit en tâche de fond côté
    // serveur pour ne jamais bloquer la réponse ; la relire tout de suite
    // ici la course parfois, avant que l'écriture n'ait fini (constaté par
    // Nico : « les écartés restent présents » jusqu'à un F5 manuel).
    for (const identity of exclure) {
      const clip = data.clips.find((c) => c.identity === identity);
      if (clip) { clip.excluded = true; clip.excludedStaged = true; }
    }
    for (const identity of inclure) {
      const clip = data.clips.find((c) => c.identity === identity);
      if (clip) { clip.excluded = false; clip.excludedStaged = false; }
    }
    // Supprimer : l'appel était synchrone côté serveur, le résultat est
    // donc fiable tout de suite, pas une supposition.
    let echecs = 0;
    for (const [identity, statut] of Object.entries(resultat.resultats || {})) {
      const clip = data.clips.find((c) => c.identity === identity);
      if (statut === "supprime" || statut === "deja_absent") {
        if (clip) { clip.sourceDeleted = true; clip.supprimerStaged = false; }
      } else {
        echecs++;
      }
    }
    if (echecs) alert(tf("selection.partial", { n: echecs }));
    render();
  } catch (erreur) {
    alert(String(erreur));
  } finally {
    bouton.disabled = false;
    majBoutonAppliquer();
  }
}

// --- connexion Blink -------------------------------------------------------
// Le serveur garde la session Blink ouverte entre les deux requêtes : la page
// n'a qu'à poser les questions dans l'ordre où il les réclame.
let authResolve = null;

function showAuth(stage, message) {
  $("authError").textContent = message || "";
  const code = stage === "2fa";
  $("authCreds").hidden = code;
  $("authCode").hidden = !code;
  $("authTitle").textContent = code ? t("auth.title.2fa") : t("auth.title");
  $("authHint").textContent = code ? t("auth.hint.2fa") : t("auth.hint");
  $("authOk").textContent = code ? t("auth.validate") : t("auth.ok");
  if (!$("auth").open) $("auth").showModal();
  (code ? $("code") : $("user")).focus();
}

function authenticate() {
  return new Promise((resolve) => {
    authResolve = resolve;
    showAuth("creds", "");
  });
}

$("passToggle").onclick = () => {
  const masque = $("pass").type === "password";
  $("pass").type = masque ? "text" : "password";
  $("passToggle").textContent = t(masque ? "auth.hide" : "auth.show");
  $("passToggle").setAttribute("aria-label", t(masque ? "auth.hide.aria" : "auth.show.aria"));
};

$("authCancel").onclick = () => {
  $("auth").close();
  if (authResolve) { authResolve(false); authResolve = null; }
};

$("authOk").onclick = async () => {
  const code = !$("authCode").hidden;
  $("authOk").disabled = true;
  $("authError").textContent = t("auth.connecting");
  const body = code
    ? { code: $("code").value }
    : { username: $("user").value, password: $("pass").value };
  let result;
  try {
    const answer = await fetch(code ? "/api/2fa" : "/api/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    result = await lireJSON(answer);
  } catch (error) {
    result = { status: "error", message: String(error) };
  }
  $("authOk").disabled = false;
  $("pass").value = "";
  if (result.status === "ok") {
    $("auth").close();
    $("code").value = "";
    // ?login=1 n'a servi qu'à ouvrir ce dialogue au premier lancement ; le
    // laisser dans l'adresse referait apparaître la connexion à chaque
    // actualisation ou depuis un signet, alors que la session est valide.
    const parametres = new URLSearchParams(location.search);
    if (parametres.get("login") === "1") {
      parametres.delete("login");
      const suite = parametres.toString();
      history.replaceState(null, "", location.pathname + (suite ? `?${suite}` : ""));
    }
    if (authResolve) { authResolve(true); authResolve = null; }
  } else if (result.status === "2fa") {
    $("code").value = "";
    showAuth("2fa", "");
  } else {
    showAuth(code ? "2fa" : "creds", result.message || t("auth.failed"));
  }
};

$("refresh").onclick = async () => {
  const status = await lireJSON(await fetch("/api/status"));
  if (!status.authenticated && !(await authenticate())) return;
  if (status.initial_setup) {
    await ouvrirReglages(true);
    return;
  }

  const button = $("refresh");
  button.disabled = true;
  actualisationLocale = true;
  $("log").style.display = "block";
  $("log").textContent = "";
  $("work").classList.add("on");
  let label = t("refresh.starting");
  $("phase").textContent = label;
  $("bar").removeAttribute("value");   // barre indéterminée tant qu'on ne sait pas

  const source = new EventSource(avecJeton("/api/refresh"));
  source.onmessage = (message) => {
    const event = JSON.parse(message.data);
    if (event.phase) {
      label = event.phase_key === "phase.usb_section"
        ? libellePhase(event.phase_key, event.phase, { hub: event.phase_hub })
        : libellePhase(event.phase_key, event.phase);
      $("phase").textContent = label;
      $("bar").removeAttribute("value");
    }
    if (event.progress) {
      // done est fractionnaire : la partie entière compte les clips terminés,
      // la décimale l'avancement dans le clip en cours. La barre est donc
      // continue au lieu de sauter d'un cran par clip.
      const p = event.progress;
      $("bar").max = p.total;
      $("bar").value = p.done;
      const current = Math.min(Math.floor(p.done) + 1, p.total);
      $("phase").textContent =
        `${label} ${current}/${p.total} (${Math.round((p.done / p.total) * 100)} %)`;
    }
    if (event.line !== undefined) {
      $("log").textContent += event.line + "\\n";
      $("log").scrollTop = $("log").scrollHeight;
    }
    if (event.done) {
      // Sans close(), EventSource se reconnecte tout seul et relancerait
      // l'actualisation en boucle.
      source.close();
      $("work").classList.remove("on");
      button.disabled = false;
      actualisationLocale = false;
      if (!event.ok) $("phase").textContent = t("refresh.errors");
      load();
      // « Actualiser » ne rapatriait que les clips : la batterie, la
      // température et le signal de chaque caméra restaient sur leur
      // dernière lecture, parfois vieille de plusieurs jours, tant qu'on
      // n'ouvrait pas soi-même l'onglet Direct (bug vécu en vrai : une
      // caméra affichait une mesure ancienne jusqu'à un rafraîchissement
      // manuel depuis l'appli officielle). loadSystem(true) force le même
      // passage que cette appli fait de son côté (blink.refresh(force=True),
      // qui relit vraiment chaque caméra, pas seulement le résumé du
      // compte - voir system_state() côté serveur).
      loadSystem(true);
    }
  };
  source.onerror = () => {
    source.close();
    $("work").classList.remove("on");
    button.disabled = false;
    actualisationLocale = false;
    $("log").textContent += t("refresh.disconnected");
  };
};

for (const id of ["view", "showOut"]) $(id).onchange = render;
// Seule cette ligne de texte se met à jour d'elle-même : elle sert précisément
// à repérer une boucle arrêtée, ce qu'on ne verrait pas en regardant des clips
// qui, eux, ne changent plus.
// Un worker automatique peut commencer à n'importe quel moment. La sonde
// dédiée ne lit que .blink_travail.json : trois secondes en permanence restent
// légères, même sous Windows 7. Le bilan plus coûteux (registre + passages +
// mise à jour) conserve, lui, sa cadence d'une minute.
etatDuTravail();
(function veillerTravail() {
  setTimeout(async () => {
    await etatDuTravail();
    veillerTravail();
  }, 3000);
})();
(function veillerPassages() {
  setTimeout(async () => {
    await heuresDePassage();
    veillerPassages();
  }, 60000);
})();
$("auto").checked = localStorage.getItem("auto") === "1";
$("auto").onchange = () => {
  localStorage.setItem("auto", $("auto").checked ? "1" : "0");
  heuresDePassage();
};
// Reflète l'état réel du système (fichier de démarrage présent ou non), pas
// une préférence mémorisée côté page : deux installations d'un même profil
// navigateur ne doivent pas se faire croire l'état de l'autre.
fetch("/api/autostart").then(lireJSON).then((etat) => {
  $("autostart").checked = !!etat.actif;
}).catch(() => {});
$("autostart").onchange = async () => {
  const voulu = $("autostart").checked;
  $("autostart").disabled = true;
  try {
    const reponse = await fetch("/api/autostart", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ actif: voulu }),
    });
    const etat = await lireJSON(reponse);
    if (etat.error) alert(etat.error);
    $("autostart").checked = !!etat.actif;
  } catch (error) {
    alert(String(error));
    $("autostart").checked = !voulu;
  } finally {
    $("autostart").disabled = false;
  }
};

let portActuel = null;   // relu à chaque ouverture, comparé à l'envoi

async function ouvrirReglages(configurationInitiale = false) {
  try {
    const reglages = await lireJSON(await fetch("/api/reglages"));
    configurationInitiale = configurationInitiale || !!reglages.initial_setup;
    $("usbMinutes").value = reglages.usb_minutes;
    $("cloudMinutes").value = reglages.cloud_minutes;
    $("port").value = reglages.port;
    portActuel = reglages.port;
    $("storageDir").value = reglages.storage_dir;
    $("timestamp").checked = reglages.timestamp;
    $("timezone").value = reglages.timezone;
    $("mergeJour").checked = reglages.merge_jour;
    $("mergeSemaine").checked = reglages.merge_semaine;
    $("mergeMois").checked = reglages.merge_mois;
    appliquerDependanceMergeJour();
    $("downloadAuto").checked = reglages.download_auto;
    appliquerDependanceDownloadAuto();
  } catch (erreur) { /* les champs gardent leur dernière valeur affichée */ }
  $("initialSetupHint").hidden = !configurationInitiale;
  $("reglagesClose").hidden = configurationInitiale;
  $("stopButton").hidden = configurationInitiale;
  $("reglages").dataset.initialSetup = configurationInitiale ? "1" : "0";
  chargerSourdine();
  chargerSuppressionAuto();
  $("reglages").showModal();
}
$("reglagesButton").onclick = () => ouvrirReglages(false);
$("reglagesClose").onclick = () => $("reglages").close();
$("reglages").addEventListener("cancel", (evenement) => {
  // Échap ne doit pas transformer une installation neuve en page vide : le
  // parent attend la validation et aucun téléchargement ne démarrera. Le
  // dialogue reste fermable lors de toutes les ouvertures ordinaires.
  if ($("reglages").dataset.initialSetup === "1") evenement.preventDefault();
});

$("filtreButton").onclick = ouvrirFiltre;
$("filtreClose").onclick = () => $("filtre").close();
$("filtreApply").onclick = appliquerFiltre;
for (const bouton of document.querySelectorAll("#filtre .presets button")) {
  bouton.onclick = () => choisirPreset(bouton.dataset.preset);
}
$("rangeFrom").oninput = choisirPlagePersonnalisee;
$("rangeTo").oninput = choisirPlagePersonnalisee;

// Ouvre le sélecteur natif côté serveur (tkinter : voir /api/choisir-dossier)
// plutôt qu'un <input type="file" webkitdirectory> - celui-ci ne rend qu'un
// nom de dossier relatif au navigateur, jamais un chemin absolu utilisable
// par le serveur (restriction de vie privée du web, pas une limite de ce code).
$("storageDirBrowse").onclick = async () => {
  const bouton = $("storageDirBrowse");
  bouton.disabled = true;
  try {
    const reponse = await fetch("/api/choisir-dossier");
    const resultat = await lireJSON(reponse);
    if (resultat.error) {
      alert(t("reglages.storageDir.browse.unavailable"));
    } else if (resultat.path) {
      $("storageDir").value = resultat.path;
    }
  } catch (erreur) {
    alert(t("reglages.storageDir.browse.unavailable"));
  } finally {
    bouton.disabled = false;
  }
};

// Semaine et mois n'ont de sens que si la journalière tourne : décocher
// « jour » les grise et les décoche, plutôt que de laisser espérer un
// agrégat qui ne sera jamais construit faute de base.
function appliquerDependanceMergeJour() {
  const actif = $("mergeJour").checked;
  $("mergeSemaine").disabled = !actif;
  $("mergeMois").disabled = !actif;
  if (!actif) {
    $("mergeSemaine").checked = false;
    $("mergeMois").checked = false;
  }
}
$("mergeJour").onchange = appliquerDependanceMergeJour;

// Les cadences n'ont plus de sens si rien n'est téléchargé : grisées plutôt
// que retirées, pour retrouver la dernière valeur en recochant.
function appliquerDependanceDownloadAuto() {
  const actif = $("downloadAuto").checked;
  $("usbMinutes").disabled = !actif;
  $("cloudMinutes").disabled = !actif;
}
$("downloadAuto").onchange = appliquerDependanceDownloadAuto;

// Séparé du reste du panneau : contrairement aux cadences, au port ou au
// fuseau, la sourdine n'exige pas de redémarrage (watch relit son état à
// chaque passage), donc chaque case s'applique tout de suite, comme le
// bouton Écarter d'un clip.
async function chargerSourdine() {
  const conteneur = $("sourdineListe");
  conteneur.textContent = t("sourdine.loading");
  let etat;
  try {
    etat = await lireJSON(await fetch("/api/sourdine"));
  } catch (erreur) {
    conteneur.textContent = t("sourdine.unavailable");
    return;
  }
  if (!etat.cameras.length) {
    conteneur.textContent = t("sourdine.none");
    return;
  }
  conteneur.replaceChildren();
  if ((etat.legacy_ignored || []).length) {
    const avertissement = document.createElement("p");
    avertissement.textContent = t("suppressionAuto.legacy");
    conteneur.appendChild(avertissement);
  }
  for (const camera of etat.cameras) {
    const label = document.createElement("label");
    const case_ = document.createElement("input");
    case_.type = "checkbox";
    case_.checked = etat.ignored.includes(camera);
    case_.onchange = async () => {
      case_.disabled = true;
      try {
        const reponse = await fetch("/api/sourdine", { method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ camera, ignored: case_.checked }) });
        const resultat = await lireJSON(reponse);
        if (resultat.error) {
          alert(resultat.error);
          case_.checked = !case_.checked;
        }
      } catch (erreur) {
        alert(String(erreur));
        case_.checked = !case_.checked;
      } finally {
        case_.disabled = false;
      }
    };
    label.appendChild(case_);
    label.append(` ${camera}`);
    conteneur.appendChild(label);
  }
}

// Même motif que chargerSourdine() : chaque case s'applique tout de suite,
// pas de redémarrage. Liste distincte (issue GitHub #1) : seules les
// caméras vues sur la clé USB ont un sens ici, le cloud de l'abonnement
// n'est jamais concerné par cette suppression.
async function chargerSuppressionAuto() {
  const conteneur = $("suppressionAutoListe");
  conteneur.textContent = t("suppressionAuto.loading");
  let etat;
  try {
    etat = await lireJSON(await fetch("/api/suppression-auto"));
  } catch (erreur) {
    conteneur.textContent = t("suppressionAuto.unavailable");
    return;
  }
  if (!etat.cameras.length) {
    conteneur.textContent = t("suppressionAuto.none");
    return;
  }
  conteneur.innerHTML = "";
  for (const camera of etat.cameras) {
    const label = document.createElement("label");
    const case_ = document.createElement("input");
    case_.type = "checkbox";
    case_.checked = etat.actives.includes(camera.key);
    case_.onchange = async () => {
      case_.disabled = true;
      try {
        const reponse = await fetch("/api/suppression-auto", { method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ camera: camera.key, actif: case_.checked }) });
        const resultat = await lireJSON(reponse);
        if (resultat.error) {
          alert(resultat.error);
          case_.checked = !case_.checked;
        }
      } catch (erreur) {
        alert(String(erreur));
        case_.checked = !case_.checked;
      } finally {
        case_.disabled = false;
      }
    };
    label.appendChild(case_);
    label.append(` ${camera.name}${camera.detail ? ` · ${camera.detail}` : ""}`);
    conteneur.appendChild(label);
  }
}

// Même déroulé que le bouton de mise à jour : enregistrer, attendre que le
// serveur disparaisse puis revienne, recharger. Le verbe diffère (« restart »
// au lieu de « update ») puisqu'aucune nouvelle version n'est en jeu, mais
// c'est le même arrêt-puis-relance vu de la page. Si le port change, la page
// qui redémarre n'écoute plus à la même adresse : le sondage habituel (même
// origine) ne verrait jamais le retour, il faut viser la nouvelle adresse.
$("reglagesApply").onclick = async () => {
  const usb = parseInt($("usbMinutes").value, 10);
  const cloud = parseInt($("cloudMinutes").value, 10);
  const port = parseInt($("port").value, 10);
  if (!(usb >= 1) || !(cloud >= 1)) {
    alert(t("reglages.error.cadence"));
    return;
  }
  if (!(port >= 1 && port <= 65535)) {
    alert(t("reglages.error.port"));
    return;
  }
  const timezone = $("timezone").value.trim();
  if (!timezone) {
    alert(t("reglages.error.timezone"));
    return;
  }
  const bouton = $("reglagesApply");
  let configurationInitiale = false;
  bouton.disabled = true;
  bouton.textContent = t("reglages.restarting");
  try {
    const storageDir = $("storageDir").value.trim();
    const timestamp = $("timestamp").checked;
    const mergeJour = $("mergeJour").checked;
    const mergeSemaine = $("mergeSemaine").checked;
    const mergeMois = $("mergeMois").checked;
    const downloadAuto = $("downloadAuto").checked;
    const reponse = await fetch("/api/reglages", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ usb_minutes: usb, cloud_minutes: cloud, port,
                             storage_dir: storageDir, timestamp, timezone,
                             merge_jour: mergeJour, merge_semaine: mergeSemaine,
                             merge_mois: mergeMois, download_auto: downloadAuto }) });
    const resultat = await lireJSON(reponse);
    if (resultat.error) {
      alert(resultat.error);
      bouton.disabled = false;
      bouton.textContent = t("reglages.apply");
      return;
    }
    configurationInitiale = !!resultat.initial_setup;
  } catch (erreur) {
    // Une erreur de validation arrive toujours en JSON propre, AVANT que le
    // serveur ne se tue pour redémarrer (voir resultat.error ci-dessus) :
    // si on arrive ici, c'est que la réponse a été coupée par ce
    // redémarrage lui-même, pas que la sauvegarde a échoué. Même principe
    // que le bouton Stop plus bas : on continue comme en cas de succès
    // plutôt que d'alarmer à tort sur une erreur réseau qui ne veut rien
    // dire ici.
  }
  bouton.disabled = false;
  bouton.textContent = t("reglages.apply");
  $("reglages").close();

  if (port !== portActuel) {
    // Un délai fixe se serait trompé de quelques secondes selon la charge
    // de la machine : on attend plutôt la confirmation que l'ancien
    // serveur (cette origine) a bien disparu, comme pour un redémarrage
    // ordinaire, avant de viser la nouvelle adresse.
    const nouvelleAdresse = `http://${location.hostname}:${port}/`;
    $("phase").textContent = tf("reglages.portchange", { url: nouvelleAdresse });
    $("bar").removeAttribute("value");
    $("work").classList.add("on");
    $("refresh").disabled = true;
    let parti = false;
    const attentePort = setInterval(async () => {
      try {
        await fetch("/api/status", { cache: "no-store" });
      } catch (erreur) {
        parti = true;
      }
      if (parti) {
        clearInterval(attentePort);
        // L'ancien a disparu ; le nouveau, déjà en cours de lancement,
        // a besoin d'un instant de plus pour se lier au port.
        setTimeout(() => { location.href = nouvelleAdresse; }, 3000);
      }
    }, 1000);
    setTimeout(() => {
      if (!parti) {
        clearInterval(attentePort);
        $("phase").textContent = t("reglages.restartFailed");
        $("bar").value = 0;
        $("refresh").disabled = false;
      }
    }, 45000);
    return;
  }

  if (configurationInitiale) {
    // Le parent ``start`` remplace le serveur temporaire par le serveur
    // complet seulement après avoir vu la validation. Attendre explicitement
    // un /api/status sorti du mode initial évite de manquer une coupure très
    // brève sur une machine rapide et de rester bloqué inutilement.
    $("phase").textContent = t("reglages.restarting.settings");
    $("bar").removeAttribute("value");
    $("work").classList.add("on");
    $("refresh").disabled = true;
    let delaiInitial = null;
    const attenteInitiale = setInterval(async () => {
      try {
        const etat = await (await fetch("/api/status", { cache: "no-store" })).json();
        if (etat.initial_setup === false) {
          clearInterval(attenteInitiale);
          clearTimeout(delaiInitial);
          // Retire ?setup=1 : le serveur définitif ne doit pas rouvrir le
          // panneau qui vient précisément d'être validé.
          location.href = location.pathname;
        }
      } catch (erreur) { /* coupure attendue entre les deux serveurs */ }
    }, 1000);
    delaiInitial = setTimeout(() => {
      clearInterval(attenteInitiale);
      $("phase").textContent = t("reglages.restartFailed");
      $("bar").value = 0;
      $("refresh").disabled = false;
    }, 60000);
    return;
  }

  $("phase").textContent = t("reglages.restarting.settings");
  $("bar").removeAttribute("value");
  $("work").classList.add("on");
  $("refresh").disabled = true;
  let parti = false;
  const attente = setInterval(async () => {
    try {
      await fetch("/api/status", { cache: "no-store" });
      if (parti) location.reload();
    } catch (erreur) {
      parti = true;      // il s'est arrêté : la relance suit
    }
  }, 2000);
  setTimeout(() => {
    if (!parti) {
      clearInterval(attente);
      $("phase").textContent = t("reglages.restartFailed");
      $("bar").value = 0;
      $("refresh").disabled = false;
    }
  }, 45000);
};

$("stopButton").onclick = async () => {
  const bouton = $("stopButton");
  bouton.disabled = true;
  bouton.textContent = t("stop.stopping");
  try {
    const reponse = await fetch("/api/stop", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: "{}" });
    const resultat = await reponse.json();
    if (resultat.error) {
      alert(resultat.error);
      bouton.disabled = false;
      bouton.textContent = t("reglages.stop");
      return;
    }
  } catch (erreur) { /* la réponse peut ne pas arriver, l'arrêt est déjà lancé */ }
  document.body.innerHTML = `<p class="empty">${t("stop.stopped")}</p>`;
  // « accepté » n'est pas « terminé ». Si le serveur répond encore après le
  // délai maximal de grâce + kill, rendre l'échec visible au lieu d'affirmer
  // indéfiniment que l'application est arrêtée.
  setTimeout(async () => {
    try {
      const reponse = await fetch("/api/status", { cache: "no-store" });
      if (reponse.ok) {
        document.body.innerHTML = `<p class="empty">${t("stop.failed")}</p>`;
      }
    } catch (erreur) { /* disparition attendue : arrêt confirmé */ }
  }, 30000);
};

// Les cartes sont reconstruites à chaque actualisation : une délégation sur
// leur conteneur garde les handlers hors des chaînes HTML et évite toute
// interprétation d'un nom de caméra ou de fichier comme code JavaScript.
$("list").addEventListener("click", (event) => {
  const cible = event.target.closest("[data-action]");
  if (!cible || !$("list").contains(cible)) return;
  const name = cible.dataset.name || "";
  switch (cible.dataset.action) {
    case "arm":
      setArmed(cible.dataset.scope, name, cible.dataset.armed === "true");
      break;
    case "wake":
      reveillerCamera(name, cible);
      break;
    case "watch-live":
      watchMse(name);
      break;
    case "stop-live":
      stopWatch(name);
      break;
    case "fullscreen":
      toggleFullscreen(name);
      break;
  }
});
$("list").addEventListener("change", (event) => {
  const cible = event.target.closest("[data-action]");
  if (!cible || !$("list").contains(cible)) return;
  if (cible.dataset.action === "stage-exclusion") {
    stagerExclusion(cible.dataset.identity, cible.checked);
  } else if (cible.dataset.action === "stage-suppression") {
    stagerSuppression(cible.dataset.identity, cible.checked);
  }
});
$("applyButton").onclick = appliquerSelection;
document.querySelectorAll("[data-lang-btn]").forEach((button) => {
  button.onclick = () => setLang(button.dataset.langBtn, true);
});

// Vue par défaut posée AVANT setLang() : celui-ci appelle render(), qui lit
// $("view").value pour décider quoi peindre. Sans cadre du navigateur pour
// distinguer une option "selected" ici, la valeur par défaut du <select>
// serait la première déclarée (Direct) - render() y déclencherait alors un
// appel réseau vers /api/system dont la réponse, arrivée en retard, écrase
// la liste de clips déjà affichée sans que le menu déroulant ne bouge (bug
// vécu en conditions réelles : la page « repassait en Direct toute seule »).
$("view").value = "clips";
// Override manuel mémorisé prioritaire ; sinon la langue du navigateur, comme
// au premier lancement de lidar2map. Placé ici, en fin de script : setLang()
// appelle render()/renderLive(), qui référencent des `let` déclarés plus haut
// (MSE_ABORT, actualisationLocale...) — appelé trop tôt, avant l'exécution de
// ces déclarations, ça lève ReferenceError (zone morte temporelle), comme vu
// en testant réellement au navigateur.
setLang(localStorage.getItem("lang") || detectLang(), false);

load();
// E-01 : connexion puis réglages s'enchaînent dans le même onglet. Le second
// dialogue n'est ouvert qu'après une authentification réussie ; l'annulation
// laisse le parent en attente et garantit qu'aucun worker ne démarre.
(async function lancerParcoursInitial() {
  const parametres = new URLSearchParams(location.search);
  let continuer = true;
  if (parametres.get("login") === "1") continuer = await authenticate();
  if (continuer && parametres.get("setup") === "1") {
    await ouvrirReglages(true);
  }
})();
