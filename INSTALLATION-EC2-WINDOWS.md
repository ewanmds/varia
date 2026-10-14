# Installer la V1 sur EC2 Windows

Le code et `config.toml` sont ceux du bot local. `requirements-lock.txt` reprend
les versions Python installées sur le PC au moment de la publication.

## Installation

Installer Python 3.12 et Chrome, puis ouvrir PowerShell dans le dossier du projet :

```powershell
git clone https://github.com/ewanmds/varia.git
cd varia
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe -m pip check
```

Avec « Download ZIP », extraire le dépôt et ouvrir PowerShell dans le dossier
extrait ; commencer directement à la création de `.venv`.

1. Arrêter proprement l'ancien bot et attendre la fermeture des positions.
2. Lancer `start-real.cmd` sur EC2 : le serveur prépare la configuration locale
   de l'extension avant son chargement.
3. Dans Chrome, ouvrir `chrome://extensions`, activer le mode développeur et
   charger le dossier `browser_extension` comme extension non empaquetée.
4. Ouvrir un seul onglet https://omni.variational.io et reconnecter le wallet
   directement dans Omni.
5. Ouvrir http://127.0.0.1:8792, connecter le wallet du bot, puis cliquer Démarrer.

Pour un contrôle sans ordres réels, arrêter le serveur réel et utiliser
`start-paper.cmd` avec http://127.0.0.1:8791.

## Configuration conservée

Le bot publie la configuration locale : 1 000 USD par jambe, tranches de 500 USD,
levier de 5, contrôle de tendance chaque minute, cycle volume obligatoire de
5 minutes. Le signal utilise TVC:USOIL / TVC:UKOIL en bougies de 1 minute,
sur 5 bougies closes avec confirmation de 2 minutes.

## État local à transférer séparément

GitHub ne contient pas les données de compte ou les identifiants locaux.
Pour conserver le PnL journalisé et l'historique des strikes, transférer
`data/journal.db` directement sur l'EC2, après arrêt du bot source, avant le
premier démarrage du bot destination. Ne pas écraser un journal de destination
déjà utilisé. Les cotations et fenêtres de signal seront remplies à nouveau.

Ne pas transférer `.venv`, `.chrome-var`, `browser_extension/bridge_config.js`
ou les jetons du pont : ils sont propres à la machine et le pont est régénéré
au lancement. Reconnecter Omni sur EC2. Le mode paire Var ne nécessite aucune
clé d'agent Hyperliquid.

## Fonctionnement continu

Laisser Chrome, Omni et le serveur ouverts sur EC2. Déconnecter la fenêtre RDP
sans fermer la session Windows ; vérifier les politiques de déconnexion.
Un redémarrage de Windows nécessite de relancer les applications et de
revérifier la session Omni. L'interface du bot reste liée à `127.0.0.1`.

Les versions et paramètres sont identiques ; les résultats et la latence
restent dépendants de la machine, du réseau, des spreads et du marché.
