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

## Configuration

`config/config.toml` = preset `chat-simple` + profil de sécurité `personal` + analytics désactivées.
Modèles : `qwen3.5:4b` (léger, ~8 Go de RAM conseillés) ou `qwen3.5:9b` (meilleur, ~16 Go).
