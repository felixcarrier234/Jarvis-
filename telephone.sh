#!/usr/bin/env bash
# Relie Jarvis à Telegram pour lui parler depuis ton téléphone.
# À lancer sur l'ordinateur où OpenJarvis est installé (après ./install.sh).
set -euo pipefail

CONFIG="${OPENJARVIS_HOME:-$HOME/.openjarvis}/config.toml"
SRC="${OPENJARVIS_HOME:-$HOME/.openjarvis}/src"
VENV_PY="${OPENJARVIS_HOME:-$HOME/.openjarvis}/.venv/bin/python"
export PATH="$HOME/.local/bin:$PATH"

[[ -f "$CONFIG" ]] || { echo "Config introuvable ($CONFIG). Lance d'abord ./install.sh"; exit 1; }

echo "==> Installation du module serveur (nécessaire pour 'jarvis serve')"
if [[ -x "$VENV_PY" && -d "$SRC" ]]; then
  uv pip install --python "$VENV_PY" -q -e "$SRC[server]"
else
  echo "   Installation OpenJarvis non standard : lance toi-même  uv pip install 'openjarvis[server]'"
fi

if grep -q '^\[channel.telegram\]' "$CONFIG"; then
  echo "==> Telegram est déjà configuré dans $CONFIG (supprime les sections [channel] pour recommencer)."
else
  echo
  echo "1) Sur ton téléphone, ouvre Telegram et écris à @BotFather : /newbot"
  echo "   Choisis un nom, il te donne un token du genre 123456789:ABC-def..."
  read -rp "   Colle le token ici : " TOKEN
  echo
  echo "2) Écris à @userinfobot sur Telegram : il te répond avec ton « Id » (un nombre)."
  echo "   Seul ce compte pourra parler à ton Jarvis."
  read -rp "   Ton Id Telegram : " CHAT_ID
  [[ -n "$TOKEN" && "$CHAT_ID" =~ ^-?[0-9]+$ ]] || { echo "Token ou Id invalide."; exit 1; }

  cp "$CONFIG" "$CONFIG.bak.$(date +%s)"
  cat >> "$CONFIG" <<CFG

[channel]
enabled = true
default_channel = "telegram"
default_agent = "simple"

[channel.telegram]
bot_token = "$TOKEN"
allowed_chat_ids = "$CHAT_ID"
parse_mode = "Markdown"
CFG
  chmod 600 "$CONFIG"
  echo "==> Telegram ajouté à $CONFIG"
fi

echo
echo "==> Démarrage de Jarvis. Laisse cette fenêtre (et l'ordinateur) allumés."
echo "    Sur ton téléphone, ouvre ton bot dans Telegram, appuie sur Démarrer et écris-lui."
echo "    Ctrl+C pour arrêter."
exec jarvis serve
