# Paire pétrole sur Windows

Configuration actuelle : 1 000 $ par jambe, tranches de 500 $, contrôle chaque
minute, seuil de perte journalière de 200 $, levier de 5.
Stratégie active : suivi de tendance du ratio WTI/Brent (`signal_mode = "trend_hold"`).
Hausse confirmée : long WTI / short Brent ; baisse confirmée : short WTI / long Brent.
Source du signal : TradingView TVC:USOIL / TVC:UKOIL, bougies de 1 minute.
Régression sur 5 bougies closes, confirmation sur 2 minutes ; le mode volume obligatoire
peut entrer même avec une tendance faible une fois les données disponibles.
Au démarrage, disposer de 5 bougies closes continues ; l'historique récent peut remplir
immédiatement la fenêtre. Le sens confirmé est prioritaire ; au démarrage sans
confirmation, le sens suit la pente, ou B si elle est nulle. Les prix et ordres restent sur Variational.
La tendance est contrôlée chaque minute ; un cycle volume ferme puis rouvre la paire
toutes les 5 minutes, dans le même sens faute de retournement confirmé.
Volume d'un cycle : environ 4 000 $. Le cycle est obligatoire même avec une tendance
faible ; aucun veto du CPM estimé à l'échéance. Source TradingView, cotations
d'exécution et garde-fous doivent rester valides.
Les retournements anticipés passent par le filtre des spreads RFQ ; les cycles volume
obligatoires ne sont pas bloqués par le CPM estimé. Les sorties de sécurité restent prioritaires.
Détails : `docs/TENDANCE-PAIRE.md`.

1. Ouvrir `start-real.cmd`, puis `start-chrome.cmd`. Ce dernier utilise le
   Chrome habituel, ses wallets et ses sessions existantes. Aucun profil
   séparé ni port de débogage n'est utilisé.
2. Une seule fois : ouvrir `chrome://extensions`, activer **Mode développeur**,
   cliquer **Charger l'extension non empaquetée**, sélectionner le dossier
   `browser_extension`. Le fichier local `bridge_config.js` est préparé par
   le serveur ; il contient uniquement un jeton du pont local, aucune clé wallet.
3. Garder un seul onglet https://omni.variational.io ouvert et connecté.
   L'extension épingle cet onglet pour éviter de changer de wallet silencieusement.
4. Ouvrir http://127.0.0.1:8792, connecter le wallet et signer le message
   d'authentification. Vérifier le bandeau du mode réel, puis cliquer **Démarrer**.
   Les ordres seront réels ; aucun agent Hyperliquid n'est requis.
5. Cliquer sur **Arrêter** et attendre la fermeture avant de quitter le serveur.

Pour simuler : fermer le serveur réel après avoir arrêté le bot, puis ouvrir
`start-paper.cmd` et http://127.0.0.1:8791. Le journal simulé est séparé dans
`data/journal-paper.db`. Ne pas démarrer deux instances du bot en parallèle.

Le statut du pont est visible à http://127.0.0.1:8792/api/browser/status.
Il indique aussi la dernière erreur du pont (`last_error`) lorsqu'elle existe.
Après déconnexion, l'extension se reconnecte sans renvoyer de requêtes anciennes.
Un ordre au résultat inconnu n'est jamais automatiquement rejoué par le pont.

Après mise à jour de l'extension : ouvrir `chrome://extensions`, cliquer sur
**Recharger** pour « Bot paire pétrole — session Chrome », puis garder un
seul onglet Omni connecté. Au démarrage explicite du bot, cet onglet est
sélectionné à nouveau et sa session est vérifiée avant les réglages de levier.
Pendant un run, l'onglet reste épinglé : aucun changement automatique de wallet.

`start-real.cmd` réutilise une interface réelle déjà ouverte sur 8792,
sans lancer un second serveur. L'avertissement de clé d'agent HL n'est plus
affiché dans le mode paire, qui n'utilise aucune clé d'agent HL.

Le serveur lancé en arrière-plan écrit dans `data/server-paper.stdout.log`
et `data/server-paper.stderr.log`. Son PID est dans `data/server-paper.pid`.
Le démarrage du serveur seul ne démarre pas les cycles.

L'installation utilise `.venv`. Pour vérifier :

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest -q
```

Le serveur réel a été lancé manuellement par l'utilisateur. Le démarrage des
cycles reste commandé par le bouton **Démarrer**, après authentification wallet.

L'objectif de coût de 70–80 $ par million
provient de la spécification et n'est pas une mesure actuelle de ce compte.

## Vérification du 13 octobre 2026

- Interface HTTP : 200 ; statut `mode=pair`, `paper=true`, `running=false`.
- Configuration chargée : 1 000 $ par jambe, tranches de 500 $, cycle de 10 minutes.
- Vérification initiale via CDP : flux publics USOILP et UKOILP reçus.
- Tests ciblés paire, backtest, serveur, configuration et arrêt : 161 réussis,
  1 ignoré (signal SIGHUP absent sur Windows). Syntaxe JavaScript vérifiée.
- Suite globale : 558 réussis, 7 échecs, 1 ignoré. Les échecs concernent
  l'ancien orchestrateur HL/Var (4), la lecture de textes UTF-8 avec l'encodage
  Windows par défaut (2) et une assertion de permissions POSIX sous Windows (1).
- Dépendances : `pip check` sans conflit.
- Après connexion du wallet : session Omni active ; contrôle paper sur cotations
  réelles réussi, ouverture puis cycle complet, environ 6 002 $ de volume simulé
  et fermeture confirmée. Période accélérée à 6 secondes sur une copie de la
  configuration pour ce contrôle ; le fichier et le serveur restent à 10 minutes.
- Avant le lancement réel : aucune position USOILP/UKOILP. Aucun ordre réel
  envoyé pendant ce contrôle initial.
- Après adaptation Chrome habituel : statut du serveur 8792 `mode=pair`,
  `paper=false`, `running=false` ; pont `transport=extension`, `connected=true`.
  Extension chargée par l'utilisateur. Connexion à l'extension confirmée ;
  exécution réelle via ce nouveau transport encore à vérifier après Démarrer.
- Pont : 8 tests Python réussis (authentification, périmètre, délai d'ordre,
  déconnexion, refus d'un second client, intégration FastAPI) et 3 tests
  JavaScript réussis (onglet épinglé, URL, refus d'onglets multiples).
- Configuration : `var_transport = "extension"`. L'ancien transport CDP reste
  disponible avec `var_transport = "cdp"` si nécessaire.
