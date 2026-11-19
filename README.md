# Jarvis

Ma mise en place de **[OpenJarvis](https://github.com/open-jarvis/OpenJarvis)** : un assistant IA personnel qui tourne en local sur ta machine (via Ollama).

## Installation (macOS / Linux / WSL2)

```bash
git clone https://github.com/felixcarrier234/Jarvis-.git
cd Jarvis-
./install.sh
```

Le script :
1. lance l'installeur officiel d'OpenJarvis (uv, Python, Ollama, un modèle de départ) ;
2. copie `config/config.toml` vers `~/.openjarvis/config.toml` (l'ancienne config est sauvegardée en `.bak`) ;
3. télécharge le modèle `qwen3.5:4b`.

Pour un autre modèle : `JARVIS_MODEL=qwen3.5:9b ./install.sh`

**Windows natif** : dans PowerShell, `irm https://open-jarvis.github.io/OpenJarvis/install.ps1 | iex`, puis copie `config/config.toml` dans `%USERPROFILE%\.openjarvis\config.toml`.
Il existe aussi une appli bureau (`.exe`, `.dmg`, `.deb`…) dans les [releases](https://github.com/open-jarvis/OpenJarvis/releases/latest).

## Utilisation

```bash
jarvis doctor                    # vérifier que tout est OK
jarvis                           # discuter
jarvis ask "Explique-moi X"      # question unique
jarvis gui                       # interface dans le navigateur (Node.js 22+)
```

Autres presets disponibles (remplacent la config) :
`jarvis init --preset <nom> --force` avec `code-assistant`, `deep-research`, `morning-digest-mac`, `morning-digest-linux`, `scheduled-monitor`, `chat-simple`.

## Jarvis Voix : ton assistant dans Telegram (`jarvis_voix.py`)

Tu lui parles en vocal (ou par écrit) dans Telegram, il répond en texte **et en vocal**.
Il lit tes courriels, connaît ton agenda, te rappelle tes rendez-vous et te fait un résumé le matin.

### Installation (Windows + Ubuntu/WSL)
1. Crée un bot avec **@BotFather** (`/newbot`) et récupère ton Id avec **@userinfobot**.
2. Mets le token et l'Id dans `~/.openjarvis/config.toml` (sections `[channel]` avec `enabled = false` et `[channel.telegram]` avec `bot_token` et `allowed_chat_ids`).
3. Outils :
   ```bash
   sudo apt install -y ffmpeg zstd
   ~/.local/bin/uv venv ~/.jarvis-voix
   ~/.local/bin/uv pip install --python ~/.jarvis-voix/bin/python \
     python-telegram-bot faster-whisper edge-tts httpx icalendar recurring-ical-events "av>=11,<19" ddgs "openwakeword==0.4.0"
   cp jarvis_voix.py ~/
   ```
4. Courriel + agenda : `~/.jarvis-voix/bin/python ~/jarvis_voix.py reglages`
   - **Courriel** : Outlook/Hotmail bloque l'IMAP (« Basic authentication is disabled »). Solution : transférer Outlook → Gmail (outlook.live.com → ⚙️ → Courrier → Transfert), puis utiliser l'adresse Gmail, un **mot de passe d'application Google** (myaccount.google.com/apppasswords) et le serveur `imap.gmail.com`.
   - **Agenda** : Google Agenda → ⚙️ Paramètres → ton agenda → Intégrer l'agenda → « Adresse secrète au format iCal ».
5. Démarrage automatique avec Windows : `./demarrage-auto.sh` (retirer : `./demarrage-auto.sh retirer`).

### Utilisation
- **Mode jeu** : quand un jeu est lancé (Steam, Epic, Riot, Xbox, Battle.net…), Jarvis libère la carte graphique, répond avec une petite IA sur le processeur et se tait sur les haut-parleurs ; retour automatique à la fermeture du jeu. Aussi « mode jeu » / « j'ai fini de jouer » / `/jeu`. Hors jeu, l'IA libère la carte graphique après 10 min sans question (réglable).
- **Google Agenda en écriture** (après `~/.jarvis-voix/bin/python ~/jarvis_voix.py google`) : « Ajoute dentiste mardi à 14 h à mon agenda », « Mets dans mon agenda : souper avec Julie vendredi de 18 h à 21 h », « Prends-moi un rendez-vous chez le coiffeur jeudi à 16 h pendant une heure », « Déplace le dentiste à jeudi », « Annule mon rendez-vous chez le dentiste ». Toujours confirmé avant d'écrire. L'agenda est aussi lu en temps réel (plus de délai de l'adresse iCal).
- **Répondre aux courriels** : « Réponds à Amélie que je serai là samedi », ou « Réponds à Amélie » (il résume et demande quoi répondre), ou « Écris un courriel à nom@exemple.com pour dire que … ». Il lit le brouillon et n'envoie qu'après « oui » (boutons ✅/❌ sur Telegram). Envoi par Gmail (même mot de passe d'application).
- **Contrôler le PC** : « Ouvre Spotify / YouTube / la calculatrice », « Mets Céline Dion sur YouTube », « Mets de la musique », « Pause », « Chanson suivante », « Monte le son », « Volume à 30 », « Verrouille l'ordi », « Éteins l'ordi » (à confirmer, « annule l'arrêt »), « Mets un minuteur de 10 minutes ».
- **Trajets** (OpenStreetMap, sans trafic) : « Combien de temps pour aller au Costco de Laval ? » ; avant un rendez-vous avec un lieu, « il est temps de partir ». Adresse de départ dans les réglages.
- **Interface « HUD »** (http://localhost:8765) : dites **« Hey Jarvis »** (prononcé à l'anglaise), cliquez sur le réacteur ou appuyez sur Espace. Il répond phrase par phrase sur les haut-parleurs, réécoute 6 s pour la suite, et « Hey Jarvis » le coupe quand il parle. Nécessite `openwakeword==0.4.0`.
- **Reconnaissance vocale sur la carte NVIDIA** : `~/.jarvis-voix/bin/python ~/jarvis_voix.py accelerer`.
- **Mémoire** : « Souviens-toi que … », « Oublie ce que je t'ai dit sur … », « Qu'est-ce que tu sais sur moi ? » (`/memoire`). Les anniversaires dictés (« l'anniversaire de Julie est le 21 novembre ») sont rappelés le jour même.
- **Listes** : « Ajoute du lait et des œufs à l'épicerie », « J'ai acheté le lait », « Qu'est-ce qu'il y a sur ma liste ? », « Ajoute appeler le garage à mes tâches » (`/liste`, `/epicerie`, `/taches`).
- **Automatique** : tri des courriels (pubs ignorées), alertes météo la veille (neige, grand froid, verglas, vent), résumé du soir, résultat du Canadien dès la fin du match (`/canadiens`).
- **Stop** : « Stop » / « Tais-toi » / `/stop`, ou cliquer sur le cercle, coupe la voix sur l'ordi.
- **IA selon la carte graphique** : `~/.jarvis-voix/bin/python ~/jarvis_voix.py modele` — **Diagnostic** : `… jarvis_voix.py diagnostic`.
- **Sur l'ordinateur, avec le micro** : ouvre http://localhost:8765 dans Edge ou Chrome, clique sur le cercle et parle (il répond à voix haute et réécoute). Astuce : dans Edge, menu ⋯ → Applications → « Installer ce site en tant qu'application » pour avoir une icône Jarvis.
- Vocal 🎤 (bouton micro de Telegram) ou texte : « Qu'est-ce que j'ai demain ? », « Lis-moi mes courriels »…
- Commandes : `/recap`, `/agenda`, `/courriels`, `/meteo`, `/nouvelles`, `/cherche …`, `/rappels`, `/aide`.
- Changer la voix (on écoute chaque voix sur l'ordi) : `~/.jarvis-voix/bin/python ~/jarvis_voix.py voix`
- Automatique : rappel avant chaque rendez-vous, annonce des nouveaux courriels, résumé du matin.
- Journal : `tail -20 ~/.openjarvis/jarvis-voix.log` — Redémarrer : `pkill -f jarvis_voix.py` (relancé en 15 s).
- Désactive la **mise en veille** de Windows, sinon Jarvis ne répond plus.

Note : `telephone.sh` est l'ancienne méthode (connecteur Telegram intégré à OpenJarvis, texte seulement) ; Jarvis Voix la remplace.

## Configuration

`config/config.toml` = preset `chat-simple` + profil de sécurité `personal` + analytics désactivées.
Modèles : `qwen3.5:4b` (léger, ~8 Go de RAM conseillés) ou `qwen3.5:9b` (meilleur, ~16 Go).
