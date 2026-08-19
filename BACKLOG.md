# Backlog

Demandes venues de Reddit (ou d'ailleurs) pas encore traitees. But : ne pas
les perdre, pas forcement les construire toutes.

- **Desactiver le telechargement automatique** (garder juste le direct).
  Source : reddit/daxxruckus, r/blinkcameras, 2026-08-19. Aujourd'hui aucun
  bouton pour ca : `standard()` (runtime.py) cable toujours `download`
  USB+cloud avec `serve`. Contournement donne : lancer `serve` seul plutot
  que `start`/autostart. Candidat naturel : une coche dans le panneau de
  reglages, meme esprit que `merge_jour`.

- **Integration Home Assistant native.**
  Source : reddit/MoneySquare6212, r/blinkcameras, 2026-08-19. Ecarte pour
  l'instant (repondu sur Reddit) : un vrai chantier a part (config flow,
  modele d'entites, HACS), pas une extension de ce qui existe. Note ici
  pour ne pas l'oublier si la demande revient.
