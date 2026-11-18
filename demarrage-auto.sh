#!/usr/bin/env bash
# Fait démarrer Jarvis Voix tout seul quand tu ouvres ta session Windows (WSL).
#   ./demarrage-auto.sh           installe le démarrage automatique
#   ./demarrage-auto.sh retirer   le désinstalle
set -euo pipefail

LANCEUR="$HOME/jarvis-demarrage.sh"
LOG="$HOME/.openjarvis/jarvis-voix.log"
APPDATA_WIN="$(cmd.exe /c 'echo %APPDATA%' 2>/dev/null | tr -d '\r')"
[[ -n "$APPDATA_WIN" ]] || { echo "Ce script doit être lancé dans Ubuntu sous Windows (WSL)."; exit 1; }
STARTUP="$(wslpath "$APPDATA_WIN")/Microsoft/Windows/Start Menu/Programs/Startup"
VBS="$STARTUP/Jarvis.vbs"

if [[ "${1:-}" == "retirer" ]]; then
  rm -f "$VBS" "$LANCEUR"
  pkill -f jarvis-demarrage.sh 2>/dev/null || true
  pkill -f jarvis_voix.py 2>/dev/null || true
  echo "Démarrage automatique retiré et Jarvis arrêté."
  exit 0
fi

# 1) Lanceur côté Ubuntu : un seul Jarvis à la fois, relancé s'il s'arrête.
cat > "$LANCEUR" <<'SH'
#!/usr/bin/env bash
exec 9>/tmp/jarvis-voix.lock
flock -n 9 || exit 0          # déjà lancé
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
LOG="$HOME/.openjarvis/jarvis-voix.log"
while true; do
  echo "=== Démarrage $(date) ===" >> "$LOG"
  "$HOME/.jarvis-voix/bin/python" "$HOME/jarvis_voix.py" >> "$LOG" 2>&1
  echo "=== Arrêt $(date), relance dans 15 s ===" >> "$LOG"
  sleep 15
done
SH
chmod +x "$LANCEUR"

# 2) Raccourci invisible dans le dossier Démarrage de Windows.
mkdir -p "$STARTUP"
printf 'Set s = CreateObject("WScript.Shell")\r\ns.Run "wsl.exe -d %s -- bash -lc ""~/jarvis-demarrage.sh""", 0, False\r\n' \
  "$WSL_DISTRO_NAME" > "$VBS"

echo "✅ Démarrage automatique installé."
echo "   Raccourci Windows : $VBS"
echo "   Journal de Jarvis : $LOG"
