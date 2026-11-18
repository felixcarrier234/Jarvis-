#!/usr/bin/env bash
# Fait démarrer Jarvis Voix tout seul quand tu ouvres ta session Windows (WSL).
#   ./demarrage-auto.sh                 installe le démarrage automatique (+ ouvre la fenêtre Jarvis)
#   ./demarrage-auto.sh --sans-fenetre  pareil, sans ouvrir la fenêtre
#   ./demarrage-auto.sh retirer   le désinstalle
set -euo pipefail

LANCEUR="$HOME/jarvis-demarrage.sh"
LOG="$HOME/.openjarvis/jarvis-voix.log"
# Trouver le dossier « Démarrage » de Windows (depuis C:\ pour éviter les soucis de chemin UNC).
cd /mnt/c 2>/dev/null || true
STARTUP_WIN="$(powershell.exe -NoProfile -Command '[Console]::OutputEncoding=[Text.Encoding]::UTF8; [Environment]::GetFolderPath("Startup")' 2>/dev/null | tr -d '\r' | tail -n1 || true)"
if [[ ! "$STARTUP_WIN" =~ ^[A-Za-z]:\\ ]]; then
  APPDATA_WIN="$(cmd.exe /c 'echo %APPDATA%' 2>/dev/null | tr -d '\r' | tail -n1 || true)"
  [[ "$APPDATA_WIN" =~ ^[A-Za-z]:\\ ]] && STARTUP_WIN="$APPDATA_WIN\\Microsoft\\Windows\\Start Menu\\Programs\\Startup"
fi
STARTUP="$(wslpath -u "$STARTUP_WIN" 2>/dev/null || true)"
if [[ -n "$STARTUP" && ! -d "$(dirname "$STARTUP")" ]]; then
  # Accents abîmés (ex. « F�lix ») : on remplace chaque caractère spécial par « * » et on cherche.
  MOTIF="$(dirname "$STARTUP" | LC_ALL=C sed 's/[^!-~]\+/*/g')"
  for d in $MOTIF; do [[ -d "$d" ]] && STARTUP="$d/Startup" && break; done
fi
cd "$HOME"
if [[ -z "$STARTUP" || ! -d "$(dirname "$STARTUP")" ]]; then
  echo "❌ Impossible de trouver le dossier Démarrage de Windows."
  echo "   Réponse de Windows : « $STARTUP_WIN »"
  echo "   Copie ces deux lignes à Claude."
  exit 1
fi
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

# 2) Raccourci invisible dans le dossier Démarrage de Windows :
#    lance Jarvis, attend qu'il soit prêt, puis ouvre sa fenêtre (sauf avec --sans-fenetre).
mkdir -p "$STARTUP"
{
  printf 'Set s = CreateObject("WScript.Shell")\r\n'
  printf 's.Run "wsl.exe -d %s -- bash -lc ""~/jarvis-demarrage.sh""", 0, False\r\n' "$WSL_DISTRO_NAME"
  if [[ " $* " != *" --sans-fenetre "* ]]; then
    printf '%s\r\n' \
      'On Error Resume Next' \
      'For i = 1 To 90' \
      '  WScript.Sleep 2000' \
      '  Set h = CreateObject("MSXML2.ServerXMLHTTP.6.0")' \
      '  h.open "GET", "http://localhost:8765/", False' \
      '  h.send' \
      '  If Err.Number = 0 Then' \
      '    If h.status = 200 Then Exit For' \
      '  End If' \
      '  Err.Clear' \
      'Next' \
      's.Run "msedge --app=http://localhost:8765", 1, False'
  fi
} > "$VBS"

# 3) Le lancer tout de suite (en arrière-plan, sans fenêtre).
(cd /mnt/c 2>/dev/null; wscript.exe "$(wslpath -w "$VBS")" >/dev/null 2>&1 &) || true

echo "✅ Démarrage automatique installé."
if [[ " $* " == *" --sans-fenetre "* ]]; then
  echo "   Jarvis démarre en arrière-plan (environ 20 secondes)."
else
  echo "   Jarvis démarre en arrière-plan ; sa fenêtre s'ouvrira dès qu'il est prêt (moins d'une minute)."
fi
echo "   Raccourci Windows : $VBS"
echo "   Journal de Jarvis : $LOG"
