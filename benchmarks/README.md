# Benchmarks de l'audit

`stage0_baseline.py` mesure les scénarios P-01 à P-12 définis dans
`AUDIT-2026-08-13.md`. Il n'utilise jamais le compte Blink ni Internet : les
entrées distantes sont simulées et le média P-07 est servi sur loopback.

## Campagne rapide reproductible

```powershell
python -B benchmarks/stage0_baseline.py --profile quick --label run-a
python -B benchmarks/stage0_baseline.py --profile quick --label run-b
python -B benchmarks/stage0_baseline.py --compare `
  benchmarks/results/<premiere-campagne>.json `
  benchmarks/results/<seconde-campagne>.json `
  --compare-mode stability
```

Le profil rapide respecte trois chauffes, vingt répétitions pour les mesures
courtes et cinq pour les scénarios lourds. Ses tailles réduites sont inscrites
dans chaque résultat ; elles ne doivent pas être présentées comme les volumes
du profil complet.

P-01 crée exactement 120 processus dans ce profil : cinq commandes × trois
chauffes, vingt mesures et un contrôle sémantique. Une pause de 250 ms, exclue
du chronométrage, sépare chaque création afin de laisser refroidir l'antivirus.

Si l'antivirus analyse chaque nouveau processus au point de saturer la machine,
le profil `safe` permet uniquement un diagnostic préliminaire :

```powershell
python -B benchmarks/stage0_baseline.py --profile safe --label diagnostic
```

Il limite P-01 à deux échantillons sans chauffe et réduit les autres répétitions.
Son JSON porte le type `preliminary-baseline` : il ne permet ni de fermer
l'étape 0 ni de revendiquer une amélioration. Il ne faut pas désactiver
l'antivirus pour obtenir un meilleur chiffre.

## Profil complet

```powershell
python -B benchmarks/stage0_baseline.py --profile full --label reference
```

Ce profil inclut 100 000 entrées et un flux synthétique de 1 Gio. Il faut
contrôler l'espace disque, la mémoire disponible et le mode d'alimentation
avant de le lancer. Le profil rapide est celui utilisé sur la machine auditée.

## Lecture des résultats

Chaque campagne produit :

- un JSON brut avec tous les échantillons, médiane, p95, dispersion et
  intervalle de confiance bootstrap à 95 % ;
- un résumé Markdown ;
- le commit, les empreintes des fichiers de production, l'OS, Python, les
  dépendances, le processeur, la mémoire et le mode d'alimentation.

Le nom contient un horodatage UTC précis et les fichiers sont ouverts en mode
exclusif : une campagne existante n'est jamais écrasée. La révision de référence
du rapport et le HEAD réellement exécuté sont enregistrés séparément.

Le comparateur refuse les schémas, profils, protocoles ou listes de scénarios
incompatibles. Le mode `stability` contrôle la reproductibilité ; le mode
`candidate` exige le gain minimal passé avec `--min-improvement-percent`.

Les modes sont explicites : `real_subprocess`, `real_json_io`,
`real_loopback_http`, `real_multiprocess_forced_interleaving`, ou orchestration
réelle avec entrées simulées. Une mesure fonctionnellement incorrecte reste
dans la baseline mais ne peut servir à revendiquer un gain.

P-12 demeure `partial` tant que le préflight, la page d'authentification et le
mini-smoke de l'étape 5 n'existent pas. Ces valeurs sont `unavailable`, jamais
égales à zéro.
