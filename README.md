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

## Parler à Jarvis depuis ton téléphone (Telegram)

Jarvis tourne sur ton ordinateur ; ton téléphone lui écrit via un bot Telegram privé.

1. Installe **Telegram** sur ton téléphone.
2. Sur l'ordinateur (après `./install.sh`) : `./telephone.sh`
3. Le script te demande :
   - le **token du bot** : écris `/newbot` à **@BotFather** dans Telegram ;
   - ton **Id Telegram** : écris à **@userinfobot**. Seul ton compte pourra parler au bot.
4. Il démarre Jarvis (`jarvis serve`). Ouvre ton bot dans Telegram, appuie sur **Démarrer** et écris-lui.

**Pour parler à voix haute** : appuie sur le 🎤 du clavier de ton téléphone (dictée) dans la conversation Telegram.
Les messages vocaux Telegram (le bouton micro du bot) ne sont pas encore compris par OpenJarvis, seul le texte l'est.

Ensuite, pour relancer Jarvis : `jarvis serve`. L'ordinateur doit rester allumé et connecté.
Le token est stocké dans `~/.openjarvis/config.toml` (droits 600) : ne le partage pas.

## Configuration

`config/config.toml` = preset `chat-simple` + profil de sécurité `personal` + analytics désactivées.
Modèles : `qwen3.5:4b` (léger, ~8 Go de RAM conseillés) ou `qwen3.5:9b` (meilleur, ~16 Go).
