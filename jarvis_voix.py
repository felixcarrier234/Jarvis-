"""Jarvis Voix — parle avec Jarvis dans Telegram, il te répond en vocal.

- Tu envoies un message vocal (ou du texte) à ton bot Telegram.
- Le vocal est transcrit sur ton ordinateur (faster-whisper).
- Jarvis (jarvis serve, http://127.0.0.1:8000) prépare la réponse.
- La réponse est renvoyée en texte + message vocal (voix edge-tts).

Lit le token, ton Id et le modèle dans ~/.openjarvis/config.toml
(sections [channel.telegram] et [intelligence]).
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
import time
import tomllib
from collections import defaultdict, deque
from pathlib import Path

import edge_tts
import httpx
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

CONFIG = Path(os.environ.get("OPENJARVIS_HOME", Path.home() / ".openjarvis")) / "config.toml"
API = "http://127.0.0.1:8000"
VOICE = os.environ.get("JARVIS_VOIX", "fr-FR-HenriNeural")  # voix d'homme française
WHISPER_MODEL = os.environ.get("JARVIS_WHISPER", "small")  # tiny/base/small/medium
SYSTEM = (
    "Tu es Jarvis, l'assistant personnel de l'utilisateur. Tu réponds toujours en "
    "français, de façon naturelle et assez courte (2 à 4 phrases), car ta réponse "
    "sera lue à voix haute. N'utilise pas de listes, d'emojis ni de mise en forme."
)

cfg = tomllib.loads(CONFIG.read_text())
tg = cfg.get("channel", {}).get("telegram", {})
TOKEN = tg.get("bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED = {c.strip() for c in str(tg.get("allowed_chat_ids", "")).split(",") if c.strip()}
MODEL = cfg.get("intelligence", {}).get("default_model") or "qwen3.5:2b"

history: dict[int, deque] = defaultdict(lambda: deque(maxlen=10))
_whisper = None


def whisper():
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel

        print(f"Chargement de la reconnaissance vocale ({WHISPER_MODEL})…", flush=True)
        _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    return _whisper


def transcribe(path: str) -> str:
    segments, _ = whisper().transcribe(path, language="fr")
    return " ".join(s.text.strip() for s in segments).strip()


def clean(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"[*_`#>]", "", text)
    return text.strip()


async def ask_jarvis(chat_id: int, question: str) -> str:
    h = history[chat_id]
    h.append({"role": "user", "content": question})
    messages = [{"role": "system", "content": SYSTEM}, *h]
    async with httpx.AsyncClient(timeout=300) as client:
        r = await client.post(
            f"{API}/v1/chat/completions",
            json={"model": MODEL, "messages": messages, "max_tokens": 600},
        )
        r.raise_for_status()
        answer = clean(r.json()["choices"][0]["message"]["content"] or "")
    answer = answer or "Désolé, je n'ai pas de réponse."
    h.append({"role": "assistant", "content": answer})
    return answer


async def speak(text: str, workdir: str) -> str:
    mp3 = os.path.join(workdir, "reponse.mp3")
    ogg = os.path.join(workdir, "reponse.ogg")
    await edge_tts.Communicate(text, VOICE).save(mp3)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", mp3, "-c:a", "libopus", "-b:a", "48k", ogg],
        check=True,
    )
    return ogg


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat_id = update.effective_chat.id
    if ALLOWED and str(chat_id) not in ALLOWED:
        return

    with tempfile.TemporaryDirectory() as tmp:
        await context.bot.send_chat_action(chat_id, "typing")
        if msg.voice or msg.audio:
            f = await (msg.voice or msg.audio).get_file()
            src = os.path.join(tmp, "question.ogg")
            await f.download_to_drive(src)
            question = await asyncio.to_thread(transcribe, src)
            if not question:
                await msg.reply_text("Je n'ai pas compris, tu peux répéter ?")
                return
            await msg.reply_text(f"🎤 « {question} »")
        else:
            question = msg.text or ""

        try:
            answer = await ask_jarvis(chat_id, question)
        except Exception as exc:  # noqa: BLE001
            await msg.reply_text(f"Jarvis ne répond pas ({exc}). Est-ce que 'jarvis serve' tourne ?")
            return

        await msg.reply_text(answer)
        try:
            await context.bot.send_chat_action(chat_id, "record_voice")
            ogg = await speak(answer, tmp)
            with open(ogg, "rb") as fh:
                await msg.reply_voice(fh)
        except Exception as exc:  # noqa: BLE001
            print(f"Voix indisponible : {exc}", flush=True)


def ensure_jarvis_serve() -> subprocess.Popen | None:
    try:
        httpx.get(f"{API}/v1/models", timeout=3)
        return None
    except httpx.HTTPError:
        pass
    jarvis = shutil.which("jarvis") or str(Path.home() / ".local/bin/jarvis")
    print("Démarrage de Jarvis (jarvis serve)…", flush=True)
    log = open(Path.home() / ".openjarvis" / "jarvis-serve.log", "a")
    proc = subprocess.Popen([jarvis, "serve"], stdout=log, stderr=subprocess.STDOUT)
    for _ in range(120):
        time.sleep(1)
        try:
            httpx.get(f"{API}/v1/models", timeout=3)
            return proc
        except httpx.HTTPError:
            if proc.poll() is not None:
                raise SystemExit("jarvis serve s'est arrêté : voir ~/.openjarvis/jarvis-serve.log")
    raise SystemExit("jarvis serve ne démarre pas : voir ~/.openjarvis/jarvis-serve.log")


def main() -> None:
    if not TOKEN:
        raise SystemExit(f"Pas de token Telegram dans {CONFIG} (section [channel.telegram]).")
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg manquant : sudo apt install -y ffmpeg")
    proc = ensure_jarvis_serve()
    try:
        app = ApplicationBuilder().token(TOKEN).build()
        app.add_handler(
            MessageHandler((filters.TEXT & ~filters.COMMAND) | filters.VOICE | filters.AUDIO, on_message)
        )
        print("✅ Jarvis Voix est prêt. Parle-lui sur Telegram ! (Ctrl+C pour arrêter)", flush=True)
        app.run_polling()
    finally:
        if proc is not None:
            proc.terminate()


if __name__ == "__main__":
    main()
