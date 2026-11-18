#!/usr/bin/env bash
# Installe OpenJarvis (https://github.com/open-jarvis/OpenJarvis) et applique
# la configuration de ce dépôt. macOS / Linux / WSL2.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODEL="${JARVIS_MODEL:-qwen3.5:4b}"

echo "==> 1/3 Installation officielle d'OpenJarvis (uv, Python, Ollama, modèle de départ)"
curl -fsSL https://open-jarvis.github.io/OpenJarvis/install.sh | bash

# Rendre `jarvis` disponible dans ce shell
export PATH="$HOME/.local/bin:$PATH"

echo "==> 2/3 Application de la configuration ($HERE/config/config.toml)"
mkdir -p "$HOME/.openjarvis"
if [[ -f "$HOME/.openjarvis/config.toml" ]]; then
  cp "$HOME/.openjarvis/config.toml" "$HOME/.openjarvis/config.toml.bak.$(date +%s)"
fi
sed "s/^default_model = .*/default_model = \"$MODEL\"/" "$HERE/config/config.toml" \
  > "$HOME/.openjarvis/config.toml"

echo "==> 3/3 Téléchargement du modèle $MODEL"
if command -v ollama >/dev/null 2>&1; then
  ollama pull "$MODEL" || echo "   (échec du pull, relance plus tard : ollama pull $MODEL)"
else
  echo "   Ollama introuvable — installe-le depuis https://ollama.com puis : ollama pull $MODEL"
fi

echo
echo "Terminé. Vérifie avec :  jarvis doctor"
echo "Puis lance :             jarvis            (chat)"
echo "                         jarvis gui        (interface web, Node.js 22+)"
