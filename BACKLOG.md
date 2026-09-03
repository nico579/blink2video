# Backlog

Demandes venues de Reddit (ou d'ailleurs) pas encore traitees. But : ne pas
les perdre, pas forcement les construire toutes.

- **Integration Home Assistant native.**
  Source : reddit/MoneySquare6212, r/blinkcameras, 2026-08-19. Ecarte pour
  l'instant (repondu sur Reddit) : un vrai chantier a part (config flow,
  modele d'entites, HACS), pas une extension de ce qui existe. Note ici
  pour ne pas l'oublier si la demande revient.

- **Indicateur de mise a jour pas assez visible.**
  Source : reddit/SR_gAr, r/blinkcameras, 2026-08-20. A dit avoir ete
  "perdu dans toutes les infos" et ne pas avoir vu qu'une mise a jour etait
  disponible, a cru manquer quelque chose. Signal faible (un seul retour)
  mais a surveiller : si ca revient, revoir la visibilite du bouton/de la
  bascule "Mettre a jour" dans reglages (et maintenant dans le menu de
  l'icone tray, 28.58).

- **Tester-XR.exe affiche une "blink2video version" perimee dans son
  rapport.** Source : reddit/cutthin, auteur de la demande initiale de prise
  en charge du Sync Module XR et des tests sur materiel reel, 2026-08-31. Le
  rapport du testeur XR (r2, tag xr-local-storage-test-r2) annoncait "blink2video
  version: 0.10.6" alors que l'utilisateur confirmait tourner sous 0.10.08.
  Confirme via `gh release list` : le tag r2 a ete publie le
  2026-08-31T10:01Z, alors que v0.10.7 et v0.10.8 sont sortis apres (12:23Z
  et 12:39Z le meme jour) sans rebuild du testeur. Ce n'est pas une erreur
  de l'utilisateur : diagnostic_xr.py:88 lit `runtime.VERSION` au moment du
  build PyInstaller de Tester-XR.exe (`build_xr_tester.py`/`xr_tester.spec`),
  qui ne sont references ni dans `deploy.py` ni dans
  `.github/workflows/*.yml` (verifie, zero occurrence) - ca redivergera a
  chaque bump tant que ce n'est pas rattache. Deux corrections possibles,
  pas exclusives : rebuild+republish Tester-XR a chaque version (mecanique
  a ajouter au pipeline), ou faire lire au testeur la version reelle du
  blink2video.exe voisin plutot que sa propre runtime.VERSION figee au
  build.

- **Latence du direct (MSE) : piste WebRTC construite, active derriere un
  drapeau, validee en usage reel (2026-09-03).** Source : meme fil reddit/cutthin que l'entree Tester-XR.exe
  ci-dessus (plainte initiale sur la lenteur du direct des cameras
  eloignees), approfondi en session le 2026-09-03 apres que l'utilisateur a
  mesure a la main Salon : 10s sur blink2video contre 3s sur l'appli
  officielle Blink. Diagnostic instrumente (horodatage reel ajoute a
  _journal_direct_mse/serve.py et au recv() patche de blink_engine.py,
  direct.log) : l'essentiel du delai n'est pas cote Blink mais dans
  blink2video meme, structurel au MSE - ffmpeg doit voir passer le SPS
  avant de pouvoir ecrire un entete MP4 (empty_moov), pas une histoire de
  reglage. Baisser -analyzeduration/-probesize (5000000 -> 1500000,
  serve.py) a ete teste sans regression sur jardin (la camera batterie
  lente a l'origine du reglage genereux), a garder.

  Prototype WebRTC (aiortc + blinkpy, venv isole, aucune modification du
  process en direct) confirme un vrai gain, mesure deux fois par camera :
  Salon 9,3s (MSE) -> 4,10s (WebRTC decodage+reencodage) -> 3,23s (WebRTC
  passthrough, sans reencodage) ; jardin ~15,8s (MSE, mesure sur Terrasse1,
  meme materiel) -> 6,81s -> 5,60s. Facteur 2,3 a 2,9x selon camera et
  mode. Le mode passthrough (equivalent WebRTC du -c:v copy deja utilise en
  MSE, evite le cout CPU du reencodage) a demande de monkey-patcher
  aiortc.codecs.CODECS["video"] pour declarer le vrai profil H.264 de la
  camera (High, 640028) : aiortc n'annonce que du Baseline en dur
  (42001f/42e01f). Verifie qu'aucune solution officielle n'existe cote
  aiortc (issue aiortc/aiortc#944, fermee sans suite ; la reserve du
  mainteneur porte sur l'encodage, pas sur le passthrough, ne s'applique
  pas a notre cas).

  Construit dans le depot (pas juste un prototype jetable) : blink_webrtc.py
  (module optionnel, DISPONIBLE=False proprement si aiortc absent), route
  POST /live-webrtc/<name> dans serve.py, watchLive()/watchWebRTC() dans
  serve_app.js avec repli automatique sur watchMse() si la negociation
  echoue. Active par la variable d'environnement BLINK_DIRECT_WEBRTC=1, pas
  encore un reglage de la page web. aiortc + cryptography/pyopenssl/cffi
  plafonnes dans requirements.in (cffi<2 : le python systeme partage de
  l'utilisateur porte aussi timezonefinder, sans rapport avec blink2video,
  qui exige cffi<2 - non pertinent si ce depot passe un jour a un venv
  dedie).

  Incident reel a l'activation (2026-09-03) : premiere tentative restee
  bloquee sur "Reconnexion...", .blink_hub.lock jamais libere (MODULE_SLOT
  pareil), plus aucun direct possible - meme MSE, verrou partage. Cause :
  RTCPeerConnection() sans configuration explicite retombe sur le defaut
  d'aiortc (stun:stun.l.google.com:19302, aiortc/codecs/rtcicetransport.py
  RTCIceGatherer.getDefaultIceServers), et setLocalDescription() attend la
  fin de la collecte de candidats avant de rendre la main - un blocage
  reseau/pare-feu dessus bloque tout indefiniment, jamais de
  connectionstatechange donc jamais de nettoyage. Corrige : iceServers=[]
  (navigateur et serveur sur le meme reseau local, un STUN n'a de toute
  facon rien a apporter) plus deux plafonds durs dans blink_webrtc.py,
  memes principes que LIVE_FIRST_FRAME_SECONDS/LIVE_MAX_SECONDS en MSE -
  NEGOCIATION_MAX_SECONDS (15s, echoue proprement plutot que de bloquer) et
  SESSION_MAX_SECONDS (300s, ferme une session jamais terminee proprement
  cote client). Revalide ensuite par l'utilisateur : fonctionne, "beaucoup
  plus rapide".

  Reste ouvert : pas de reglage dans la page web (variable d'environnement
  seulement) ; pas de reprise automatique si la connexion tombe apres la
  premiere image (contrairement a connecterMse) ; detection du profil
  H.264 verifiee sur deux modeles de camera seulement (Salon, jardin) ;
  CODECS["video"] reste un detail d'implementation non documente d'aiortc
  qu'une mise a jour peut casser sans avertissement (issue aiortc/aiortc#944
  toujours sans solution officielle cote projet). A trancher avec
  l'utilisateur : rendre ca un reglage visible, ou garder en drapeau
  experimental encore un temps.

  PyAV retire (2026-09-03), remplace par un demultiplexeur MPEG-TS/PES/NAL
  maison, blink_ts_demux.py (~200 lignes, assez de ISO/IEC 13818-1 pour
  isoler le flux elementaire H.264 : PAT -> PMT -> PID video -> decoupage en
  NAL units des qu'une fin est vue, sans attendre un paquet PES ou une
  image entiere complets). Motive par le retrait de la dependance PyAV elle
  meme, pas par un bug precis d'origine ; a permis au passage de lire le PTS
  reel encode par la camera plutot que de sonder les flux (extradata
  SPS/PPS incomplet a probesize bas, cf. l'essai analyzeduration/probesize
  abandonne plus haut). Valide hors-ligne contre une capture TS reelle
  (373 NAL units, 0 pts=None, PTS = valeur de reference PyAV a l'identique).

  Saccade signalee par l'utilisateur en usage reel (2026-09-03, "le debit
  est plus saccade") apres activation de WebRTC : deux causes distinctes
  trouvees et corrigees ensemble, aucune des deux liee a la latence de
  demarrage deja mesuree plus haut.
  1. PTS invente a la reception (horloge monotone locale) au lieu du PTS
     reel encode par la camera dans l'entete PES (ITU-T H.222.0, 2.4.3.7) :
     un reseau qui livre par rafales plutot qu'a cadence reguliere faisait
     perdre au recepteur l'information de rythme necessaire pour lisser
     l'affichage. Corrige par l'extraction PTS ci-dessus dans
     blink_ts_demux.py (deja cadencee a 90 kHz, meme horloge que WebRTC
     utilise pour la video RTP - aucune conversion requise).
  2. `<video autoplay>` seul ne demarre pas toujours la lecture d'un
     srcObject WebRTC (constate : video paused, currentTime bloque a 0,
     100% des images decodees comptees perdues par
     getVideoPlaybackQuality() jusqu'a un appel explicite video.play()).
     watchWebRTC() dans serve_app.js n'avait jamais reproduit le geste
     defensif que connecterMse() applique deja pour MSE (meme raison :
     autoplay n'est pas fiable a 100%, seuls les evenements
     loadeddata/playing prouvent qu'une image reelle est affichee).
     Corrige par le meme appel video.play().catch(() => {}) dans
     pc.ontrack.
  Valide : suite complete (407 tests) verte, harnais e2e isole
  (scratchpad, hors-depot) sur une fenetre fraiche de 4s apres le fix ->
  0 image perdue, currentTime avance normalement. Redeploye en production,
  webrtc:true confirme, verifie en reel sur Salon (paused:false,
  currentTime avance au rythme reel). Le compteur d'images perdues n'a pas
  pu etre revalide en conditions de prod via l'automatisation navigateur :
  l'onglet controle passait document.hidden=true (throttling Chrome des
  onglets non visibles), ce qui a lui seul suffit a faire compter comme
  perdue toute image decodee, independamment de la qualite reelle du flux -
  artefact de methode de test, pas un signal sur le code.

  Confirme par l'utilisateur (2026-09-03) : Salon fluide, jardin encore
  saccade mais differemment - longues periodes fluides puis saccade,
  hypothese bande passante avancee par l'utilisateur. Diagnostic instrumente
  a nouveau plutot que suppose : capture TS brute de jardin (45s) avec
  horodatage reseau reel par lecture (scratchpad/capture_ts_timing.py),
  rejouee hors-ligne a travers blink_ts_demux.py. Resultat net : le PTS
  camera reste parfaitement regulier (30 fps sans le moindre saut) sur toute
  la capture - la camera encode sans probleme - mais la livraison reseau
  montre un trou d'environ 1,0s tres regulier (6 occurrences mesurees,
  7,95s/8,95s/9,95s/10,95s/11,94s/12,95s - quasi exactement une par
  seconde), 0,62 a 0,86s sans le moindre octet recu a chaque fois. Confirme
  et precise l'hypothese bande passante de l'utilisateur : tres
  probablement le wifi de la camera a pile qui s'endort par intervalles
  pour economiser la batterie (Blink Outdoor, contrairement au Blink Mini
  cable de Salon), pas un signal faible en continu.

  Cause reelle : _PisteH264.recv() (blink_webrtc.py) renvoyait une image des
  qu'elle etait demultiplexee, zero tampon - le moindre trou reseau se
  voyait donc directement comme un arret de lecture. MSE n'a jamais ce
  probleme par construction (SourceBuffer + <video> du navigateur
  bufferisent deja plusieurs secondes d'avance par defaut), pas verifie en
  reel une deuxieme fois pour ca, pas necessaire. Corrige par un tampon de
  lecture (jitter buffer, meme principe que tout flux temps reel - RTP,
  visio) : TAMPON_LECTURE_SECONDS = 1.2, recv() cadence chaque image sur son
  PTS depuis une ancre posee a la premiere image plutot que de la renvoyer
  des son arrivee. Applique uniformement (pas seulement aux cameras a pile)
  pour rester simple ; Salon a largement la marge pour l'absorber (WebRTC y
  reste tres au-dessus de MSE malgre le tampon).

  Valide avant redeploiement (pas de nouveau reveil camera) : le VRAI
  _PisteH264.recv() rejoue contre l'enregistrement reseau reel de jardin
  (scratchpad/valide_tampon_lecture.py, FauxReader qui respecte les memes
  horodatages d'arrivee que la capture) - les 6 trous de regime etabli
  tombent tous a des ecarts normaux (~0,033s) apres le fix, seul un ecart de
  0,89s subsiste, en tout debut de session (image #2, le temps que le
  tampon se remplisse une premiere fois - inevitable, pas une resurgence du
  probleme). Suite complete (407 tests) verte. Redeploye en production,
  webrtc:true confirme. Confirme par l'utilisateur (2026-09-03) : "ca
  marche" sur jardin.

  Suite donnee par l'utilisateur (2026-09-03), deux demandes separees.

  1. Reglage webrtc/mse dans la page (webrtc par defaut), a la place de la
  variable d'environnement BLINK_DIRECT_WEBRTC. MJPEG (troisieme choix
  propose par l'utilisateur) volontairement pas ajoute : deja compare a
  MSE une fois (audit 28.15, MSE gagnant), code mort retire depuis en
  deux temps (28.53, commit 7339f85, doctrine explicite "ne pas laisser de
  code mort") - le reintroduire comme option sans raison nouvelle
  reviendrait sur cette decision. runtime.py : nouveau champ
  "live_protocol" (webrtc/mse) dans REGLAGES_DEFAUT, valide a la lecture
  (PROTOCOLES_LIVE_VALIDES, retombe sur le defaut sinon, meme pattern que
  timezone/booleens), accepte en parametre de ecrire_reglages(). serve.py :
  WEBRTC_ACTIF lit desormais runtime.lire_reglages() au demarrage au lieu
  de os.environ - meme moment de lecture que les autres reglages (redemarre
  deja au changement, comme port/fuseau/etc.), meme validation cote
  POST /api/reglages. Page web : select id=liveProtocol dans le fieldset
  Video existant, deux options, cle i18n FR/EN. Tests : 7 appels
  ecrire_reglages() dans test_runtime_reglages.py mis a jour (nouveau
  parametre requis, pas de defaut - coherent avec le reste de la
  signature), 2 nouveaux tests (valeur inconnue retombe sur webrtc, "mse"
  respecte). 407 tests -> 409.

  2. Changer de camera en direct sans cliquer Arreter ne marchait pas
  ("il faudrait forcer un arret de la camera active"). Cause confirmee :
  MODULE_SLOT (serve.py) est un Semaphore(1) global, partage par WebRTC et
  MSE, acquis en mode non bloquant - la deuxieme camera tombe donc en 409
  immediat. watchWebRTC() n'a aucune reprise (un seul essai, repli MSE) ;
  watchMse() en a deja une, specifique au 409 (MSE_DELAI_MODULE_OCCUPE_MS
  = 10000, jusqu'a MSE_BUDGET_TOTAL_MS = 10 min) - fonctionnelle mais
  jamais concue pour ce cas : elle n'aboutit que quand la premiere camera
  expire d'elle-meme (LIVE_MAX_SECONDS, jusqu'a 5 min), d'ou le "ca ne
  marche pas" en pratique. Corrige cote page web uniquement (aucun
  changement serveur necessaire) : watchLive() (serve_app.js) arrete
  desormais explicitement toute autre camera active (WEBRTC_PC/MSE_ABORT)
  avant de lancer la nouvelle - stopWatch() existait deja, seulement jamais
  appele automatiquement ici. La boucle de reprise 409 deja presente dans
  watchMse() absorbe le residu de course cote serveur (liberation pas
  encore terminee au moment ou la nouvelle requete part) sans code
  supplementaire.

  Verifie en reel (Salon actif -> bascule vers jardin sans Arreter) :
  Salon correctement arrete (revient a la vignette), jardin recoit son
  tour immediatement (flux Blink ouvert quelques secondes apres, pas
  bloque des minutes). Suite complete verte, redeploye, webrtc:true et
  reglage confirmes en page (select prerempli sur "webrtc").

  Observation separate en verifiant, non confirmee : la lecture MSE de
  jardin, apres la bascule, est repassee plusieurs fois en paused avec
  currentTime bloque a 0 malgre buffered non vide et readyState=4 (play()
  manuel la debloque un instant, puis retombe) - pattern different du bug
  play()/autoplay deja corrige cote WebRTC (connecterMse() appelle deja
  video.play()). Piste non creusee : meme reseau bursty que celui
  diagnostique et corrige cote WebRTC (jitter buffer, plus haut), mais
  MSE n'a recu aucun correctif equivalent - purement hors perimetre de
  cette demande (bascule de camera), pas cause par elle. A surveiller si
  signale a nouveau.

  Affine par l'utilisateur (2026-09-03) : "blink demande un certain temps
  entre le passage d'une camera a une autre... on pourrait utiliser le
  verrou de blink, sur le systeme occupe ?". Verifie dans le code source de
  blinkpy (site-packages, pas suppose) avant de repondre : un vrai
  mecanisme existe (LiveStreamAPI.poll(), livestream.py) - tant que la
  connexion TCP vers le relais Blink n'a pas vu EOF, il continue
  d'interroger api.request_command_status() en boucle ; seulement une fois
  EOF vu, son bloc finally appelle api.request_command_done(). _stop_stream
  (serve.py) attend deja cette tache dans son integralite avant de rendre
  MODULE_SLOT - le serveur n'ecourte donc rien. Le vrai temps d'attente est
  celui, reel, que met le relais Blink a repondre a la fermeture du flux
  cote client (mesure en reel : ~8,8 s sur la bascule Salon -> jardin de
  tout a l'heure) : ni instantane, ni infini, borne par les propres
  timeouts internes de blinkpy (COMMAND_POLL_TIME=1s, MAX_RETRY=120). Ce
  qui manquait n'etait donc pas d'attendre le bon signal (deja fait) mais
  de le voir arriver plus vite cote client : MSE_DELAI_MODULE_OCCUPE_MS
  (10 s) est un choix delibere et documente dans le code pour le cas
  generique "un tiers inconnu tient le module" (patience justifiee, ETA
  inconnue) - pas adapte au cas "je viens de declencher moi-meme cette
  liberation, elle est deja en cours".

  Premier correctif (serve_app.js uniquement, depasse par la suite - voir
  plus bas) : delai court (1000 ms) specifique au 409 rencontre juste
  apres une bascule, au lieu du delai generique de 10 s. Mesure en reel
  sur une bascule Salon -> jardin : 8,76 s -> 1,65 s. Retire ensuite
  entierement (cf. ci-dessous), la nouvelle mesure ayant montre que ce
  1,65 s n'etait qu'un echantillon chanceux, pas une valeur stable.

  Repousse par l'utilisateur (2026-09-03), deux retours factuels
  precedant la conception finale :
  1. "mon temps d'attente n'est pas de 1,65s entre salon et jardin;
     plutot 10s" - le vrai temps de liberation cote Blink est variable
     (deja 8,76 s vs 1,65 s dans les deux mesures precedentes), un delai
     fixe, meme raccourci a 1 s, ne peut que mal deviner selon les jours.
  2. "je trouve que c'est plus propre d'attendre une confirmation du
     serveur, plutot que d'essayer en force" - jugement architecture
     explicite en faveur d'un signal reel plutot qu'un delai calibre.
  Et une contrainte produit supplementaire (meme session) : "on ne doit
  pas basculer automatiquement de webrtc a mse ! il y a un reglage manuel
  pour ca" - watchLive() retombait jusque-la sur watchMse() a la moindre
  erreur WebRTC (comportement herite d'avant le reglage live_protocol,
  jamais retire depuis) ; explique aussi pourquoi une bascule atterrissait
  perceptiblement sur MSE malgre le reglage webrtc ("pourquoi MSE ? on
  n'utilise pas webrtc maintenant ?", meme session) - watchWebRTC() n'a
  qu'un seul essai, sans boucle de reprise, donc le moindre 409 pendant
  une bascule le faisait echouer puis basculer vers MSE.

  Conception finale : nouvelle route GET /api/attente-module (serve.py,
  send_attente_module) qui attend reellement MODULE_SLOT.acquire(blocking=
  True, timeout=ATTENTE_MODULE_MAX_SECONDS=25) puis le relache aussitot
  (ne le retient pas pour elle-meme - seulement une confirmation, la
  vraie tentative suit juste apres). Ne declenche aucun arret : stopWatch()
  cote page l'a deja fait avant cet appel, ce serait redondant de le
  refaire ici. watchLive() (serve_app.js) attend cette confirmation
  (attendreModuleLibre()) avant de tenter quoi que ce soit, seulement
  quand une autre camera etait active. Repli automatique WebRTC -> MSE
  retire de watchLive() : un echec WebRTC affiche desormais une vraie
  erreur (failWatch(), meme traitement que MSE) plutot que de substituer
  silencieusement l'autre protocole - coherent avec le reglage
  live_protocol, qui perdrait son sens si contourne en silence des le
  premier accroc. Supprime au passage MSE_DELAI_MODULE_OCCUPE_APRES_
  BASCULE_MS et le parametre viensDeBasculer (premier correctif,
  desormais inutile - watchMse() retrouve son delai unique d'origine,
  10 s, pour le seul cas qui lui reste : un tiers reellement inconnu).

  Verifie en reel (Salon WebRTC actif -> bascule vers jardin sans
  Arreter) : jardin atterrit desormais sur WebRTC (pas de repli MSE),
  paused:false, currentTime avance normalement des la premiere tentative.
  Delai total mesure entre la fin de session Salon et la premiere image
  video de jardin (direct.log) : 2,73 s - integre cette fois le reveil de
  la camera lui-meme, pas seulement la liberation du module (mesure pas
  directement comparable aux 8,76 s / 1,65 s precedents, qui isolaient la
  seule ouverture du flux Blink). Suite complete (409 tests) verte,
  redeploye.

  Regression signalee par l'utilisateur dans la foulee (2026-09-03) : "a
  un moment, les 2 boutons sont a voir le direct, alors que celui de la
  2eme camera aurait du changer de suite". Introduite par le changement
  ci-dessus : watchWebRTC()/watchMse() ne touchent la case qu'apres
  attendreModuleLibre(), qui peut prendre plusieurs secondes - la case de
  la nouvelle camera restait donc sur "Voir en direct" tout ce temps,
  comme si le clic n'avait rien declenche. Corrige (watchLive(),
  serve_app.js) : la case affiche l'indice d'attente (repos() avec le
  libelle "watch.waking") immediatement, avant meme d'arreter l'autre
  camera ou d'attendre - meme texte que celui affiche juste apres par
  watchWebRTC()/watchMse(), pas de changement visible au moment de la
  relve. Verifie en reel : le bouton passe de "Voir en direct" a "Reveil
  de la camera..." de facon synchrone des le clic (avant tout await),
  pendant que l'ancienne camera revient a l'etat repos au meme instant.
  Suite complete verte, redeploye.

  "ca bloque sur le liveview jardin" (2026-09-03, apres publication de
  v0.11.0). Direct.log : "echec (webrtc), TimeoutError:" suivi 24s plus
  tard de "session rendue de force". Cause reelle, verifiee dans
  blink_webrtc.py (pas supposee) : l'attente du SPS/PPS envoye par la
  camera (track.sps_pps_pret.wait(), le vrai reveil materiel) partageait
  NEGOCIATION_MAX_SECONDS (15s) avec les etapes de negociation SDP/DTLS
  purement locales - alors que MSE accorde deja 40s a cette meme attente
  (LIVE_FIRST_FRAME_SECONDS, serve.py) precisement parce qu'une camera a
  pile doit se reveiller. 15s suffisait le plus souvent (d'ou tous les
  succes vus dans ce meme direct.log), pas toujours. Corrige : nouvelle
  constante PREMIERE_IMAGE_MAX_SECONDS = 40 (meme valeur et raison que
  cote MSE), dediee a cette seule attente ; NEGOCIATION_MAX_SECONDS reste
  a 15s pour la suite (SDP/DTLS, purement locale, aucune raison d'etre
  aussi patiente).

  Message d'erreur corrige au passage : un TimeoutError sans reponse
  serveur avant plusieurs dizaines de secondes (negociation + jusqu'a 45s
  de nettoyage cote send_offer_webrtc) s'affichait ensuite comme
  "TimeoutError: " brut sur le bouton Reessayer, sans explication - meme
  qualite de message que MSE desormais ("La camera n'a envoye aucune
  image. Hors de portee du module, endormie, ou deja occupee par une
  autre session.").

  Confusion separee dans la foulee : "pourquoi mse??? on est sur
  webrtc!" - le prefixe de journal "[direct-mse]" (nom herite de
  l'epoque ou seul MSE existait) apparaissait sur CHAQUE ligne de
  direct.log, y compris les echecs WebRTC ci-dessus, laissant croire a
  une bascule silencieuse. Aucune bascule reelle (deja retiree, plus
  haut) : juste un nom de prefixe reste generique par accident.
  _journal_direct_mse() renommee _journal_direct() (serve.py, 12 sites
  d'appel) ; meme prefixe "[direct]" reproduit dans blink_engine.py (son
  propre point de journalisation, hors de portee de la fonction ci-dessus
  pour eviter un import circulaire). Le protocole reste lisible dans le
  texte de chaque message, jamais dans ce prefixe desormais neutre.

  Suite complete (409 tests) verte, redeploye.

  Toujours la, reconfirme par l'utilisateur (2026-09-03) : "ca bloque
  toujours lors du passage salon a jardin; il faut attendre un moment".
  Direct.log de cette nouvelle occurrence (pas suppose) : Salon arrete a
  14:30:17.825, jardin echoue a 14:30:59.126 (41,3s - pile
  PREMIERE_IMAGE_MAX_SECONDES=40, le nouveau plafond deja atteint), une
  nouvelle tentative reussit a 14:31:41 (42s plus tard) - ~84s ressenties
  au total pour cette seule bascule. Le plafond de 40s (juste corrige,
  voir plus haut) n'etait donc pas mal calibre : jardin peut reellement
  prendre plus de temps que ca a repondre, tout particulierement juste
  apres une bascule depuis une autre camera (probable temps de reattache
  materiel du module de synchronisation a une camera differente - non
  verifiable depuis blink2video, hors de sa portee). Le vrai manque
  restant : un seul essai de negociation WebRTC, sans reprise - MSE a
  deja une boucle pour exactement ce cas depuis le debut
  (MSE_MAX_ECHECS_A_VIDE), WebRTC ne l'avait jamais eue ("pas encore de
  boucle de reprise pour ce chemin tout neuf, un nouveau clic suffit" -
  ecrit a la construction initiale, jamais revisite jusqu'ici).

  Corrige : boucle de reprise pour la negociation initiale
  (watchWebRTC()/tenterWebRTC(), serve_app.js), symetrique a celle de
  MSE - memes valeurs que les constantes MSE_* (WEBRTC_MAX_ECHECS=5,
  WEBRTC_DELAI_RECONNEXION_MS=3000, WEBRTC_BUDGET_TOTAL_MS=10 min),
  meme distinction compteur de secondes uniquement au tout premier essai
  vs texte fixe "Reconnexion..." ensuite. Necessite une annulation
  propre pour un essai en cours ET pour l'attente entre deux essais :
  nouveau WEBRTC_ABORT (parallele a MSE_ABORT), verifie par stopWatch()
  et par watchLive() (une camera "active" au sens de la bascule inclut
  desormais une reprise en cours, meme sans RTCPeerConnection etablie
  pour l'instant). Repli automatique vers MSE toujours absent (inchange,
  demande explicite de l'utilisateur) : ceci reprend seulement au sein
  du protocole choisi, jamais vers l'autre. Porte volontairement limitee
  a la negociation initiale, comme documente des la premiere version :
  une coupure apres la premiere image reste, elle, non reprise
  automatiquement (portee differente).

  Valide en reel avant redeploiement :
  - chemin normal (Salon) inchange, succes du premier coup ;
  - boucle de reprise verifiee sur Portail (camera reellement hors
    ligne, echec rapide et repetable, contrairement a jardin) : indice
    "Reconnexion..." visible entre les essais, WEBRTC_ABORT peuple
    pendant la sequence, message d'erreur clair apres epuisement des 5
    essais, meme qualite que le message KeyError deja existant
    ("Blink n'a fourni aucune adresse de flux...") ;
  - annulation en cours de reprise (bouton Arreter pendant l'attente
    entre deux essais) : case revient proprement au repos, WEBRTC_ABORT/
    WEBRTC_PC/MSE_ABORT tous vides ensuite, aucun etat residuel.
  Suite complete verte, redeploye.

- **"Interrogation du systeme Blink..." (mode Direct) plus lent que
  necessaire (2026-09-03).** Question de l'utilisateur : une seule
  requete, ou plusieurs qu'on pourrait paralleliser/differer ? Verifie
  dans le code source de blinkpy avant de repondre (pas suppose) :
  system_state() (serve.py) appelait _blink.refresh(force=True), qui
  enchaine en serie get_homescreen() + par module get_network_info() +
  update_local_storage_manifest() + check_new_videos() + par camera
  get_camera_info()+update() (sync_module.py) - soit 1+1+1+1+N appels
  reseau successifs pour ce compte (N=4 cameras ici). Or l'affichage de
  cette page ne lit en realite que get_homescreen() (nom/batterie/
  temperature/statut par camera) et network_info par module (armement) :
  le reste (manifeste de stockage, nouveaux clips, detail par camera)
  n'est jamais lu par cette route. sync.cameras lui-meme (identifiants
  device_id/network_id, utilises pour rapprocher chaque camera de son
  entree dans l'ecran d'accueil) est peuple une seule fois a la connexion
  initiale (update_cameras(), appele par start(), jamais par refresh()) -
  deja stable, pas besoin d'un nouvel appel pour ca non plus.

  Pas de rendu "rapide puis enrichi" necessaire au final : juste retirer
  ce qui ne sert a rien pour cette page precise. system_state() appelle
  desormais get_homescreen() + get_network_info() par module seulement,
  au lieu de refresh(force=True) complet. Meme donnees affichees,
  verifie champ par champ en reel (armement, batterie, temperature,
  hors-ligne, y compris Salon en null - Blink Mini, normal, deja le cas
  avant). Mesure reelle avant/apres, meme session, dos a dos, dans un
  script isole (scratchpad/comparer_system_state.py, pas suppose) :
  nouvelle sequence 3,34s contre 7,42s pour l'ancienne (2,2x). Chiffre
  exact variable d'un appel a l'autre (l'API Blink elle-meme varie,
  constate plusieurs fois cette session) mais l'ecart structurel (2
  appels reseau au lieu de 8) est solide. Suite complete verte,
  redeploye.

  Regression introduite par ce meme allegement, trouvee par un audit
  general demande par l'utilisateur juste apres (2026-09-03), pas par
  l'utilisateur lui-meme : describe_camera() (serve.py) lit battery/
  battery_signal/voltage/temperature/wifi/firmware/kind/model depuis
  camera.attributes (objet camera de blinkpy), pas depuis raw/info
  (ecran d'accueil). attributes n'est mis a jour que par camera.update()
  (extract_config_info(), camera.py), appele seulement par
  sync_module.refresh() (une fois par camera, precisement ce que
  l'allegement retire de system_state()) ou par update_cameras() au tout
  premier demarrage. Consequence reelle : ces champs se figeaient
  silencieusement a la valeur du demarrage du serveur, plus jamais
  rafraichis ensuite - invisible sur le moment (le serveur venait de
  redemarrer, tout etait encore frais), ne se serait vu qu'apres des
  heures ou des jours d'activite, sans jamais se corriger seul. armed/
  battery_signal/lfr n'etaient eux pas touches (deja lus depuis raw/
  signals, verifie plus haut).

  Corrige en verifiant d'abord (pas suppose) un ecran d'accueil reel
  (scratchpad/inspecter_homescreen.py) : battery, fw_version, type et
  signals.{wifi,temp,battery,lfr} y sont deja tous presents, per-camera,
  sans aucun appel supplementaire. describe_camera() lit desormais ces
  champs depuis info/signals, comme armed/battery_signal/lfr deja avant.
  temperature recalculee depuis signals.temp (Fahrenheit brut, comme
  Blink le rapporte) avec exactement la meme formule que camera.
  temperature_c (blinkpy, camera.py) : round((f-32)/9*5, 1). Seul voltage
  reste lu depuis attributes (donc perime apres le demarrage) : aucun
  equivalent dans l'ecran d'accueil, et verifie non affiche cote page
  (aucune occurrence dans serve_app.js) - perime sans consequence
  visible, pas la peine d'y consacrer un appel reseau dedie.

  Valide en reel : donnees correctes et fraiches par camera (Terrasse1
  49,4 degC, Portail 28,3 degC - distinctes, pas une valeur figee
  dupliquee -, coherentes avec l'ecran d'accueil brut inspecte juste
  avant). Salon (Blink Mini) toujours a null pour battery/temp/wifi,
  normal, deja le cas avant. Suite complete verte, redeploye.

  Audit general par ailleurs : deux autres pistes remontees, pas encore
  traitees, a trancher avec l'utilisateur.
  - collect_videos() (serve.py) reprobe la duree de chaque video
    assemblee a chaque appel de /api/videos (probe_duration() brut, non
    mis en cache), alors que le meme fichier a deja le mecanisme qu'il
    faudrait juste reutiliser : probe_duration_cached() (empreinte
    taille+mtime), deja utilise pour les clips ecartes juste a cote, et
    merge_daily.py a le meme motif pour les clips source. Gain probable,
    risque faible, motif deja eprouve ailleurs dans ce depot.
  - _telecharger_cloud() (blink_engine.py) telecharge les clips du cloud
    strictement en sequence, un await complet avant le suivant. Gain de
    temps total plausible sur un gros retard a rattraper, mais rien
    n'indique que ce soit reellement ressenti comme lent (contrairement
    a /api/system, jamais signale), et rendre ca concurrent demande de
    revoir la progression SSE et l'ecriture du registre pour rester
    correct a plusieurs telechargements en vol - a decider deliberement,
    pas un gain evident au meme titre que le premier point.

## Revue de code du 2026-08-20 (commit 0eab463)

Les onze bugs numerotes de la revue sont tous traites (28.59 a 28.68) :
Dockerfile CMD start, autostart.py quoi manquant, CSRF/Origin, verrou
disque a double proprietaire, validation MP4/adoption, trois copies
divergentes de safe_name, plafond cloud silencieux, course a la
sauvegarde de session, reglages JSON mal types, surveillance watch.py
(batterie au premier passage + clips ecartes). Restent deux points
reformules ci-dessous, volontairement pas ceux d'origine - chacun touche
une decision de conception plutot qu'un simple oubli, a trancher avec
l'utilisateur - et le lot d'optimisations, non urgentes.

- **Journalieres/hebdo/mensuelles sans distinction si deux cameras se
  nettoient pareil.** (Reformule apres 28.64 : la derive entre les trois
  copies de safe_name est fermee, ceci est ce qui restait reellement.)
  merge_daily.py, etapes de regroupement : `safe_name(camera)` sert de nom
  de dossier ET de fichier sans aucun suffixe distinctif, contrairement a
  `target_path()` (clips bruts) qui en a un. Deux cameras dont le nom brut
  differe mais se nettoie pareil ("Garage" / "Garage!") verraient leurs
  videos assemblees atterrir au meme endroit. Rare en pratique (exige une
  coincidence de nommage precise), et changer le nommage des dossiers de
  sortie toucherait des installations existantes deja organisees autour de
  `Blink_Daily/<camera>/...` - decision a prendre avec l'utilisateur, pas
  a trancher seule.

- **Silence d'une camera n'ayant jamais enregistre.**
  (Reformule apres 28.68 : les deux autres defauts de watch.py sont
  fermes, ceci est ce qui restait reellement.) Le controle de silence
  prolonge (`compare()`) ne visite que les cameras presentes dans
  `last_clip` (au moins un clip, meme ecarte, un jour) - une camera qui
  n'a jamais rien enregistre depuis son installation n'y entre jamais,
  quelle que soit la duree. Fermer ce cas demande un point d'ancrage
  temporel qui n'existe pas encore (depuis quand cette camera n'a-t-elle
  rien enregistre ?) : un suivi d'etat a ajouter a WATCH_STATE (horodater
  la premiere observation d'une camera sans historique), pas un correctif
  d'une ligne - a concevoir plutot qu'a improviser au milieu d'une serie
  de corrections.

- **Optimisations identifiees (pas des bugs, pas urgentes).**
  Registre reecrit en entier a chaque clip (quadratique sur un lot) ;
  known_identities() reparcourt tout a chaque vignette (cache par mtime
  utile) ; telechargements cloud charges entierement en memoire
  (response.read(), a chunker) ; cache _DURATIONS qui ne purge jamais ses
  anciennes cles ; dependances non epinglees alors que le code touche des
  attributs prives de blinkpy ; bootstrap qui reutilise l'environnement
  global au lieu de rester isole ; plusieurs helpers JSON divergents,
  a centraliser (meme esprit que safe_name, deja fait en 28.64).
