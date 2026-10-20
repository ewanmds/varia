# Mode tendance du ratio WTI / Brent

Le ratio est **USOILP / UKOILP**. Une tendance haussière confirmée sélectionne
**B : long USOILP, short UKOILP** ; une tendance baissière sélectionne
**A : short USOILP, long UKOILP**. Ce signal suit une tendance passée ; il ne
garantit pas sa continuation ni un profit.

Le mode `trend_hold` contrôle le signal chaque minute. Un cycle volume séparé
de 5 minutes reste actif (`volume_cycle_min = 5`, 0 pour désactiver).
À échéance, la paire est fermée puis rouverte, ou retournée, dans le sens
choisi avec les données disponibles à cet instant.
Le volume est d'environ 4 000 $ par cycle avec 1 000 $ par jambe.
Entre ces échéances, la paire est conservée sans ordre.
Une paire existante éloignée de la taille cible est ajustée directement,
sans fermeture/réouverture préalable, après le filtre de coût.
Avec `volume_cycle_required = true`, un signal faible n'empêche pas l'entrée
volume dès que l'historique requis est disponible, ni le cycle de 5 minutes.
La direction volume suit, dans l'ordre : le contexte 30 minutes confirmé,
le signal court confirmé, puis le signe de la pente courte actuelle.
Le sens de la position existante, puis le sens précédent, ne servent qu'en
l'absence de direction mesurable ; B est le dernier choix au premier démarrage.
Une ancienne position short ne prend donc plus priorité sur une pente haussière.
Si une paire existe, elle est conservée jusqu'à
un retournement confirmé, un cycle volume ou une fermeture par les garde-fous. Le filtre de coût
peut aussi différer un retournement : la paire précédente reste alors ouverte.

## Paramètres actuels

- 1 000 $ par jambe ; tranches de 500 $ ; contrôle toutes les minutes.
- Taille réglable de 20 à 10 000 $ par jambe, sans obligation de multiple de 500.
  Avec la tranche configurée à 500 $, une jambe de 20 $ ouvre un ordre de 20 $,
  750 $ ouvre 500 + 250 $, et 1 000 $ conserve deux ordres de 500 $.
  Les jambes restent alternées ; les seuils d'équilibrage s'adaptent aux petites tailles.
- Après un statut final de rejet, le bot vérifie les positions, attend au moins
  une seconde, puis demande une nouvelle cotation pour sa seconde et dernière
  tentative. Une position modifiée ou impossible à vérifier interdit le renvoi
  automatique de la tranche. Un rejet persistant met toujours le bot en pause,
  avec le motif fourni par Variational lorsqu'il est disponible ; la paire
  déjà exécutée peut rester ouverte si elle est équilibrée.
- Source active : bougies **1 minute TradingView TVC:USOIL et TVC:UKOIL**.
- Ratio des clôtures portant exactement le même horodatage ; la bougie en cours est exclue.
- Une bougie doit déjà être close au début de la requête TradingView pour
  les deux jambes. Une valeur partielle reçue avant la clôture ne devient
  pas utilisable par simple passage du temps : une nouvelle lecture est requise.
- Régression sur les **5 dernières bougies closes**, confirmation sur les 2 dernières minutes.
- Contexte sur les **30 dernières bougies closes** (`trend_context_min = 30`,
  0 pour désactiver). Sa pente, son R² et son mouvement sont évalués
  indépendamment de la confirmation courte. Les seuils sont ceux du signal court.
  Un contexte haussier confirmé bloque un short sur un simple repli local ;
  le mécanisme est symétrique en contexte baissier. Aux cycles volume, ce
  contexte détermine le sens même si la confirmation courte est absente.
  Si le contexte est faible, le signal court reprend la main.
- Seules les minutes closes sont utilisées ; historique discontinu refusé.
- Mouvement ajusté sur 5 min d'au moins 3 pb ; R² d'au moins 0,4.
- Aucun sens forcé au démarrage : disposer de 30 bougies closes continues
  avant toute entrée. `warmup_entry_side` reste absent de la configuration.
  L'historique récent chargé au démarrage peut remplir immédiatement cette fenêtre.
  Un historique de contexte incomplet ou discontinu bloque aussi les cycles volume.
- Avant disponibilité du signal, aucune nouvelle paire n'est ouverte. Une paire
  existante reste surveillée par les garde-fous.
- Avant entrée/retournement hors cycle obligatoire, lecture de deux cotations RFQ de taille 500 $.
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
La décision est relue après les attentes de lecture des positions et des
cotations. Si les bougies publiées entre-temps changent le sens choisi, le
cycle est différé et recalculé. Un arrêt, une fermeture de séance ou un flux
périmé pendant ces attentes bloque aussi une nouvelle entrée.
La fermeture de séance, la garde de liquidation, les limites de perte, la
réconciliation et l'arrêt restent prioritaires : le filtre de coût ne bloque
jamais les sorties de sécurité. Les autres actifs et les tailles autorisées
ne sont pas modifiés.

La cadence volume, les tailles, les tranches et les limites de risque restent
identiques. Les contrôles intermédiaires peuvent toujours retourner une paire
sur signal confirmé après le filtre de coût ; le nombre réel d'ordres dépend
donc aussi des retournements, des gardes et de la disponibilité des données.
Le contexte peut retarder la réaction à un véritable retournement : 30 minutes
est un choix de recherche, sans garantie d'amélioration du PnL.

Le CPM affiché vaut `-(PnL réalisé + latent) / volume exécuté × 1 000 000`.
Il mesure une perte nette de marché par volume, pas seulement les frais.
Un CPM de 0 correspond au point mort et un CPM négatif à un bénéfice. L'absence
de volume donne une valeur indisponible, jamais un CPM artificiellement nul.

