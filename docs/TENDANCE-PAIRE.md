# Mode tendance du ratio WTI / Brent

Le ratio est **USOILP / UKOILP**. Une tendance haussière confirmée sélectionne
**B : long USOILP, short UKOILP** ; une tendance baissière sélectionne
**A : short USOILP, long UKOILP**. Ce signal suit une tendance passée ; il ne
garantit pas sa continuation ni un profit.

Le mode `trend_hold` remplace le recyclage systématique du mode volume.
Le suivi est contrôlé chaque minute. Un cycle volume séparé de 5 minutes est
également actif (`volume_cycle_min = 5`, 0 pour désactiver).
À échéance, la paire est fermée puis rouverte dans le même sens si aucune
direction inverse n'est confirmée. Un retournement remplace le recyclage.
Le volume est d'environ 4 000 $ par cycle avec 1 000 $ par jambe.
Entre ces échéances, la paire est conservée sans ordre.
Une paire existante éloignée de la taille cible est ajustée directement,
sans fermeture/réouverture préalable, après le filtre de coût.
Avec `volume_cycle_required = true`, un signal faible n'empêche plus l'entrée
volume dès que les 5 bougies sont disponibles, ni le cycle de 5 minutes.
Sans position, la pente positive sélectionne B, négative A même sans confirmation ;
si elle est nulle, le sens précédent est repris, ou B au premier démarrage.
Avec une position, son sens reste conservé faute de retournement confirmé.
Si une paire existe, elle est conservée jusqu'à
un retournement confirmé, un cycle volume ou une fermeture par les garde-fous. Le filtre de coût
peut aussi différer un retournement : la paire précédente reste alors ouverte.

## Paramètres actuels

- 1 000 $ par jambe ; tranches de 500 $ ; contrôle toutes les minutes.
- Source active : bougies **1 minute TradingView TVC:USOIL et TVC:UKOIL**.
- Ratio des clôtures portant exactement le même horodatage ; la bougie en cours est exclue.
- Régression sur les **5 dernières bougies closes**, confirmation sur les 2 dernières minutes.
- Seules les minutes closes sont utilisées ; historique discontinu refusé.
- Mouvement ajusté sur 5 min d'au moins 3 pb ; R² d'au moins 0,4.
- Aucun sens forcé au démarrage : disposer de 5 bougies closes continues
  avant toute entrée. `warmup_entry_side` reste absent de la configuration.
  L'historique récent chargé au démarrage peut remplir immédiatement cette fenêtre.
- Avant disponibilité du signal, aucune nouvelle paire n'est ouverte. Une paire
  existante reste surveillée par les garde-fous.
- Avant entrée/retournement, lecture de deux cotations RFQ de taille 500 $.
  Coût estimé : somme des deux spreads complets + réserve de 2 pb.
  La pente extrapolée sur les 10 minutes suivantes (`trend_projection_min`,
  indépendant de la fréquence de contrôle) doit dépasser 1,5 fois
  ce coût. Cette extrapolation n'est pas une estimation probabiliste de gain.

Les cotations d'exécution restent fraîches et indépendantes de celles du filtre.
Les cycles obligatoires ne sont bloqués ni par la pente prévue ni par le budget
CPM estimé de 80 $/M. Cotations RFQ fraîches, glissement maximum et gardes de
risque restent exigés. Sans ce mode obligatoire, le budget CPM peut différer
le recyclage ; les retournements anticipés gardent leur filtre de coût.
Les prix Variational servent toujours à l'exécution, au PnL et aux gardes de risque.
Le flux TradingView est lu par le protocole public des graphiques, sans compte,
en lecture seule : il ne s'agit pas d'une API officiellement prise en charge.
Un seul symbole est demandé à la fois ; le ratio est calculé localement.
Un refus d'accès arrête le flux. Une source indisponible, une bougie absente ou
des données anciennes bloquent toute entrée/retournement, sans repli vers Var.
Le tableau affiche le ratio TradingView de la dernière minute close et l'état du flux.
La fermeture de séance, la garde de liquidation, les limites de perte, la
réconciliation et l'arrêt restent prioritaires : le filtre de coût ne bloque
jamais les sorties de sécurité. Les autres actifs et les tailles autorisées
ne sont pas modifiés.

Le volume sera inférieur à celui du mode farming. Réduire les ordres inutiles
diminue les coûts de transaction en dollars, mais ne garantit pas un meilleur
PnL ou coût réalisé par million. L'indicateur CPM inclut le PnL de marché.

## Vérification et comparaison

Les tests couvrent le sens, la confirmation, les minutes closes, les trous
d'historique, l'absence de recyclage, le filtre de coût, le retournement et
les sorties de sécurité. Les scénarios synthétiques vérifient le comportement ;
ils ne prouvent pas une performance sur le pétrole réel.

Aucun historique continu de prix/spreads exploitable n'était présent dans ce
dossier. Les fills du journal sont trop espacés pour reconstruire un signal
sur 30 minutes. L'amélioration du profit et du CPM reste donc non validée.

Pour comparer offline le signal sur un CSV de prix synchronisés et une seule
séance continue (colonnes `ts,wti,brent`, ts en secondes Unix) :

```powershell
.\.venv\Scripts\python.exe tools\pair_trend_replay.py prix.csv --half-spread-bps 0.3 --slip-bps 0.2
```

Remplacer ces hypothèses de coût par des valeurs réellement mesurées. Ce replay
exécute les décisions au point suivant, compare les directions et les coûts,
mais ne simule pas les garde-fous, refus RFQ, funding, marge ou remboursements.
Il ne suffit pas à lui seul pour valider une stratégie live.

Le serveur charge la nouvelle configuration au prochain lancement. Arrêter
le bot et attendre les positions à plat avant de relancer `start-real.cmd`.
Pour retrouver le farming initial : `signal_mode = "mean_reversion"`.

Le lancement explicite `main.py --port 8792 --start-pair` démarre la paire dès
connexion de l'extension. Le port est réservé avant ce démarrage pour éviter
deux instances concurrentes sur le même port. Le lanceur normal conserve le
bouton Démarrer et n'envoie pas d'ordres au simple lancement du serveur.

Une entrée ponctuelle ne s'utilise qu'après une instruction explicite de l'opérateur :
`main.py --port 8792 --start-pair --pair-entry-side B` ouvre long WTI / short Brent,
avec les tailles configurées, sans exiger le signal ou son filtre de coût prévisionnel.
La demande est consommée une seule fois ; ensuite, les contrôles suivent TradingView.
Les horaires, limites de perte, contrôles de positions, cotations d'exécution et
slippage maximum s'appliquent toujours. Le lanceur habituel n'utilise pas cette option.
