# V1 — Bot paire pétrole Variational

Le mode actuellement configuré trade la paire USOILP / UKOILP sur Variational.
Le dépôt contient le code et les paramètres du bot local, ses tests et son
extension Chrome. Les sessions, clés, jetons et journaux de compte ne sont pas publiés.

**Installation Windows / EC2 : [INSTALLATION-EC2-WINDOWS.md](INSTALLATION-EC2-WINDOWS.md)**

Pour retrouver les mêmes dépendances, installer `requirements-lock.txt`.
La stratégie courante est décrite dans [docs/TENDANCE-PAIRE.md](docs/TENDANCE-PAIRE.md).

## Documentation du moteur historique HL / Var

Bot d'arbitrage automatisé entre Hyperliquid (HL) et Variational (Var, Omni) : il cote en maker sur HL, couvre chaque fill par un ordre sur Var (et inversement si besoin), et capture l'écart entre les deux venues sur des marchés equity/index synthétiques. Une interface web locale en français permet de le piloter (connexion wallet, approbation d'un agent de trading, démarrage/arrêt, suivi des marchés en temps réel).

## Prérequis

1. **Un Chrome dédié pour Variational** (session WalletConnect persistée), lancé avant de démarrer le bot :

   ```bash
   google-chrome --user-data-dir=$HOME/bot-arb-varia/chrome-var --remote-debugging-port=9223 --no-first-run https://omni.variational.io
   ```

   Connectez le wallet Var à l'intérieur de cette fenêtre (WalletConnect) et laissez l'onglet Omni ouvert : le bot pilote ce même onglet via le protocole CDP, il n'ouvre pas de nouvelle session.

2. **Dépendances Python** :

   ```bash
   pip install -r requirements.txt
   ```

3. Un fichier `config.toml` à la racine (voir `config.toml` fourni) décrivant les marchés suivis et les paramètres globaux (`var_cdp_port` doit correspondre au port CDP ouvert ci-dessus, 9223 par défaut).

## Lancement

```bash
python3 main.py            # mode réel : envoie de vrais ordres sur HL et Var
python3 main.py --paper    # mode simulation : aucun ordre réel, sert à valider la chaîne
                            # cotations -> prix -> motifs de blocage -> interface
```

Au démarrage, le mot de passe du keystore est demandé (il chiffre la clé de l'agent de trading au repos). En mode réel, un mot de passe vide est refusé pour ne jamais garder cette clé en clair sur disque. Le mode `--paper` ne nécessite aucun mot de passe ni aucun agent approuvé.

L'interface est ensuite disponible sur `http://127.0.0.1:8791` (option `--port` pour changer le port). Le serveur n'écoute que sur `127.0.0.1` : il n'est jamais exposé au réseau.

## Utilisation de l'interface

1. **Connecter le wallet** : signe un message (aucune transaction) pour prouver la propriété du compte HL principal.
2. **Approuver l'agent** : signe une approbation EIP-712 d'un agent nommé `BOTARB`. Le bot trade ensuite avec la clé de cet agent, jamais avec la clé principale.
3. **Démarrer** : construit les clients HL et Var, initialise l'orchestrateur (`setup()`) puis lance la boucle de cotation en tâche de fond.
4. **Arrêter** : ferme les positions puis arrête le bot. Étapes affichées : annulation des ordres HL, fermeture des positions Var, fermeture des positions HL, vérification des positions, fermeture des connexions. Tous les marchés sont fermés, y compris ceux désactivés ou à l'arrêt qui portent encore une position, par le même chemin que l'arrêt d'urgence (Var d'abord en reduce-only, puis HL en IOC reduce-only). Les sorties partielles sont réessayées jusqu'à ce que les positions RÉELLES relues sur HL et Var soient à plat, dans la limite de `stop_flatten_timeout_s` (120 s par défaut, section `[global]` de `config.toml`). Si des positions restent ouvertes à ce délai, le bot NE s'arrête PAS : il reste en état arrêté, continue de réessayer, journalise « arrêt : positions non fermées, nouvel essai en cours » et l'interface affiche l'erreur (cliquer à nouveau sur « Arrêter » relance l'attente). Une jambe dont le notionnel est sous `stop_dust_usd` (12 $ par défaut, juste au-dessus du minimum d'ordre HL de 11 $) compte comme à plat : sa sortie est tentée, mais elle ne bloque pas l'arrêt, et un incident « reliquat ... sous le minimum, à solder à la main » est journalisé une fois. Session Var tombée : aucune fermeture (on ne laisse jamais une jambe nue), donc pas d'arrêt tant qu'elle n'est pas reconnectée. L'arrêt d'urgence reste disponible pendant un arrêt normal.
5. **Arrêt d'urgence** : ferme immédiatement toutes les positions sur les deux venues (HL et Var), après confirmation dans l'interface. Le bot reste en marche en état arrêté et réessaie les sorties partielles.

### Arrêt à distance par fichier

- `data/STOP` : même arrêt que le bouton « Arrêter » (fermeture des positions, vérification, puis arrêt). Positions encore ouvertes au délai : le bot reste en marche et la surveillance continue.
- `data/KILL` : arrêt d'urgence, puis arrêt du bot.

### Voyants

- **Session Var** : session WalletConnect active dans l'onglet Omni.
- **WS HL** : flux temps réel (carnet et fills) Hyperliquid vivant.
- **Strikes** : refus 429/418 côté Var sur 10 min et sur 24 h. 3 en 10 min suspendent les cotations 10 min ; 5 en 24 h les suspendent jusqu'à minuit UTC (les couvertures et les sorties passent toujours). Les strikes sont conservés dans le journal et rechargés au redémarrage.
- **Requêtes HL restantes** : marge sur la limite de débit HL de l'adresse ; « annulation seule » quand elle est trop basse.
- **Marge HL** : ratio de marge de maintenance utilisée sur Hyperliquid.
- **Solde Var** : solde du portefeuille Variational.
- **PnL du jour** : cumul du jour (arrêt d'urgence automatique si la perte journalière maximale est atteinte).

### États d'un marché

- **cote** : en cotation maker normale.
- **couverture** : un fill HL vient d'avoir lieu, la couverture Var est en cours.
- **pause** : coté suspendue temporairement (récupérable via le bouton On/Off ou automatiquement).
- **sortie** : débouclage en cours avant fermeture de session ou fin de semaine.
- **bloqué** : anomalie détectée (delta incohérent, couverture incertaine...), intervention manuelle requise. Un marché bloqué n'est jamais débouclé automatiquement.
- **arrêt** : marché désactivé (bouton On/Off), ordres annulés, positions ouvertes conservées.

Chaque ligne du tableau des marchés permet d'activer/désactiver un marché individuellement et d'ajuster à chaud sa marge minimale (points de base), sa taille de fill et sa position maximale.

## Rappels opérationnels

- Les sorties sont automatiques avant 21h UTC et le vendredi (fermeture hebdomadaire des marchés synthétiques Var) : le bot débouble progressivement ses positions avant la fermeture, sans intervention.
- Le rééquilibrage du capital entre Arbitrum (Var) et Hyperliquid reste manuel : le bot ne déplace jamais de fonds entre les deux venues, il gère uniquement les positions ouvertes sur chacune.

## Limites connues

- **Websocket HL sans reconnexion** : le websocket du SDK Hyperliquid ne se reconnecte pas tout seul. Après une coupure prolongée (voyant « WS HL » sur « muet »), le bot annule ses ordres et cesse de coter, mais il ne repart pas de lui-même : arrêtez-le puis relancez-le.
- **Limite de débit HL par adresse** : le budget de requêtes (10 000 plus 1 par USDC tradé) est partagé avec les autres bots du compte principal. Le voyant « Requêtes HL restantes » l'affiche ; sous `hl_min_request_headroom` (2 000 par défaut), le bot passe en « annulation seule » (plus aucun nouvel ordre, seules les annulations et les sorties passent). Le dead-man switch (`scheduleCancel`, réarmé toutes les 60 s avec une échéance à 120 s, seulement quand le bot a des ordres au repos et hors annulation seule) consomme aussi ce budget ; s'il est refusé (compte pas encore éligible), un incident « scheduleCancel indisponible » est journalisé une fois et le bot continue sans.
- **Pas de dead-man switch HL sur ce compte** : `scheduleCancel` exige 1 M$ de volume cumulé (64 k$ au 23/09/2026), il est donc refusé. À la place, le bot annule tous ses ordres HL lors d'un arrêt demandé : bouton « Arrêter », fichier `data/STOP`, `SIGTERM`, `SIGINT` (Ctrl+C) et `SIGHUP` (fermeture de la fenêtre du terminal) déclenchent l'arrêt propre (annulation globale puis fin de la boucle), et une dernière annulation globale synchrone est tentée à la sortie du processus (`atexit`). **Seuls le bouton « Arrêter » et `data/STOP` ferment les positions.** Un arrêt du processus (`SIGTERM`, `SIGINT`, `SIGHUP`, fermeture de la fenêtre, `atexit`) n'a pas le temps de fermer : il annule seulement les ordres et laisse les positions ouvertes sur les deux venues. Pour fermer avant de quitter, cliquer d'abord sur « Arrêter » et attendre la fin. **Ne sont PAS couverts : un `kill -9`, un plantage de la machine, une coupure de courant ou de réseau.** Dans ces cas, les ordres ALO restent au carnet HL : les annuler à la main depuis l'interface Hyperliquid.
- **PnL des sorties et corrections** : c'est une estimation (écart de sortie HL contre Var pour la part couverte, prix d'entrée moyen de la venue pour les parts non couvertes, frais taker HL estimés d'après `userFees`). Le PnL réel se lit sur les deux venues.
- **Vérifications au premier contact, avant le vrai run** (en `--paper` puis avec de petites tailles) :
  - le signe de `qty` dans `/api/positions` de Var (positif = long, négatif = short, ce que le bot suppose partout) ;
  - que `/api/quotes/accept` avec `is_reduce_only` réduit bien une position existante sans en ouvrir une en sens inverse ;
  - le format de `/api/orders/v2` (liste `result`, champ `rfq_id`) et le statut `cleared` d'un ordre exécuté, ainsi que le champ `price` renvoyé ;
  - le format exact de l'instrument `swap` et les tailles minimales Var (spec §11).
  - un `modify` d'ordre ALO réel : inspecter `response.data.statuses[0]` (présence de `resting.oid`, oid identique ou nouveau, forme de l'erreur en cas de refus) ; le bot suppose qu'un oid au repos est renvoyé, et traite une réponse sans oid comme un état inconnu (relecture du carnet) ;
  - le coût d'un `scheduleCancel` sur la limite de débit : comparer `nRequestsUsed` (`userRateLimit`) juste avant et juste après un réarmement du dead-man switch.
- **Démarrage avec un delta** : un marché dont les positions HL et Var ne se compensent pas (écart d'au moins 20 % d'une taille de fill) démarre « bloqué ». Vérifiez les deux venues, corrigez à la main si besoin, puis cliquez « Débloquer ».
- **Interface** : les commandes (arrêt, arrêt d'urgence, activation, paramètres, déblocage) exigent une session wallet ; après un rechargement de la page, reconnectez le wallet avant de les utiliser.