## Exécution et mesure des coûts

Lors d'un retournement ou recyclage, les deux jambes sont entièrement fermées
en reduce-only avant la première réouverture. Les tranches alternent à
l'intérieur de chaque phase. Cette séparation évite de rouvrir une jambe
alors que l'autre conserve son ancien sens lorsque les positions adoptées
exigent des nombres de tranches différents. Elle ne change pas le nombre
d'ordres du plan. Elle ne garantit pas une borne stricte par tranche pour
une position adoptée dont la taille réelle diffère du plan arrondi.

Le journal conserve la provenance d'exécution dans
`pair_execution_evidence`, liée à la ligne `pair_fill` par `arb_id`, dans la
même transaction SQLite. Il stocke le mid et le prix RFQ, le demi-spread,
le slippage et le statut reduce-only. Un prix reçu dans le statut d'ordre
est marqué `order_status` ; un prix seulement estimé depuis la cotation
est marqué `quote_proxy`, avec prix exécuté et slippage confirmés laissés
indisponibles. La quantité reste `requested_unverified` tant qu'elle n'est
pas indépendamment confirmée. Aucun identifiant de compte ou jeton n'est
ajouté. Cette mesure réutilise les cotations existantes, sans requête supplémentaire.

La comptabilité historique conserve ses estimations pour compatibilité.
Les anciennes lignes n'ont pas cette provenance : leurs volumes et PnL
ne permettent donc pas, à eux seuls, de certifier le CPM réellement payé.
Les exports officiels de trades et de transferts `realized_pnl` servent
à vérifier le volume et la perte nette réalisés pour une même période.

## Vérification et comparaison

Les tests couvrent le sens, les replis opposés au contexte dans les deux sens,
le retournement durable du contexte, les minutes closes, les trous
d'historique, le préchauffage, le recyclage à 299/300 secondes, le filtre de
coût et les sorties de sécurité. Les scénarios synthétiques vérifient le
comportement ; ils ne prouvent pas une performance sur le pétrole réel.

Aucun historique continu des prix et spreads **exécutables Variational** n'est
présent dans ce dossier. Les fills du journal sont trop espacés pour reconstruire
le signal. Un historique public TradingView peut servir de proxy de recherche,
avec un modèle de coût explicite ; il ne valide pas le CPM réalisable sur Var.
L'amélioration du profit réel et l'objectif de 0 $/M restent à vérifier.

Pour comparer offline le signal sur un CSV de prix synchronisés et une seule
séance continue (colonnes `ts,wti,brent`, ts en secondes Unix) :

```powershell
.\.venv\Scripts\python.exe tools\pair_trend_replay.py prix.csv --half-spread-bps 0.3 --slip-bps 0.2
```

Remplacer ces hypothèses de coût par des valeurs réellement mesurées. Ce replay
exécute les décisions au point suivant, tient compte du cycle volume et de
l'horizon `trend_projection_min`, compare les directions et les coûts,
mais ne simule pas les garde-fous, refus RFQ, funding, marge ou remboursements.
Il ne suffit pas à lui seul pour valider une stratégie live.

Pour isoler le choix de direction à nombre d'ordres comparable, utiliser
`--fixed-cadence`. Cette comparaison oppose l'ancien choix de sens sans
contexte au nouveau, après un préchauffage commun, sur une horloge de décision
fixe de 5 minutes. Elle exclut les retournements intermédiaires du moteur réel
et affiche si le nombre d'ordres et de cycles est effectivement égal.

Avec `--signal-csv bougies.csv`, les signaux viennent de bougies indépendantes
horodatées à leur **ouverture**, utilisables seulement 60 secondes plus tard.
Le CSV principal doit dater les prix à leur **instant de disponibilité**.
Pour un proxy constitué de clôtures TradingView, décaler donc les timestamps
d'exécution de +60 secondes. Chaque décision est exécutée à l'observation
suivante : des observations espacées d'une minute ajoutent 60 secondes de
latence. En replay complet, l'horloge depuis la dernière exécution produit
ainsi des cycles de 6 minutes ; la comparaison fixe isole la direction avec
des exécutions espacées de 5 minutes. Ni l'une ni l'autre ne reproduit les
délais réels entre tranches ou des cotations exécutables Variational.

Exemple de comparaison, avec deux CSV préparés selon ces conventions :

```powershell
.\.venv\Scripts\python.exe tools\pair_trend_replay.py observations.csv --signal-csv bougies.csv --fixed-cadence --half-spread-bps 0.3 --slip-bps 0.2
```

L'outil optionnel `tools/pair_forward_paper.py` compare les deux politiques
sur les données publiques reçues prospectivement. Il conserve l'instant de
publication TradingView et attend de nouvelles observations des deux prix
Var après la décision. Les fills sont locaux dans `PaperPairVar`, avec
demi-spread et glissement fournis comme hypothèses explicites. Il enregistre
les compteurs réels de cette simulation et leur égalité, sans la présumer.
Il termine après la durée demandée, ou si les données deviennent indisponibles.
Cet outil ne lance pas de serveur et n'utilise pas de session de compte.

```powershell
.\.venv\Scripts\python.exe tools\pair_forward_paper.py --minutes 15 --half-spread-bps 0.3 --slip-bps 0.2 --out data\research\forward-paper
```

Ce lancement reste manuel. Un prix public Var et un coût supposé ne permettent
pas de certifier le coût d'exécution Variational. Les fichiers `status.json`
et `summary.json` indiquent le modèle de coût et toute fermeture simulée
incomplète, sans inventer de prix quand le flux est périmé.

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
