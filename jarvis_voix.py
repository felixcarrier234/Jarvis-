"""Jarvis Voix — ton assistant dans Telegram, qui te parle.

- Tu lui envoies un vocal (ou du texte), il te répond en texte + vocal.
- Il lit tes courriels (Outlook/Hotmail via IMAP) quand tu lui demandes.
- Il connaît ton agenda (Google Agenda via l'adresse secrète iCal).
- Il te rappelle tes rendez-vous avant qu'ils commencent, t'annonce les
  nouveaux courriels et te fait un résumé de ta journée le matin.
- Rappels perso (« rappelle-moi demain à 9 h d'appeler maman »), météo,
  nouvelles, recherche sur Internet, et il peut parler sur les haut-parleurs
  de l'ordinateur.
- Page « Jarvis » sur l'ordinateur (http://localhost:8765) : tu lui parles
  avec le micro du PC, il te répond à voix haute.

Usage :
    python jarvis_voix.py            # lance l'assistant
    python jarvis_voix.py reglages   # configure courriel / agenda / rappels
    python jarvis_voix.py voix       # écouter et choisir la voix de Jarvis
    python jarvis_voix.py modele     # choisir la meilleure IA selon la carte graphique
    python jarvis_voix.py accelerer  # reconnaissance vocale sur la carte graphique (NVIDIA)
    python jarvis_voix.py diagnostic # tout tester

Parle directement à Ollama (le moteur d'IA local installé avec OpenJarvis).
Lit le token Telegram, ton Id et le modèle dans ~/.openjarvis/config.toml,
et les réglages de l'assistant dans ~/.openjarvis/assistant.json.
"""

from __future__ import annotations

import asyncio
import email
import getpass
import imaplib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

HOME = Path(os.environ.get("OPENJARVIS_HOME", Path.home() / ".openjarvis"))
CONFIG = HOME / "config.toml"
SETTINGS = HOME / "assistant.json"
OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
if not OLLAMA.startswith("http"):
    OLLAMA = "http://" + OLLAMA
VOICE = os.environ.get("JARVIS_VOIX", "fr-FR-HenriNeural")
EFFET_IA = ("highpass=f=90,aecho=0.8:0.6:18:0.12,equalizer=f=2800:t=q:w=1.2:g=2.5,"
            "acompressor=threshold=-18dB:ratio=3")
VOIX = [  # (nom, voix edge-tts, débit, hauteur, effet IA)
    ("Majordome — grave, posé, petit effet IA", "fr-FR-HenriNeural", "-8%", "-6Hz", True),
    ("Henri — voix normale (France)", "fr-FR-HenriNeural", "+0%", "+0Hz", False),
    ("Rémy — France, chaleureux", "fr-FR-RemyMultilingualNeural", "-4%", "-2Hz", False),
    ("Antoine — Québec", "fr-CA-AntoineNeural", "+0%", "+0Hz", False),
    ("Jean — Québec", "fr-CA-JeanNeural", "+0%", "+0Hz", False),
    ("Thierry — Québec", "fr-CA-ThierryNeural", "+0%", "+0Hz", False),
    ("Gérard — Belgique", "fr-BE-GerardNeural", "+0%", "+0Hz", False),
]
WHISPER_MODEL = os.environ.get("JARVIS_WHISPER", "small")
WEB_HOST = os.environ.get("JARVIS_HOST", "127.0.0.1")
WEB_PORT = int(os.environ.get("JARVIS_PORT", "8765"))

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]

REMINDERS = HOME / "rappels.json"
NEWS_DEFAULT = "https://ici.radio-canada.ca/rss/4159"

MOTS_METEO = re.compile(r"météo|meteo|température|temperature|pleuvoir|pluie|neige|neiger|froid|chaud|"
                        r"parapluie|manteau|temps qu.il (fait|va faire)|degrés|degres|soleil|orage|vent", re.I)
MOTS_NOUVELLES = re.compile(r"nouvelles|actualit|news|journal|qu.est-ce qui se passe", re.I)
MOTS_WEB = re.compile(r"cherche|recherche|internet|sur le web|google|trouve-moi|résultat|resultat|score|"
                      r"qui a gagné|qui a gagne|combien co[uû]te|prix d|horaire d|ouvert|dernière|derniere|récent|recent", re.I)
MOTS_RAPPEL = re.compile(r"\b(rappelle[- ]?moi|rapelle[- ]?moi|fais[- ]moi penser|fait[- ]moi penser|"
                         r"n.oublie pas de me rappeler|mets?[- ]moi un rappel|mets? un rappel|ajoute un rappel|"
                         r"crée un rappel|cree un rappel)\b[,:]?", re.I)

MOTS_COURRIEL = re.compile(r"courriel|e-?mail|mail|message|boîte|boite|inbox|reçu", re.I)
MOTS_AGENDA = re.compile(
    r"agenda|calendrier|rendez|rdv|rencontre|réunion|reunion|horaire|programme|planning|"
    r"aujourd|demain|semaine|ce soir|cet après|cet apres|lundi|mardi|mercredi|jeudi|"
    r"vendredi|samedi|dimanche|occupé|occupe|libre|journée|journee",
    re.I,
)


# ---------------------------------------------------------------------------
# Réglages
# ---------------------------------------------------------------------------

def load_settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_settings(s: dict) -> None:
    SETTINGS.write_text(json.dumps(s, indent=2, ensure_ascii=False))
    os.chmod(SETTINGS, 0o600)


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix} : ").strip()
    return val or default


def diagnostic() -> None:
    """Teste chaque maillon et dit clairement ce qui marche ou pas."""
    s = load_settings()
    ok = lambda m: print(f"  ✅ {m}", flush=True)  # noqa: E731
    ko = lambda m: print(f"  ❌ {m}", flush=True)  # noqa: E731
    print("\n=== Diagnostic de Jarvis ===\n")
    print(f"  Voix réglée : "
          f"{s.get('voice', VOICE)} {s.get('voice_rate', '+0%')} {s.get('voice_pitch', '+0Hz')}"
          f"{' + effet IA' if s.get('voice_effect') else ''}")
    ok(f"Token Telegram présent") if TOKEN else ko("Pas de token Telegram dans la config")
    ok("ffmpeg installé") if shutil.which("ffmpeg") else ko("ffmpeg manquant : sudo apt install -y ffmpeg")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            out = asyncio.run(synth("Test de la voix.", os.path.join(tmp, "t.mp3"), s))
            ok(f"Fabrication de la voix (Microsoft Edge TTS) : {os.path.getsize(out)} octets")
    except Exception as exc:  # noqa: BLE001
        ko(f"Fabrication de la voix impossible : {type(exc).__name__}: {exc}")
    print("  … dossier temporaire Windows :", _win_temp() or "(introuvable, j'utilise Ubuntu)", flush=True)
    try:
        asyncio.run(speak_on_pc("Bonjour Monsieur, ceci est un test de mes haut-parleurs.", wait=True, s=s))
        ok("Son joué sur l'ordinateur — tu l'as entendu ? Sinon vérifie le volume et la sortie audio de Windows.")
    except Exception as exc:  # noqa: BLE001
        ko(f"Son sur l'ordinateur impossible : {exc}")
    ok("« Hey Jarvis » installé") if charger_mot_reveil() else ko(
        "« Hey Jarvis » absent : ~/.local/bin/uv pip install --python ~/.jarvis-voix/bin/python openwakeword==0.4.0")
    print("  … reconnaissance vocale :", "carte graphique" if s.get("whisper_gpu") else
          "processeur (pour accélérer : jarvis_voix.py accelerer)", flush=True)
    smi = shutil.which("nvidia-smi") or ("/usr/lib/wsl/lib/nvidia-smi" if os.path.exists("/usr/lib/wsl/lib/nvidia-smi") else None)
    if smi:
        r = subprocess.run([smi, "--query-gpu=name,memory.total", "--format=csv,noheader"], capture_output=True, text=True)
        ok(f"Carte graphique : {r.stdout.strip()}") if r.returncode == 0 and r.stdout.strip() else ko(
            f"nvidia-smi ne répond pas : {r.stderr.strip()[:200]}")
    else:
        ko("Carte NVIDIA non visible dans Ubuntu (pilote NVIDIA pour Windows à jour ?)")
    try:
        ps = httpx.get(f"{OLLAMA}/api/ps", timeout=5).json().get("models", [])
        for m in ps:
            tot, vram = m.get("size", 0), m.get("size_vram", 0)
            ok(f"Modèle chargé : {m.get('name')} — {round(100 * vram / tot) if tot else 0} % sur la carte graphique")
    except Exception:  # noqa: BLE001
        pass
    try:
        names = [m["name"] for m in httpx.get(f"{OLLAMA}/api/tags", timeout=5).json().get("models", [])]
        ok(f"Ollama répond, modèles : {', '.join(names) or 'AUCUN'} (config : {MODEL})")
    except Exception as exc:  # noqa: BLE001
        ko(f"Ollama ne répond pas : {exc}")
    try:
        httpx.get(f"http://127.0.0.1:{WEB_PORT}/", timeout=5).raise_for_status()
        ok(f"Jarvis tourne (page http://localhost:{WEB_PORT} OK)")
    except Exception:  # noqa: BLE001
        ko("Jarvis ne tourne pas en ce moment (regarde : tail -20 ~/.openjarvis/jarvis-voix.log)")
    print("\nCopie tout ce qui est au-dessus à Claude si quelque chose est en ❌.\n")


def choisir_modele() -> None:
    """Choisit le meilleur modèle d'IA selon la carte graphique NVIDIA et la mémoire, puis l'installe."""
    vram = 0.0
    smi = shutil.which("nvidia-smi") or ("/usr/lib/wsl/lib/nvidia-smi" if os.path.exists("/usr/lib/wsl/lib/nvidia-smi") else None)
    if smi:
        r = subprocess.run([smi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            nom, mem = r.stdout.strip().splitlines()[0].rsplit(",", 1)
            vram = float(mem) / 1024
            print(f"Carte graphique : {nom.strip()} — {vram:.1f} Go")
    if not vram:
        print("Pas de carte NVIDIA visible dans Ubuntu (l'IA tournera sur le processeur).")
    ram = 0.0
    try:
        ram = int(next(l for l in open("/proc/meminfo") if l.startswith("MemTotal")).split()[1]) / 1024 / 1024
    except Exception:  # noqa: BLE001
        pass
    print(f"Mémoire disponible pour Ubuntu : {ram:.1f} Go")
    if vram >= 20:
        modele = "qwen3.5:27b"
    elif vram >= 7 or ram >= 14:
        modele = "qwen3.5:9b"
    elif ram >= 7:
        modele = "qwen3.5:4b"
    else:
        modele = "qwen3.5:2b"
    print(f"\n→ Modèle conseillé : {modele}")
    if ask("Le télécharger et l'utiliser ? (o/n)", "o").lower().startswith("o"):
        ollama = shutil.which("ollama") or "/usr/local/bin/ollama"
        if subprocess.run([ollama, "pull", modele]).returncode != 0:
            print("❌ Le téléchargement a échoué. Vérifie qu'Ollama tourne (ollama serve) et réessaie.")
            return
        txt = CONFIG.read_text()
        if re.search(r"(?m)^default_model\s*=", txt):
            txt = re.sub(r"(?m)^default_model\s*=.*$", f'default_model = "{modele}"', txt)
        elif re.search(r"(?m)^\[intelligence\]", txt):
            txt = re.sub(r"(?m)^\[intelligence\]\s*$", f'[intelligence]\ndefault_model = "{modele}"', txt, count=1)
        else:
            txt += f'\n[intelligence]\ndefault_model = "{modele}"\n'
        CONFIG.write_text(txt)
        print(f"\n✅ Jarvis utilisera maintenant {modele}. Redémarre-le :  pkill -f jarvis_voix.py")


def accelerer() -> None:
    """Installe les bibliothèques NVIDIA pour que la reconnaissance vocale tourne sur la carte graphique."""
    uv = shutil.which("uv") or str(Path.home() / ".local/bin/uv")
    print("Téléchargement des bibliothèques NVIDIA (environ 1 Go, quelques minutes)…", flush=True)
    r = subprocess.run([uv, "pip", "install", "--python", sys.executable, "nvidia-cublas-cu12", "nvidia-cudnn-cu12==9.*"])
    if r.returncode != 0:
        print("❌ Installation impossible.")
        return
    if not _precharger_cuda():
        print("❌ Bibliothèques introuvables après l'installation.")
        return
    try:
        from faster_whisper import WhisperModel

        print("Test de la reconnaissance vocale sur la carte graphique…", flush=True)
        WhisperModel(WHISPER_MODEL, device="cuda", compute_type="float16")
    except Exception as exc:  # noqa: BLE001
        print(f"❌ La carte graphique refuse ({exc}). On reste sur le processeur, ça marche quand même.")
        return
    s = load_settings()
    s["whisper_gpu"] = True
    save_settings(s)
    print("✅ La reconnaissance vocale utilisera la carte graphique. Redémarre Jarvis :  pkill -f jarvis_voix.py")


def choisir_voix() -> None:
    """Fait écouter chaque voix sur l'ordinateur, puis enregistre celle choisie."""
    s = load_settings()
    phrase = "Bonjour Monsieur. Tous les systèmes sont opérationnels. Que puis-je faire pour vous ?"
    print("\n=== Choix de la voix de Jarvis ===\nÉcoute bien tes haut-parleurs…\n")
    for i, (nom, voix, rate, pitch, effet) in enumerate(VOIX, 1):
        print(f"  {i}. {nom}", flush=True)
        essai = {**s, "voice": voix, "voice_rate": rate, "voice_pitch": pitch, "voice_effect": effet}
        try:
            asyncio.run(speak_on_pc(phrase, wait=True, s=essai))
        except Exception as exc:  # noqa: BLE001
            print(f"     ❌ impossible de jouer cette voix : {exc}")
        time.sleep(0.4)
    actuel = next((str(i) for i, v in enumerate(VOIX, 1)
                   if v[1] == s.get("voice") and v[2] == s.get("voice_rate", "+0%")), "1")
    while True:
        rep = ask(f"\nQuelle voix veux-tu ? (1 à {len(VOIX)}, « r » pour réécouter)", actuel)
        if rep.lower() == "r":
            return choisir_voix()
        if rep.isdigit() and 1 <= int(rep) <= len(VOIX):
            break
    nom, voix, rate, pitch, effet = VOIX[int(rep) - 1]
    s.update(voice=voix, voice_rate=rate, voice_pitch=pitch, voice_effect=effet)
    s["majordome"] = ask("Personnalité « majordome » (il te vouvoie et t'appelle Monsieur) ? (o/n)",
                         "o" if s.get("majordome", int(rep) == 1) else "n").lower().startswith("o")
    save_settings(s)
    print(f"\n✅ Voix choisie : {nom}.")
    try:
        asyncio.run(speak_on_pc("Parfait. C'est désormais ma voix." if not s["majordome"]
                                else "Très bien, Monsieur. Ce sera désormais ma voix.", wait=True, s=s))
    except Exception:  # noqa: BLE001
        pass
    print("Redémarre Jarvis pour l'appliquer partout :  pkill -f jarvis_voix.py")


def reglages() -> None:
    s = load_settings()
    print("\n=== Réglages de Jarvis ===  (Entrée = garder la valeur entre crochets)\n")

    print("1) COURRIEL Outlook / Hotmail")
    print("   Il faut un « mot de passe d'application » (pas ton mot de passe normal) :")
    print("   https://account.microsoft.com/security → Options de sécurité avancées")
    print("   → Mots de passe d'application → Créer. (La validation en 2 étapes doit être activée.)")
    s["email"] = ask("   Ton adresse courriel (vide = pas de courriel)", s.get("email", ""))
    if s["email"]:
        pw = getpass.getpass("   Mot de passe d'application (rien ne s'affiche, Entrée = garder) : ").strip()
        if pw:
            s["email_password"] = pw.replace(" ", "")
        s["imap_host"] = ask("   Serveur IMAP", s.get("imap_host", "outlook.office365.com"))
        s["notify_new_email"] = ask(
            "   M'annoncer les nouveaux courriels ? (o/n)",
            "o" if s.get("notify_new_email", True) else "n",
        ).lower().startswith("o")

    print("\n2) AGENDA Google")
    print("   Sur un ordinateur : calendar.google.com → ⚙️ Paramètres → clique sur ton agenda")
    print("   (à gauche, sous « Paramètres de mes agendas ») → « Intégrer l'agenda »")
    print("   → copie « Adresse secrète au format iCal » (elle finit par .ics).")
    s["ics_url"] = ask("   Adresse secrète iCal (vide = pas d'agenda)", s.get("ics_url", ""))
    if s["ics_url"]:
        s["reminder_minutes"] = int(ask("   Me rappeler un rendez-vous combien de minutes avant ?",
                                        str(s.get("reminder_minutes", 30))))

    s["morning"] = ask("\n3) Heure du résumé du matin (ex. 07:30, vide = jamais)", s.get("morning", "07:30"))

    ville = ask("\n4) MÉTÉO — ta ville (ex. Montréal, Québec, Sherbrooke ; vide = pas de météo)",
                s.get("city", ""))
    if ville and (ville != s.get("city") or "lat" not in s):
        try:
            g = geocode(ville)
            s.update(city=ville, lat=g["latitude"], lon=g["longitude"],
                     city_label=f"{g['name']}, {g.get('admin1', '')}".strip(", "))
            print(f"   → {s['city_label']}")
        except Exception as exc:  # noqa: BLE001
            print(f"   ❌ Ville introuvable ({exc}), météo désactivée.")
            s.pop("lat", None)
    elif not ville:
        for k in ("city", "lat", "lon", "city_label"):
            s.pop(k, None)

    s["news_url"] = ask("\n5) NOUVELLES — adresse du fil RSS (Entrée = Radio-Canada, « non » = pas de nouvelles)",
                        s.get("news_url", NEWS_DEFAULT))
    if s["news_url"].lower() in ("non", "n", "aucun"):
        s["news_url"] = ""

    s["pc_voice"] = ask("\n6) Parler aussi à voix haute sur l'ordinateur ? (o/n)",
                        "o" if s.get("pc_voice", True) else "n").lower().startswith("o")
    s["soir"] = ask("\n7) Heure du résumé du soir (ex. 21:00, vide = jamais)", s.get("soir", "21:00"))
    s["heure_alertes"] = ask("8) Heure des alertes météo pour le lendemain (ex. 18:00, vide = jamais)",
                             s.get("heure_alertes", "18:00"))
    s["equipe"] = ask("9) Équipe de hockey à suivre (code LNH : MTL, TOR, BOS… ; vide = aucune)",
                      s.get("equipe", "MTL")).upper()
    s["annoncer_pubs"] = ask("10) Annoncer aussi les courriels publicitaires ? (o/n)",
                             "o" if s.get("annoncer_pubs") else "n").lower().startswith("o")

    save_settings(s)
    print(f"\nRéglages enregistrés dans {SETTINGS}\n\nTest des connexions…")
    if s.get("email"):
        try:
            mails = fetch_emails(s, limit=3)
            print(f"  ✅ Courriel OK ({len(mails)} courriels lus)")
            for m in mails:
                print(f"     - {m['from']} : {m['subject']}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ Courriel : {exc}")
            print("     Vérifie l'adresse et le mot de passe d'application.")
    if s.get("ics_url"):
        try:
            evs = fetch_events(s, days=7, use_cache=False)
            print(f"  ✅ Agenda OK ({len(evs)} rendez-vous dans les 7 prochains jours)")
            for e in evs[:5]:
                print(f"     - {describe_event(e)}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ Agenda : {exc}")
            print("     Vérifie que tu as copié l'adresse SECRÈTE au format iCal.")
    if s.get("lat"):
        try:
            print(f"  ✅ Météo OK : {weather_text(s).splitlines()[1]}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ Météo : {exc}")
    if s.get("news_url"):
        try:
            print(f"  ✅ Nouvelles OK : {fetch_news(s, 1)[0]}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ Nouvelles : {exc}")
    if s.get("pc_voice"):
        print("  🔊 Test de la voix sur l'ordinateur…")
        try:
            asyncio.run(speak_on_pc("Bonjour, je suis Jarvis. Tu m'entends bien ?", wait=True))
            print("  ✅ Si tu as entendu la voix, c'est bon.")
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ Voix sur l'ordinateur : {exc}")
    print("\nRelance Jarvis :  pkill -f jarvis_voix.py   (il redémarre tout seul en 15 s)")


# ---------------------------------------------------------------------------
# Courriels (IMAP)
# ---------------------------------------------------------------------------

def _dec(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001
        return value


def _body(msg: email.message.Message) -> str:
    part = None
    if msg.is_multipart():
        for p in msg.walk():
            if p.get_content_type() == "text/plain" and "attachment" not in str(p.get("Content-Disposition")):
                part = p
                break
        if part is None:
            for p in msg.walk():
                if p.get_content_type() == "text/html":
                    part = p
                    break
    else:
        part = msg
    if part is None:
        return ""
    raw = part.get_payload(decode=True) or b""
    text = raw.decode(part.get_content_charset() or "utf-8", errors="replace")
    if part.get_content_type() == "text/html":
        text = re.sub(r"<(script|style).*?</\1>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _imap(s: dict) -> imaplib.IMAP4_SSL:
    box = imaplib.IMAP4_SSL(s.get("imap_host", "outlook.office365.com"), 993)
    box.login(s["email"], s["email_password"])
    box.select("INBOX", readonly=True)
    return box


def fetch_emails(s: dict, limit: int = 8, since_uid: int = 0) -> list[dict]:
    """Derniers courriels de la boîte de réception (sans les marquer comme lus)."""
    box = _imap(s)
    try:
        crit = f"UID {since_uid + 1}:*" if since_uid else "ALL"
        typ, data = box.uid("search", None, crit)
        uids = [int(u) for u in (data[0] or b"").split() if int(u) > since_uid]
        out = []
        for uid in sorted(uids)[-limit:][::-1]:
            typ, msgdata = box.uid("fetch", str(uid), "(FLAGS BODY.PEEK[])")
            raw = next((p[1] for p in msgdata if isinstance(p, tuple)), None)
            if not raw:
                continue
            flags = str(msgdata[0][0]) if msgdata and isinstance(msgdata[0], tuple) else ""
            msg = email.message_from_bytes(raw)
            sender = _dec(msg.get("From"))
            sender = re.sub(r"\s*<[^>]+>", "", sender).strip('" ') or sender
            try:
                when = parsedate_to_datetime(msg.get("Date")).astimezone()
            except Exception:  # noqa: BLE001
                when = None
            subject = _dec(msg.get("Subject")) or "(sans objet)"
            body = _body(msg)[:400]
            out.append({
                "uid": uid,
                "from": sender,
                "subject": subject,
                "date": when,
                "unread": "\\Seen" not in flags,
                "body": body,
                "categorie": classer_courriel(msg, subject, body),
            })
        return out
    finally:
        try:
            box.logout()
        except Exception:  # noqa: BLE001
            pass


PROMO_EXPEDITEUR = re.compile(r"no-?reply|ne-?pas-?repondre|newsletter|infolettre|marketing|promo|offres?@|deals?@|"
                              r"news@|info@|hello@|bonjour@|notifications?@|mailer", re.I)
PROMO_SUJET = re.compile(r"rabais|promo|solde|soldes|\d+ ?%|offre|exclusi|économisez|economisez|gratuit|webinaire|"
                         r"infolettre|newsletter|black friday|cyber|dernière chance|derniere chance|nouveautés", re.I)
IMPORTANT = re.compile(r"urgent|important|facture|paiement|échéance|echeance|rendez-vous|rdv|confirmation|code|"
                       r"sécurité|securite|mot de passe|compte|livraison|colis|rembours|impôt|impot|revenu québec|"
                       r"arc\b|banque|desjardins|hydro|loyer|bail|contrat|médecin|medecin|clinique|école|ecole|travail|"
                       r"entrevue|réservation|reservation|vol\b|billet|réunion|reunion|meeting", re.I)


def classer_courriel(msg, subject: str, body: str) -> str:
    """« important », « normal » ou « pub » (publicités / infolettres)."""
    expediteur = str(msg.get("From", ""))
    pub = bool(msg.get("List-Unsubscribe")) or str(msg.get("Precedence", "")).lower() in ("bulk", "list")
    if not pub and PROMO_EXPEDITEUR.search(expediteur) and PROMO_SUJET.search(subject):
        pub = True
    texte = f"{subject} {body[:200]}"
    if IMPORTANT.search(texte) and not (pub and PROMO_SUJET.search(subject)):
        return "important"
    if pub or PROMO_SUJET.search(subject):
        return "pub"
    return "normal"


def last_uid(s: dict) -> int:
    box = _imap(s)
    try:
        typ, data = box.uid("search", None, "ALL")
        uids = [int(u) for u in (data[0] or b"").split()]
        return max(uids) if uids else 0
    finally:
        box.logout()


def emails_text(mails: list[dict]) -> str:
    lines = []
    for m in mails:
        when = fmt_when(m["date"]) if m["date"] else ""
        etat = "NON LU" if m["unread"] else "lu"
        cat = {"important": " [IMPORTANT]", "pub": " [PUB]"}.get(m.get("categorie", ""), "")
        lines.append(f"- [{etat}]{cat} {when} — De : {m['from']} — Objet : {m['subject']}\n  Début : {m['body'][:250]}")
    return "\n".join(lines) or "(aucun courriel)"


# ---------------------------------------------------------------------------
# Agenda (Google, adresse secrète iCal)
# ---------------------------------------------------------------------------

_ics_cache: dict = {"t": 0.0, "cal": None}


def _calendar(s: dict, use_cache: bool = True):
    import icalendar

    if use_cache and _ics_cache["cal"] is not None and time.time() - _ics_cache["t"] < 300:
        return _ics_cache["cal"]
    r = httpx.get(s["ics_url"], timeout=30, follow_redirects=True)
    r.raise_for_status()
    cal = icalendar.Calendar.from_ical(r.content)
    _ics_cache.update(t=time.time(), cal=cal)
    return cal


def fetch_events(s: dict, days: int = 7, use_cache: bool = True) -> list[dict]:
    import recurring_ical_events

    now = datetime.now().astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=days + 1)
    evs = []
    for ev in recurring_ical_events.of(_calendar(s, use_cache)).between(start, end):
        dt = ev.get("DTSTART").dt
        all_day = not isinstance(dt, datetime)
        if all_day:
            begin = datetime.combine(dt, datetime.min.time()).astimezone()
        else:
            begin = dt.astimezone() if dt.tzinfo else dt.replace(tzinfo=now.tzinfo)
        evs.append({
            "uid": str(ev.get("UID", "")),
            "title": str(ev.get("SUMMARY", "(sans titre)")),
            "where": str(ev.get("LOCATION", "") or ""),
            "start": begin,
            "all_day": all_day,
        })
    return sorted(evs, key=lambda e: e["start"])


def fmt_hour(dt: datetime) -> str:
    return f"{dt.hour} h {dt.minute:02d}" if dt.minute else f"{dt.hour} h"


def fmt_day(d: date) -> str:
    today = datetime.now().astimezone().date()
    if d == today:
        return "aujourd'hui"
    if d == today + timedelta(days=1):
        return "demain"
    return f"{JOURS[d.weekday()]} {d.day} {MOIS[d.month - 1]}"


def fmt_when(dt: datetime) -> str:
    return f"{fmt_day(dt.date())} à {fmt_hour(dt)}"


def describe_event(e: dict) -> str:
    when = f"{fmt_day(e['start'].date())} (toute la journée)" if e["all_day"] else fmt_when(e["start"])
    where = f" — lieu : {e['where']}" if e["where"] else ""
    return f"{when} : {e['title']}{where}"


def events_text(evs: list[dict]) -> str:
    return "\n".join(f"- {describe_event(e)}" for e in evs) or "(aucun rendez-vous)"


# ---------------------------------------------------------------------------
# Météo (Open-Meteo, gratuit, sans clé)
# ---------------------------------------------------------------------------

METEO_CODES = {
    0: "ciel dégagé", 1: "plutôt dégagé", 2: "partiellement nuageux", 3: "couvert",
    45: "brouillard", 48: "brouillard givrant", 51: "bruine légère", 53: "bruine", 55: "bruine forte",
    56: "bruine verglaçante", 57: "bruine verglaçante", 61: "pluie faible", 63: "pluie", 65: "forte pluie",
    66: "pluie verglaçante", 67: "forte pluie verglaçante", 71: "neige faible", 73: "neige",
    75: "forte neige", 77: "grésil", 80: "averses", 81: "averses", 82: "fortes averses",
    85: "averses de neige", 86: "fortes averses de neige", 95: "orages", 96: "orages avec grêle",
    99: "orages avec grêle",
}


def geocode(city: str) -> dict:
    r = httpx.get("https://geocoding-api.open-meteo.com/v1/search",
                  params={"name": city, "count": 1, "language": "fr"}, timeout=20)
    r.raise_for_status()
    res = r.json().get("results") or []
    if not res:
        raise RuntimeError("aucun résultat")
    return res[0]


def weather_data(s: dict) -> dict:
    r = httpx.get("https://api.open-meteo.com/v1/forecast", timeout=20, params={
        "latitude": s["lat"], "longitude": s["lon"], "timezone": "auto", "forecast_days": 3,
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                 "snowfall_sum,precipitation_sum,wind_gusts_10m_max",
    })
    r.raise_for_status()
    d = r.json()
    c, dd = d["current"], d["daily"]
    jours = []
    for i in range(len(dd["time"])):
        jours.append({
            "nom": ["Aujourd'hui", "Demain", "Après-demain"][i] if i < 3 else dd["time"][i],
            "ciel": METEO_CODES.get(dd["weather_code"][i], "variable"),
            "min": round(dd["temperature_2m_min"][i]), "max": round(dd["temperature_2m_max"][i]),
            "pluie": dd["precipitation_probability_max"][i],
            "code": dd["weather_code"][i],
            "neige_cm": round((dd.get("snowfall_sum") or [0] * 9)[i] or 0),
            "precip_mm": round((dd.get("precipitation_sum") or [0] * 9)[i] or 0),
            "rafales": round((dd.get("wind_gusts_10m_max") or [0] * 9)[i] or 0),
        })
    return {
        "ville": s.get("city_label") or s.get("city"),
        "temp": round(c["temperature_2m"]), "ressenti": round(c["apparent_temperature"]),
        "ciel": METEO_CODES.get(c["weather_code"], "temps variable"), "vent": round(c["wind_speed_10m"]),
        "jours": jours,
    }


def weather_text(s: dict, w: dict | None = None) -> str:
    w = w or weather_data(s)
    lines = [f"MÉTÉO à {w['ville']} :",
             f"Maintenant : {w['temp']} °C (ressenti {w['ressenti']} °C), {w['ciel']}, vent {w['vent']} km/h."]
    for j in w["jours"]:
        lines.append(f"{j['nom']} : {j['ciel']}, min {j['min']} °C, max {j['max']} °C"
                     + (f", {j['pluie']} % de risque de précipitations." if j["pluie"] is not None else "."))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Nouvelles (fil RSS) et recherche Internet (DuckDuckGo)
# ---------------------------------------------------------------------------

def fetch_news(s: dict, limit: int = 6) -> list[str]:
    import xml.etree.ElementTree as ET

    r = httpx.get(s.get("news_url") or NEWS_DEFAULT, timeout=20, follow_redirects=True,
                  headers={"User-Agent": "Mozilla/5.0 (JarvisVoix)"})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    titres = []
    for item in root.iter():
        if item.tag.split("}")[-1] in ("item", "entry"):
            t = next((c.text for c in item if c.tag.split("}")[-1] == "title" and c.text), None)
            if t:
                titres.append(re.sub(r"\s+", " ", t).strip())
        if len(titres) >= limit:
            break
    if not titres:
        raise RuntimeError("fil vide")
    return titres


def web_search(query: str, limit: int = 5) -> str:
    from ddgs import DDGS

    res = DDGS().text(query, region="ca-fr", max_results=limit) or []
    lines = [f"- {r.get('title', '')} : {r.get('body', '')[:300]} ({r.get('href', '')})" for r in res]
    return "\n".join(lines) or "(aucun résultat)"


# ---------------------------------------------------------------------------
# Rappels perso (« rappelle-moi demain à 9 h d'appeler maman »)
# ---------------------------------------------------------------------------

NOMBRES = {"un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6, "sept": 7,
           "huit": 8, "neuf": 9, "dix": 10, "quinze": 15, "vingt": 20, "trente": 30, "quarante": 40,
           "quarante-cinq": 45, "cinquante": 50, "soixante": 60}
JOURS_RE = "|".join(JOURS)
MOIS_RE = "|".join(MOIS + ["fevrier", "aout", "decembre"])


def _num(x: str) -> int:
    return int(x) if x.isdigit() else NOMBRES[x.lower()]


def parse_rappel(text: str, now: datetime | None = None):
    """Retourne (date/heure, quoi) ou None si on ne trouve pas quand."""
    now = now or datetime.now().astimezone()
    t = MOTS_RAPPEL.sub(" ", text)
    spans: list[tuple[int, int]] = []
    when = None
    day = None
    hour = None
    default_hour = 9

    def take(m):
        spans.append(m.span())

    nb = r"(\d+|une?|deux|trois|quatre|cinq|six|sept|huit|neuf|dix|quinze|vingt|trente|quarante(?:-cinq)?|cinquante|soixante)"
    m = re.search(rf"\bdans\s+{nb}\s*(minutes?|mins?|heures?|h|jours?)\b(\s+et\s+demie?)?", t, re.I)
    if m:
        take(m)
        n = _num(m.group(1))
        unit = m.group(2).lower()
        if unit.startswith("min"):
            when = now + timedelta(minutes=n)
        elif unit.startswith("h"):
            when = now + timedelta(hours=n, minutes=30 if m.group(3) else 0)
        else:
            day = (now + timedelta(days=n)).date()
    if when is None and (m := re.search(r"\bdans\s+une?\s+demi-?heure\b", t, re.I)):
        take(m)
        when = now + timedelta(minutes=30)

    if when is None:
        if m := re.search(r"\bapr[eè]s[- ]demain\b", t, re.I):
            take(m); day = (now + timedelta(days=2)).date()
        elif m := re.search(r"\bdemain(\s+matin|\s+soir|\s+apr[eè]s[- ]midi)?\b", t, re.I):
            take(m); day = (now + timedelta(days=1)).date()
            part = (m.group(1) or "").strip().lower()
            default_hour = 19 if part == "soir" else 14 if part.startswith("apr") else 9
        elif m := re.search(r"\b(ce soir|ce matin|cet apr[eè]s[- ]midi|aujourd.hui|tant[oô]t)\b", t, re.I):
            take(m); day = now.date()
            w = m.group(1).lower()
            default_hour = 19 if "soir" in w else 14 if "apr" in w else 9 if "matin" in w else now.hour + 1
        elif m := re.search(rf"\b(?:le\s+)?(\d{{1,2}})(?:er)?\s+({MOIS_RE})\b", t, re.I):
            take(m)
            mois = m.group(2).lower().replace("fevrier", "février").replace("aout", "août").replace("decembre", "décembre")
            mo = MOIS.index(mois) + 1
            y = now.year + (1 if (mo, int(m.group(1))) < (now.month, now.day) else 0)
            day = date(y, mo, int(m.group(1)))
        elif m := re.search(rf"\b({JOURS_RE})(\s+prochain)?(\s+(matin|soir|apr[eè]s[- ]midi))?\b", t, re.I):
            take(m)
            target = JOURS.index(m.group(1).lower())
            delta = (target - now.weekday()) % 7 or 7
            day = (now + timedelta(days=delta)).date()
            part = (m.group(4) or "").lower()
            default_hour = 19 if part == "soir" else 14 if part.startswith("apr") else 9

        if m := re.search(r"\b(?:à|a|vers|pour)?\s*(\d{1,2})\s*(?:h|heures?|:)\s*(\d{2})?(?!\d)", t, re.I):
            take(m); hour = (int(m.group(1)), int(m.group(2) or 0))
        elif m := re.search(r"\b(?:à|a|vers)?\s*(midi|minuit)(\s+et\s+demie)?\b", t, re.I):
            take(m); hour = (12 if m.group(1).lower() == "midi" else 0, 30 if m.group(2) else 0)

        if day is None and hour is None:
            return None
        if day is None:
            day = now.date()
        h, mi = hour if hour else (min(default_hour, 23), 0)
        if hour and h < 12 and re.search(r"\b(soir|apr[eè]s[- ]midi)\b", t, re.I):
            h += 12
        when = datetime(day.year, day.month, day.day, h, mi).astimezone()
        if when <= now and hour and day == now.date():
            when += timedelta(days=1)

    quoi = t
    for a, b in sorted(spans, reverse=True):
        quoi = quoi[:a] + " " + quoi[b:]
    quoi = re.sub(r"\s+", " ", quoi).strip(" ,.!?;:")
    quoi = re.sub(r"^(de |d'|d’|qu'|qu’|que |pour |s'il te pla[iî]t,? )", "", quoi, flags=re.I).strip()
    quoi = re.sub(r"\s*,?\s*s'il te pla[iî]t$", "", quoi, flags=re.I).strip(" ,.")
    return when, quoi or "ton rappel"


def load_reminders() -> list[dict]:
    try:
        return json.loads(REMINDERS.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_reminders(items: list[dict]) -> None:
    REMINDERS.write_text(json.dumps(items, indent=2, ensure_ascii=False))


def add_reminder(when: datetime, what: str) -> None:
    items = load_reminders()
    items.append({"when": when.isoformat(), "what": what})
    items.sort(key=lambda r: r["when"])
    save_reminders(items)


# ---------------------------------------------------------------------------
# Mémoire à long terme, listes, anniversaires, alertes météo, Canadiens
# ---------------------------------------------------------------------------

MEMOIRE = HOME / "memoire.json"
LISTES = HOME / "listes.json"
ETAT = HOME / "etat.json"


def _load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def _sans_accents(t: str) -> str:
    import unicodedata

    return "".join(c for c in unicodedata.normalize("NFD", t.lower()) if unicodedata.category(c) != "Mn")


# --- Mémoire ---------------------------------------------------------------

MEM_AJOUT = re.compile(r"^\s*(?:jarvis\W+)?(?:souviens[- ]toi|rappelle[- ]toi|retiens|m[ée]morise|n.oublie pas|"
                       r"garde en m[ée]moire|note bien|note)\s+(?:bien\s+)?(?:que\s+|qu['’]|de\s+|ceci\s*:\s*|ça\s*:\s*)"
                       r"(?P<fait>.+)$", re.I)
MEM_OUBLI = re.compile(r"^\s*(?:jarvis\W+)?(?:oublie|efface de ta m[ée]moire|supprime de ta m[ée]moire)\s+"
                       r"(?:ce que je t.ai dit (?:sur|à propos de)\s+|que\s+|qu['’]|l.info sur\s+|tout sur\s+)?(?P<quoi>.+)$", re.I)
MEM_LISTE = re.compile(r"(qu.est-ce que tu sais|que sais-tu|tu sais quoi|qu.as-tu retenu|qu.est-ce que tu as retenu|"
                       r"ta m[ée]moire)", re.I)
ANNIV = re.compile(r"anniversaire\s+(?:de\s+|d['’])(?P<nom>[\w'’ -]+?)\s+(?:est\s+|c.est\s+|tombe\s+)?le\s+"
                   r"(?P<j>\d{1,2})(?:er)?\s+(?P<m>[a-zéû]+)", re.I)
NAISSANCE = re.compile(r"(?P<nom>[A-ZÉ][\w'’-]+(?:\s+[A-ZÉ][\w'’-]+)?)\s+est\s+n[ée]e?\s+le\s+(?P<j>\d{1,2})(?:er)?\s+"
                       r"(?P<m>[a-zéû]+)", re.I)


def memoire() -> list[dict]:
    return _load(MEMOIRE, [])


def memoire_ajoute(fait: str) -> str:
    fait = fait.strip(" .!?")
    items = memoire()
    if not any(_sans_accents(i["fait"]) == _sans_accents(fait) for i in items):
        items.append({"fait": fait, "date": datetime.now().astimezone().date().isoformat()})
        _save(MEMOIRE, items)
    return fait


def deuxieme_personne(t: str) -> str:
    """« ma blonde s'appelle Julie » → « ta blonde s'appelle Julie » (pour les confirmations)."""
    rempl = [(r"\bje suis\b", "tu es"), (r"\bj['’]ai\b", "tu as"), (r"\bje\b", "tu"), (r"\bma\b", "ta"),
             (r"\bmon\b", "ton"), (r"\bmes\b", "tes"), (r"\bme\b", "te"), (r"\bmoi\b", "toi"), (r"\bm['’]", "t'")]
    for a, b in rempl:
        t = re.sub(a, b, t, flags=re.I)
    return t


def memoire_oublie(quoi: str) -> list[str]:
    quoi = quoi.strip(" .!?")
    items = memoire()
    if re.fullmatch(r"(ça|ca|cela|ce que je viens de dire|la dernière chose)", quoi, re.I) and items:
        enleves = [items.pop()["fait"]]
    else:
        vides = {"les", "des", "une", "que", "qui", "sur", "mon", "mes", "ton", "tes", "son", "ses", "aux", "pour",
                 "dans", "avec", "est", "sont", "ete", "tout", "propos", "concernant", "info", "infos"}
        mots = [w for w in re.findall(r"\w+", _sans_accents(quoi)) if len(w) > 2 and w not in vides]
        garde, enleves = [], []
        for i in items:
            (enleves if mots and all(w in _sans_accents(i["fait"]) for w in mots) else garde).append(i)
        items = garde
        enleves = [i["fait"] for i in enleves]
    _save(MEMOIRE, items)
    return enleves


def memoire_texte() -> str:
    items = memoire()[-60:]
    return "\n".join(f"- {i['fait']}" for i in items)


def anniversaires() -> list[dict]:
    """Anniversaires trouvés dans la mémoire : [{nom, jour, mois}]."""
    out = []
    for i in memoire():
        for rx in (ANNIV, NAISSANCE):
            m = rx.search(i["fait"])
            if m:
                mois = _sans_accents(m.group("m"))
                noms = [_sans_accents(x) for x in MOIS]
                if mois in noms:
                    out.append({"nom": m.group("nom").strip(), "jour": int(m.group("j")), "mois": noms.index(mois) + 1})
                break
    return out


def anniversaires_proches(jours: int = 7) -> list[dict]:
    today = datetime.now().astimezone().date()
    out = []
    for a in anniversaires():
        for an in (today.year, today.year + 1):
            try:
                d = date(an, a["mois"], a["jour"])
            except ValueError:
                continue
            if 0 <= (d - today).days <= jours:
                out.append({**a, "dans": (d - today).days, "date": d})
                break
    return sorted(out, key=lambda a: a["dans"])


# --- Listes (épicerie, tâches, …) ------------------------------------------

LISTE_NOM = r"(?:la\s+|ma\s+|les\s+|mes\s+|l['’])?(?:liste(?:\s+(?:d['’]|de\s+(?:l['’])?|des\s+|du\s+|de\s+la\s+)?(?P<liste>[\w'’ -]+?))?|(?P<connue>épicerie|epicerie|courses|commissions|tâches|taches|choses à faire|to-?do))"
LISTE_AJOUT = re.compile(r"^\s*(?:jarvis\W+)?(?:ajoute|rajoute|mets|met|inscris|ajouter)\s+(?P<items>.+?)\s+(?:à|a|dans|sur)\s+"
                         + LISTE_NOM + r"\s*[.!?]*$", re.I)
LISTE_RETIRE = re.compile(r"^\s*(?:jarvis\W+)?(?:enlève|enleve|retire|supprime|efface|coche|raye|barre)\s+(?P<items>.+?)\s+"
                          r"(?:de|du|des|d['’]|à|a|sur|dans)\s+" + LISTE_NOM + r"\s*[.!?]*$", re.I)
LISTE_ACHETE = re.compile(r"^\s*(?:jarvis\W+)?j.ai\s+(?:acheté|achete|pris|trouvé)\s+(?P<items>.+?)\s*[.!?]*$", re.I)
LISTE_FAIT = re.compile(r"^\s*(?:jarvis\W+)?j.ai\s+(?:fini|terminé|termine|fait)\s+(?:de\s+|d['’])?(?P<items>.+?)\s*[.!?]*$", re.I)
LISTE_LIRE = re.compile(r"(qu.est-ce qu.il y a|qu.y a-t-il|lis|lire|dis-moi|c.est quoi|montre|affiche|il me reste quoi|"
                        r"qu.est-ce qu.il me reste)\b.*\b(liste|épicerie|epicerie|courses|tâches|taches|choses à faire|acheter)", re.I)
LISTE_VIDE = re.compile(r"^\s*(?:jarvis\W+)?(?:vide|efface|supprime)\s+(?:toute\s+)?" + LISTE_NOM + r"\s*[.!?]*$", re.I)


def _nom_liste(m) -> str:
    raw = (m.group("connue") or m.group("liste") or "").lower().strip()
    a = _sans_accents(raw)
    if any(k in a for k in ("epicerie", "course", "commission", "acheter", "magasin")):
        return "épicerie"
    if any(k in a for k in ("tache", "faire", "todo", "to-do", "to do")):
        return "tâches"
    return raw or "épicerie"


def _articles(item: str) -> str:
    return re.sub(r"^(?:du|de la|de l['’]|des|d['’]|un|une|le|la|les|l['’]|mes|mon|ma)\s*", "", item.strip(" .!?"),
                  flags=re.I).strip()


def _split_items(items: str) -> list[str]:
    return [_articles(x) for x in re.split(r"\s*,\s*|\s+et\s+|\s+puis\s+", items) if _articles(x)]


def listes() -> dict:
    return _load(LISTES, {})


def liste_texte(nom: str) -> str:
    items = listes().get(nom, [])
    return f"Ta liste {'d' + chr(39) if nom[0] in 'aeéiou' else 'de '}{nom} est vide." if not items else \
        f"Sur ta liste {'d' + chr(39) if nom[0] in 'aeéiou' else 'de '}{nom} : {', '.join(items)}."


def gerer_listes(q: str) -> str | None:
    """Ajoute / retire / lit / vide une liste. Retourne la réponse, ou None si ce n'est pas une phrase de liste."""
    data = listes()
    if m := LISTE_VIDE.match(q):
        nom = _nom_liste(m)
        data[nom] = []
        _save(LISTES, data)
        return f"J'ai vidé ta liste {'d' + chr(39) if nom[0] in 'aeéiou' else 'de '}{nom}."
    if m := LISTE_AJOUT.match(q):
        if not (m.group("connue") or re.search(r"\bliste\b", q, re.I)):
            return None
        nom, items = _nom_liste(m), _split_items(m.group("items"))
        cur = data.setdefault(nom, [])
        cur += [i for i in items if _sans_accents(i) not in map(_sans_accents, cur)]
        _save(LISTES, data)
        return f"C'est ajouté à ta liste {'d' + chr(39) if nom[0] in 'aeéiou' else 'de '}{nom} : {', '.join(items)}."
    m = LISTE_RETIRE.match(q)
    nom = _nom_liste(m) if m else "épicerie" if (m := LISTE_ACHETE.match(q)) else "tâches" if (m := LISTE_FAIT.match(q)) else None
    if m and nom:
        if LISTE_RETIRE.match(q) and not (m.group("connue") or re.search(r"\bliste\b", q, re.I)):
            return None
        cur = data.get(nom, [])
        enleves = []
        for it in _split_items(m.group("items")):
            for x in list(cur):
                if _sans_accents(it) in _sans_accents(x) or _sans_accents(x) in _sans_accents(it):
                    cur.remove(x)
                    enleves.append(x)
        if not enleves:
            if LISTE_ACHETE.match(q) or LISTE_FAIT.match(q):
                return None
            return f"Je n'ai pas trouvé ça sur ta liste {'d' + chr(39) if nom[0] in 'aeéiou' else 'de '}{nom}."
        data[nom] = cur
        _save(LISTES, data)
        reste = f" Il reste : {', '.join(cur)}." if cur else " La liste est maintenant vide."
        return f"C'est fait, j'ai retiré {', '.join(enleves)}.{reste}"
    if LISTE_LIRE.search(q):
        a = _sans_accents(q)
        if any(k in a for k in ("tache", "a faire")):
            return liste_texte("tâches")
        autres = [n for n in data if _sans_accents(n) in a and n not in ("épicerie", "tâches")]
        return liste_texte(autres[0] if autres else "épicerie")
    return None


# --- Alertes météo ---------------------------------------------------------

def alertes_meteo(w: dict, index: int = 1) -> list[str]:
    """Alertes utiles pour le jour index (0 = aujourd'hui, 1 = demain)."""
    if len(w.get("jours", [])) <= index:
        return []
    j = w["jours"][index]
    quand = j["nom"].lower()
    out = []
    if j.get("neige_cm", 0) >= 5:
        out.append(f"Il va tomber environ {j['neige_cm']} cm de neige {quand} : prévoir la pelle et plus de temps pour la route.")
    if j.get("code") in (56, 57, 66, 67):
        out.append(f"Risque de verglas {quand} : prudence sur la route et sur les trottoirs.")
    if j.get("min", 0) <= -20:
        out.append(f"Grand froid {quand} : jusqu'à {j['min']} °C. Pensez à brancher l'auto et à bien vous couvrir.")
    if j.get("rafales", 0) >= 70:
        out.append(f"Vents forts {quand}, avec des rafales jusqu'à {j['rafales']} km/h.")
    if j.get("precip_mm", 0) >= 20 and j.get("neige_cm", 0) < 5:
        out.append(f"Fortes pluies {quand} : environ {j['precip_mm']} mm.")
    return out


# --- Canadiens (API publique de la LNH) ------------------------------------

def nhl_info(equipe: str = "MTL") -> dict:
    r = httpx.get(f"https://api-web.nhle.com/v1/club-schedule-season/{equipe}/now", timeout=20, follow_redirects=True)
    r.raise_for_status()
    games = r.json().get("games", [])

    def nom(t):
        return (t.get("placeName") or {}).get("default") or (t.get("commonName") or {}).get("default") or t.get("abbrev", "?")

    def decrit(g):
        dom = g["homeTeam"].get("abbrev") == equipe
        nous, eux = (g["homeTeam"], g["awayTeam"]) if dom else (g["awayTeam"], g["homeTeam"])
        debut = datetime.fromisoformat(g["startTimeUTC"].replace("Z", "+00:00")).astimezone()
        d = {"id": g.get("id"), "adversaire": nom(eux), "domicile": dom, "debut": debut, "etat": g.get("gameState", "")}
        if "score" in nous and "score" in eux:
            d.update(nous=nous["score"], eux=eux["score"])
            fin = (g.get("gameOutcome") or {}).get("lastPeriodType", "")
            d["prolong"] = {"OT": " en prolongation", "SO": " en tirs de barrage"}.get(fin, "")
        return d

    finis = [decrit(g) for g in games if g.get("gameState") in ("FINAL", "OFF")]
    futurs = [decrit(g) for g in games if g.get("gameState") in ("FUT", "PRE")]
    live = [decrit(g) for g in games if g.get("gameState") in ("LIVE", "CRIT")]
    return {"dernier": finis[-1] if finis else None, "prochain": futurs[0] if futurs else None,
            "en_cours": live[0] if live else None}


def nhl_phrase(d: dict | None, type_: str) -> str:
    if not d:
        return ""
    ou = "à domicile" if d["domicile"] else "à l'extérieur"
    if type_ == "dernier":
        res = "a gagné" if d["nous"] > d["eux"] else "a perdu"
        return (f"Le Canadien {res} {max(d['nous'], d['eux'])} à {min(d['nous'], d['eux'])}{d.get('prolong', '')} "
                f"contre {d['adversaire']} ({fmt_day(d['debut'].date())}).")
    if type_ == "en_cours":
        return f"Match en cours contre {d['adversaire']} : {d.get('nous', 0)} à {d.get('eux', 0)} pour " + (
            "le Canadien." if d.get("nous", 0) > d.get("eux", 0) else "l'adversaire." if d.get("eux", 0) > d.get("nous", 0)
            else "personne, c'est égal.")
    return f"Prochain match : {fmt_when(d['debut'])} contre {d['adversaire']}, {ou}."


def nhl_texte(info: dict) -> str:
    return " ".join(x for x in (nhl_phrase(info.get("en_cours"), "en_cours"), nhl_phrase(info.get("dernier"), "dernier"),
                                nhl_phrase(info.get("prochain"), "prochain")) if x)


MOTS_HOCKEY = re.compile(r"canadien|\ble ch\b|\bhabs\b|hockey|\bmatch\b|\bgame\b", re.I)
MOTS_STOP = re.compile(r"^\s*(?:jarvis\W+)?(stop|arr[eê]te|tais[- ]toi|chut|silence|ça suffit|ca suffit|assez)\s*[.!]*\s*$", re.I)


# ---------------------------------------------------------------------------
# Jarvis (IA locale via Ollama)
# ---------------------------------------------------------------------------

cfg = tomllib.loads(CONFIG.read_text())
tg = cfg.get("channel", {}).get("telegram", {})
TOKEN = tg.get("bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED = [c.strip() for c in str(tg.get("allowed_chat_ids", "")).split(",") if c.strip()]
MODEL = cfg.get("intelligence", {}).get("default_model") or "qwen3.5:2b"

history: dict[int, deque] = defaultdict(lambda: deque(maxlen=10))


def system_prompt(extra: str = "") -> str:
    now = datetime.now().astimezone()
    base = (
        "Tu es Jarvis, l'assistant personnel de l'utilisateur. Tu réponds toujours en français, "
        "de façon naturelle, chaleureuse et assez courte (2 à 5 phrases), car ta réponse sera lue "
        "à voix haute. N'utilise pas de listes, d'emojis ni de mise en forme. "
        "Tu as accès à l'agenda, aux courriels, à la météo, aux nouvelles et à Internet : quand c'est "
        "utile, ces informations te sont fournies dans son message. Ne dis jamais que tu n'y as pas "
        "accès. Tu peux aussi créer des rappels (« rappelle-moi … »), retenir des choses (« souviens-toi que … ») "
        "et gérer des listes (« ajoute du lait à l'épicerie »). "
        f"Nous sommes {JOURS[now.weekday()]} {now.day} {MOIS[now.month - 1]} {now.year}, "
        f"il est {fmt_hour(now)}."
    )
    faits = memoire_texte()
    if faits:
        base += ("\n\nCe que tu sais sur l'utilisateur (notes qu'il t'a dictées : « je », « ma », « mon » = "
                 "l'utilisateur ; utilise-les naturellement quand c'est utile) :\n" + faits)
    if load_settings().get("majordome"):
        base += (" Tu t'exprimes comme un majordome britannique distingué, posé et pince-sans-rire : "
                 "tu vouvoies l'utilisateur et l'appelles « Monsieur », avec une pointe d'humour discret.")
    if extra:
        base += "\n\n" + extra
    return base


def clean(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"[*_`#>]", "", text)
    return text.strip()


_model_cache: dict = {}


async def pick_model(client: httpx.AsyncClient) -> str:
    """Le modèle de la config s'il est installé, sinon le premier disponible."""
    if "name" in _model_cache:
        return _model_cache["name"]
    r = await client.get(f"{OLLAMA}/api/tags")
    r.raise_for_status()
    names = [m.get("name", "") for m in r.json().get("models", [])]
    chosen = MODEL
    if MODEL not in names and f"{MODEL}:latest" not in names:
        if not names:
            raise RuntimeError("aucun modèle installé dans Ollama (essaie : ollama pull qwen3.5:2b)")
        chosen = names[0]
        print(f"Modèle {MODEL} absent, j'utilise {chosen}.", flush=True)
    _model_cache["name"] = chosen
    return chosen


async def llm(messages: list[dict]) -> str:
    async with httpx.AsyncClient(timeout=600) as client:
        body = {
            "model": await pick_model(client),
            "messages": messages,
            "stream": False,
            "think": False,
            "options": {"num_ctx": 8192, "temperature": 0.6},
            "keep_alive": "60m",  # garde l'IA chargée dans la carte graphique : réponses plus rapides
        }
        r = await client.post(f"{OLLAMA}/api/chat", json=body)
        if r.status_code == 400 and "think" in r.text:
            body.pop("think")
            r = await client.post(f"{OLLAMA}/api/chat", json=body)
        r.raise_for_status()
        return clean(r.json().get("message", {}).get("content", "") or "")


async def gather_context(question: str, s: dict) -> str:
    parts = []
    if s.get("ics_url") and MOTS_AGENDA.search(question):
        try:
            evs = await asyncio.to_thread(fetch_events, s, 7)
            parts.append("AGENDA (7 prochains jours) :\n" + events_text(evs))
        except Exception as exc:  # noqa: BLE001
            parts.append(f"AGENDA : impossible de le lire ({exc}).")
    if s.get("lat") and MOTS_METEO.search(question):
        try:
            parts.append(await asyncio.to_thread(weather_text, s))
        except Exception as exc:  # noqa: BLE001
            parts.append(f"MÉTÉO : impossible de la lire ({exc}).")
    if s.get("news_url") and MOTS_NOUVELLES.search(question):
        try:
            titres = await asyncio.to_thread(fetch_news, s, 8)
            parts.append("NOUVELLES DU JOUR (titres) :\n" + "\n".join(f"- {t}" for t in titres))
        except Exception as exc:  # noqa: BLE001
            parts.append(f"NOUVELLES : impossibles à lire ({exc}).")
    if MOTS_HOCKEY.search(question) and s.get("equipe", "MTL"):
        try:
            parts.append("CANADIENS DE MONTRÉAL : " + nhl_texte(await asyncio.to_thread(nhl_info, s.get("equipe", "MTL"))))
        except Exception as exc:  # noqa: BLE001
            parts.append(f"HOCKEY : résultats indisponibles ({exc}).")
    if re.search(r"\bliste|épicerie|epicerie|tâches|taches", question, re.I):
        parts.append("LISTES : " + "; ".join(f"{n} : {', '.join(v) or 'vide'}" for n, v in listes().items()) or "aucune")
    if MOTS_WEB.search(question) and not MOTS_HOCKEY.search(question):
        try:
            parts.append(f"RÉSULTATS DE RECHERCHE INTERNET pour « {question} » :\n"
                         + await asyncio.to_thread(web_search, question))
        except Exception as exc:  # noqa: BLE001
            parts.append(f"INTERNET : recherche impossible ({exc}).")
    if s.get("email") and MOTS_COURRIEL.search(question):
        try:
            mails = await asyncio.to_thread(fetch_emails, s, 8)
            parts.append("DERNIERS COURRIELS (du plus récent au plus ancien) :\n" + emails_text(mails))
        except Exception as exc:  # noqa: BLE001
            parts.append(f"COURRIELS : impossible de les lire ({exc}).")
    return "\n\n".join(parts)


async def _messages_pour(chat_id: int, question: str) -> list[dict]:
    s = load_settings()
    extra = await gather_context(question, s)
    content = question
    if extra:
        content = (
            "Voici mes vraies données (utilise-les pour me répondre, n'invente rien d'autre) :\n\n"
            f"{extra}\n\nMa question : {question}"
        )
    return [{"role": "system", "content": system_prompt()}, *history[chat_id], {"role": "user", "content": content}]


async def ask_jarvis(chat_id: int, question: str) -> str:
    messages = await _messages_pour(chat_id, question)
    answer = await llm(messages) or "Désolé, je n'ai pas de réponse."
    history[chat_id].append({"role": "user", "content": question})
    history[chat_id].append({"role": "assistant", "content": answer})
    return answer


async def llm_stream(messages: list[dict]):
    """Réponse de l'IA morceau par morceau (pour commencer à parler plus tôt)."""
    async with httpx.AsyncClient(timeout=600) as client:
        body = {"model": await pick_model(client), "messages": messages, "stream": True, "think": False,
                "options": {"num_ctx": 8192, "temperature": 0.6}, "keep_alive": "60m"}
        for essai in range(2):
            async with client.stream("POST", f"{OLLAMA}/api/chat", json=body) as r:
                if r.status_code == 400 and essai == 0:
                    await r.aread()
                    body.pop("think", None)
                    continue
                r.raise_for_status()
                dans_reflexion = False
                async for ligne in r.aiter_lines():
                    if not ligne.strip():
                        continue
                    morceau = json.loads(ligne).get("message", {}).get("content", "")
                    if "<think>" in morceau:
                        dans_reflexion = True
                    if dans_reflexion:
                        if "</think>" in morceau:
                            dans_reflexion = False
                            morceau = morceau.split("</think>", 1)[1]
                        else:
                            continue
                    if morceau:
                        yield morceau
                return


FIN_PHRASE = re.compile(r"(?<=[.!?…:;])\s+(?=[A-ZÀ-Ý«\"0-9])|\n+")


async def phrases(flux):
    """Regroupe un flux de texte en phrases (dès qu'une phrase est complète, on la donne)."""
    tampon = ""
    async for morceau in flux:
        tampon += morceau
        while True:
            m = FIN_PHRASE.search(tampon)
            if not m or m.start() < 12:
                break
            phrase, tampon = tampon[:m.start()], tampon[m.end():]
            if clean(phrase):
                yield clean(phrase)
    if clean(tampon):
        yield clean(tampon)


async def ask_jarvis_stream(chat_id: int, question: str):
    """Comme ask_jarvis, mais donne la réponse phrase par phrase."""
    messages = await _messages_pour(chat_id, question)
    toutes = []
    async for ph in phrases(llm_stream(messages)):
        toutes.append(ph)
        yield ph
    answer = " ".join(toutes) or "Désolé, je n'ai pas de réponse."
    if not toutes:
        yield answer
    history[chat_id].append({"role": "user", "content": question})
    history[chat_id].append({"role": "assistant", "content": answer})


# ---------------------------------------------------------------------------
# Voix
# ---------------------------------------------------------------------------

_whisper = None
_whisper_info = {"appareil": "pas encore chargé"}


def _precharger_cuda() -> bool:
    """Charge les bibliothèques CUDA installées par « jarvis_voix.py accelerer » (pip nvidia-*)."""
    import ctypes
    import glob
    import site

    libs = []
    for base in site.getsitepackages():
        for motif in ("nvidia/cublas/lib/libcublasLt.so*", "nvidia/cublas/lib/libcublas.so*",
                      "nvidia/cudnn/lib/libcudnn*.so*"):
            libs += sorted(glob.glob(os.path.join(base, motif)))
    ok = False
    for lib in libs:
        try:
            ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
            ok = True
        except OSError:
            pass
    return ok


def whisper():
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel

        if load_settings().get("whisper_gpu") and _precharger_cuda():
            try:
                print(f"Chargement de la reconnaissance vocale ({WHISPER_MODEL}) sur la carte graphique…", flush=True)
                _whisper = WhisperModel(WHISPER_MODEL, device="cuda", compute_type="float16")
                _whisper_info["appareil"] = "carte graphique (rapide)"
                return _whisper
            except Exception as exc:  # noqa: BLE001
                print(f"Carte graphique indisponible pour la voix ({exc}), j'utilise le processeur.", flush=True)
        print(f"Chargement de la reconnaissance vocale ({WHISPER_MODEL})…", flush=True)
        _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        _whisper_info["appareil"] = "processeur"
    return _whisper


def transcribe(path: str) -> str:
    segments, _ = whisper().transcribe(path, language="fr", beam_size=1 if _whisper_info["appareil"] == "processeur" else 5,
                                       vad_filter=True)
    return " ".join(seg.text.strip() for seg in segments).strip()


async def synth(text: str, out: str, s: dict | None = None) -> str:
    """Synthèse vocale avec la voix choisie (réglages), vers out (.ogg, .wav ou .mp3)."""
    import edge_tts

    s = load_settings() if s is None else s
    voix = s.get("voice") or VOICE
    with tempfile.TemporaryDirectory() as tmp:
        mp3 = os.path.join(tmp, "brut.mp3")
        await edge_tts.Communicate(text, voix, rate=s.get("voice_rate", "+0%"),
                                   pitch=s.get("voice_pitch", "+0Hz")).save(mp3)
        cmd = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", mp3]
        if s.get("voice_effect"):
            cmd += ["-af", EFFET_IA]
        if out.endswith(".ogg"):
            cmd += ["-c:a", "libopus", "-b:a", "48k"]
        await asyncio.to_thread(subprocess.run, cmd + [out], check=True)
    return out


async def speak(text: str, workdir: str) -> str:
    return await synth(text, os.path.join(workdir, "reponse.ogg"))


_pc_lock = asyncio.Lock()
_bg_tasks: set = set()
_lecture = {"gen": 0, "proc": None}


def stop_pc_voice() -> None:
    """Coupe la voix sur l'ordinateur (et annule les phrases en attente)."""
    _lecture["gen"] += 1
    proc = _lecture.get("proc")
    if proc is not None and proc.poll() is None:
        proc.terminate()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and _etat["v"] == "parle":
        async def repos_si_toujours_parle():
            if _etat["v"] == "parle":  # pas si on vient de passer à « j'écoute »
                await changer_etat("repos")
        task = loop.create_task(repos_si_toujours_parle())
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)


_win_temp_cache: dict = {}


def _win_temp() -> Path | None:
    """Dossier temporaire de Windows (vu depuis Ubuntu), pour que Windows lise le son sans souci."""
    if "dir" in _win_temp_cache:
        return _win_temp_cache["dir"]
    d = None
    if shutil.which("powershell.exe"):
        try:
            r = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                                "[Console]::OutputEncoding=[Text.Encoding]::UTF8; [IO.Path]::GetTempPath()"],
                               capture_output=True, text=True, timeout=30, cwd="/mnt/c" if os.path.isdir("/mnt/c") else None,
                               stdin=subprocess.DEVNULL)
            win = r.stdout.strip().splitlines()[-1].strip() if r.stdout.strip() else ""
            if win:
                lin = subprocess.run(["wslpath", "-u", win], capture_output=True, text=True).stdout.strip()
                if lin and os.path.isdir(lin):
                    d = Path(lin) / "jarvis-voix"
                    d.mkdir(exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            print(f"Dossier temporaire Windows introuvable ({exc}), j'utilise celui d'Ubuntu.", flush=True)
    _win_temp_cache["dir"] = d
    return d


def _play_cmd(wav: Path) -> tuple[list[str], str | None]:
    if shutil.which("powershell.exe"):
        win = subprocess.run(["wslpath", "-w", str(wav)], capture_output=True, text=True, check=True).stdout.strip()
        win = win.replace("'", "''")
        script = ("$ErrorActionPreference='Stop'; $p = New-Object Media.SoundPlayer '" + win + "'; "
                  "$p.Load(); $p.PlaySync()")
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], (
            "/mnt/c" if os.path.isdir("/mnt/c") else None)
    if shutil.which("paplay"):
        return ["paplay", str(wav)], None
    if shutil.which("aplay"):
        return ["aplay", "-q", str(wav)], None
    raise RuntimeError("aucun lecteur audio trouvé (ni Windows, ni paplay/aplay)")


# Les pages Jarvis ouvertes (pour leur dire « j'écoute », « je parle », etc.)
HUB: set = set()
_etat = {"v": "repos", "parle": 0}


async def diffuser(msg: dict) -> None:
    for ws in list(HUB):
        try:
            await ws.send_json(msg)
        except Exception:  # noqa: BLE001
            HUB.discard(ws)


async def changer_etat(v: str) -> None:
    if _etat["v"] != v:
        _etat["v"] = v
        await diffuser({"t": "etat", "v": v})


async def preparer_son(text: str, s: dict | None = None) -> Path:
    """Fabrique le fichier .wav à jouer sur l'ordinateur (voix, carillon ou bip)."""
    out_dir = await asyncio.to_thread(_win_temp) or Path(tempfile.gettempdir()) / "jarvis-voix"
    out_dir.mkdir(exist_ok=True)
    wav = out_dir / f"{time.time_ns()}.wav"
    sons = {
        "<carillon>": ["-f", "lavfi", "-i", "sine=f=660:d=0.12", "-f", "lavfi", "-i", "sine=f=880:d=0.12",
                       "-f", "lavfi", "-i", "sine=f=1320:d=0.35", "-filter_complex",
                       "[0][1][2]concat=n=3:v=0:a=1,afade=t=out:st=0.35:d=0.24,aecho=0.8:0.5:60:0.3,volume=0.5"],
        "<bip>": ["-f", "lavfi", "-i", "sine=f=880:d=0.09", "-f", "lavfi", "-i", "sine=f=1320:d=0.11",
                  "-filter_complex", "[0][1]concat=n=2:v=0:a=1,afade=t=out:st=0.12:d=0.08,volume=0.35"],
    }
    if text in sons:
        await asyncio.to_thread(subprocess.run, ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", *sons[text], str(wav)],
                                check=True)
    else:
        await synth(text, str(wav), s)
    return wav


async def jouer_son(wav: Path, gen: int, effet: bool = False) -> None:
    """Joue un .wav sur l'ordinateur, une phrase à la fois ; « stop » coupe tout.
    effet=True (bip, carillon) : ne change pas l'état affiché."""
    cmd, cwd = _play_cmd(wav)
    _etat["parle"] += 0 if effet else 1
    try:
        async with _pc_lock:
            if gen != _lecture["gen"]:
                return
            if not effet:
                await changer_etat("parle")
            proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    stdin=subprocess.DEVNULL, text=True)
            _lecture["proc"] = proc
            try:
                out, err = await asyncio.to_thread(proc.communicate, timeout=300)
            finally:
                _lecture["proc"] = None
        if proc.returncode != 0 and gen == _lecture["gen"]:
            raise RuntimeError(f"le lecteur audio a échoué : {(err or out).strip()[-400:]}")
    finally:
        wav.unlink(missing_ok=True)
        if not effet:
            _etat["parle"] -= 1
            if _etat["parle"] <= 0 and _etat["v"] == "parle":
                await changer_etat("repos")


async def speak_on_pc(text: str, wait: bool = False, s: dict | None = None) -> None:
    """Dit le texte sur les haut-parleurs de l'ordinateur (Windows via WSL, ou Linux).
    Avec wait=True, attend la fin et lève une erreur claire si le son n'a pas pu être joué."""
    gen = _lecture["gen"]
    effet = text.startswith("<") and text.endswith(">")
    wav = await preparer_son(text, s)
    if wait:
        await jouer_son(wav, gen, effet)
    else:
        async def play_logged():
            try:
                await jouer_son(wav, gen, effet)
            except Exception as exc:  # noqa: BLE001
                print(f"Voix sur l'ordinateur : {exc}", flush=True)
        task = asyncio.create_task(play_logged())
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)


async def dire_phrases(source) -> str:
    """Dit des phrases au fur et à mesure qu'elles arrivent : la phrase suivante est fabriquée
    pendant que la précédente est jouée. Retourne tout le texte."""
    gen = _lecture["gen"]
    fabriques: asyncio.Queue = asyncio.Queue()
    texte = []

    async def producteur():
        async for ph in source:
            texte.append(ph)
            await diffuser({"t": "jarvis", "texte": ph, "suite": len(texte) > 1})
            fabriques.put_nowait(asyncio.create_task(preparer_son(ph)))
        fabriques.put_nowait(None)

    prod = asyncio.create_task(producteur())
    _etat["parle"] += 1  # « je parle » pendant toute la réponse, même entre deux phrases
    try:
        while True:
            tache = await fabriques.get()
            if tache is None:
                break
            try:
                wav = await tache
                if gen == _lecture["gen"]:
                    await jouer_son(wav, gen)
                else:
                    wav.unlink(missing_ok=True)
            except Exception as exc:  # noqa: BLE001
                print(f"Voix sur l'ordinateur : {exc}", flush=True)
    finally:
        if not prod.done():
            prod.cancel()  # coupé (« stop » ou « Hey Jarvis ») : on arrête de lire la réponse de l'IA
        await asyncio.gather(prod, return_exceptions=True)
        while not fabriques.empty():
            t = fabriques.get_nowait()
            if t is not None:
                t.cancel()
        _etat["parle"] -= 1
        if _etat["parle"] <= 0 and _etat["v"] == "parle":
            await changer_etat("repos")
    return " ".join(texte)


async def say(bot, chat_id: int | str, text: str, reply_to=None) -> None:
    """Envoie le texte, la même chose en message vocal, et le dit sur l'ordinateur si activé."""
    if reply_to is not None:
        await reply_to.reply_text(text)
    else:
        await bot.send_message(chat_id, text)
    if load_settings().get("pc_voice"):
        try:
            await speak_on_pc(text)
        except Exception as exc:  # noqa: BLE001
            print(f"Voix sur l'ordinateur indisponible : {exc}", flush=True)
    try:
        await bot.send_chat_action(chat_id, "record_voice")
        with tempfile.TemporaryDirectory() as tmp:
            ogg = await speak(text, tmp)
            with open(ogg, "rb") as fh:
                await bot.send_voice(chat_id, fh)
    except Exception as exc:  # noqa: BLE001
        print(f"Voix indisponible : {exc}", flush=True)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

async def on_message(update, context) -> None:
    msg = update.effective_message
    chat_id = update.effective_chat.id
    if ALLOWED and str(chat_id) not in ALLOWED:
        return

    await context.bot.send_chat_action(chat_id, "typing")
    if msg.voice or msg.audio:
        with tempfile.TemporaryDirectory() as tmp:
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
        answer = await handle_question(question)
    except Exception as exc:  # noqa: BLE001
        await msg.reply_text(f"L'IA ne répond pas ({exc}). Est-ce qu'Ollama tourne ?")
        return
    await say(context.bot, chat_id, answer, reply_to=msg)


def conversation_id() -> int:
    """Même fil de discussion pour Telegram et la page de l'ordinateur."""
    return int(ALLOWED[0]) if ALLOWED and ALLOWED[0].lstrip("-").isdigit() else 0


async def handle_question(question: str) -> str:
    """Stop, rappel, mémoire, listes, sinon question à Jarvis. Retourne le texte de la réponse."""
    r = intention(question)
    return r if r is not None else await ask_jarvis(conversation_id(), question)


def intention(question: str) -> str | None:
    """Les commandes qui n'ont pas besoin de l'IA (stop, rappels, mémoire, listes). None sinon."""
    if MOTS_STOP.match(question):
        stop_pc_voice()
        return "D'accord."
    if MOTS_RAPPEL.search(question) and not MEM_AJOUT.match(question):
        parsed = parse_rappel(question)
        if parsed is None:
            return "Je veux bien, mais pour quand ? Dis par exemple : rappelle-moi demain à 9 h d'appeler maman."
        when, what = parsed
        add_reminder(when, what)
        return f"C'est noté : je te rappellerai {fmt_when(when)} : {what}."
    if m := MEM_AJOUT.match(question):
        fait = memoire_ajoute(m.group("fait"))
        return f"C'est noté, je m'en souviendrai : {deuxieme_personne(fait)}."
    if m := MEM_OUBLI.match(question):
        enleves = memoire_oublie(m.group("quoi"))
        return ("C'est oublié : " + " ; ".join(map(deuxieme_personne, enleves)) + ".") if enleves else \
            "Je n'avais rien retenu là-dessus."
    if MEM_LISTE.search(question) and re.search(r"\bmoi\b|retenu|mémoire|memoire", question, re.I):
        faits = [i["fait"] for i in memoire()]
        return ("Voici ce que je sais sur toi : " + " ; ".join(map(deuxieme_personne, faits)) + ".") if faits else \
            "Je ne sais encore rien sur toi. Dis-moi par exemple : souviens-toi que j'adore le café."
    if (rep_liste := gerer_listes(question)) is not None:
        return rep_liste
    return None


async def cmd_courriels(update, context) -> None:
    chat_id = update.effective_chat.id
    if ALLOWED and str(chat_id) not in ALLOWED:
        return
    s = load_settings()
    if not s.get("email"):
        await update.effective_message.reply_text("Courriel pas branché : lance « jarvis_voix.py reglages ».")
        return
    try:
        mails = await asyncio.to_thread(fetch_emails, s, 5)
    except Exception as exc:  # noqa: BLE001
        await update.effective_message.reply_text(f"Impossible de lire tes courriels : {exc}")
        return
    if not mails:
        await say(context.bot, chat_id, "Ta boîte de réception est vide.")
        return
    lignes = [f"De {m['from']}, {fmt_when(m['date']) if m['date'] else ''} : {m['subject']}." for m in mails]
    nonlus = sum(m["unread"] for m in mails)
    await say(context.bot, chat_id, f"Tes {len(mails)} derniers courriels, dont {nonlus} non lus. " + " ".join(lignes))


async def cmd_agenda(update, context) -> None:
    chat_id = update.effective_chat.id
    if ALLOWED and str(chat_id) not in ALLOWED:
        return
    s = load_settings()
    if not s.get("ics_url"):
        await update.effective_message.reply_text("Agenda pas branché : lance « jarvis_voix.py reglages ».")
        return
    try:
        evs = await asyncio.to_thread(fetch_events, s, 7, False)
    except Exception as exc:  # noqa: BLE001
        await update.effective_message.reply_text(f"Impossible de lire ton agenda : {exc}")
        return
    now = datetime.now().astimezone()
    evs = [e for e in evs if e["all_day"] or e["start"] >= now]
    if not evs:
        await say(context.bot, chat_id, "Tu n'as aucun rendez-vous dans les 7 prochains jours.")
        return
    await say(context.bot, chat_id, "Tes prochains rendez-vous : " + " ; ".join(describe_event(e) for e in evs[:10]) + ".")


def _allowed(update) -> bool:
    return not ALLOWED or str(update.effective_chat.id) in ALLOWED


async def cmd_rappels(update, context) -> None:
    if not _allowed(update):
        return
    items = load_reminders()
    if not items:
        await update.effective_message.reply_text("Aucun rappel prévu. Dis-moi « rappelle-moi … » pour en créer un.")
        return
    lignes = [f"{i}. {fmt_when(datetime.fromisoformat(r['when']))} : {r['what']}" for i, r in enumerate(items, 1)]
    await update.effective_message.reply_text("⏰ Tes rappels :\n" + "\n".join(lignes)
                                              + "\n\nPour en effacer un : /effacer suivi du numéro (ex. /effacer 2)")


async def cmd_effacer(update, context) -> None:
    if not _allowed(update):
        return
    items = load_reminders()
    try:
        n = int(context.args[0])
        r = items.pop(n - 1)
    except (IndexError, ValueError):
        await update.effective_message.reply_text("Écris /effacer suivi du numéro du rappel (voir /rappels).")
        return
    save_reminders(items)
    await update.effective_message.reply_text(f"Rappel effacé : {r['what']}.")


async def cmd_meteo(update, context) -> None:
    if not _allowed(update):
        return
    s = load_settings()
    if not s.get("lat"):
        await update.effective_message.reply_text("Météo pas branchée : lance « jarvis_voix.py reglages ».")
        return
    try:
        data = await asyncio.to_thread(weather_text, s)
        text = await llm([{"role": "system", "content": system_prompt()},
                          {"role": "user", "content": data + "\n\nRésume-moi la météo d'aujourd'hui et de demain."}])
    except Exception as exc:  # noqa: BLE001
        await update.effective_message.reply_text(f"Météo impossible : {exc}")
        return
    await say(context.bot, update.effective_chat.id, text or data)


async def cmd_nouvelles(update, context) -> None:
    if not _allowed(update):
        return
    s = load_settings()
    try:
        titres = await asyncio.to_thread(fetch_news, s, 5)
    except Exception as exc:  # noqa: BLE001
        await update.effective_message.reply_text(f"Nouvelles impossibles : {exc}")
        return
    await say(context.bot, update.effective_chat.id, "Les titres du moment : " + " ; ".join(titres) + ".")


async def cmd_cherche(update, context) -> None:
    if not _allowed(update):
        return
    q = " ".join(context.args)
    if not q:
        await update.effective_message.reply_text("Écris /cherche suivi de ta question (ex. /cherche résultat du Canadien hier).")
        return
    await context.bot.send_chat_action(update.effective_chat.id, "typing")
    try:
        res = await asyncio.to_thread(web_search, q)
        text = await llm([{"role": "system", "content": system_prompt()},
                          {"role": "user", "content": f"Résultats de recherche Internet :\n{res}\n\n"
                                                      f"Réponds à ma question en t'appuyant dessus : {q}"}])
    except Exception as exc:  # noqa: BLE001
        await update.effective_message.reply_text(f"Recherche impossible : {exc}")
        return
    await say(context.bot, update.effective_chat.id, text or res)


async def cmd_recap(update, context) -> None:
    if not _allowed(update):
        return
    await context.bot.send_chat_action(update.effective_chat.id, "typing")
    try:
        texte = await recap_text(await collect_overview(load_settings()))
    except Exception as exc:  # noqa: BLE001
        await update.effective_message.reply_text(f"Récap impossible : {exc}")
        return
    await say(context.bot, update.effective_chat.id, texte)


async def _repondre_cmd(update, context, texte: str, voix: bool = True) -> None:
    if voix:
        await say(context.bot, update.effective_chat.id, texte)
    else:
        await update.effective_message.reply_text(texte)


async def cmd_memoire(update, context) -> None:
    if not _allowed(update):
        return
    items = memoire()
    if not items:
        await _repondre_cmd(update, context, "Je ne sais encore rien sur toi. Dis-moi : « souviens-toi que … ».", False)
        return
    await _repondre_cmd(update, context, "🧠 Ce que je sais sur toi :\n" + "\n".join(
        f"{i}. {x['fait']}" for i, x in enumerate(items, 1)) + "\n\nPour en effacer un : /oublie suivi du numéro.", False)


async def cmd_oublie(update, context) -> None:
    if not _allowed(update):
        return
    items = memoire()
    try:
        x = items.pop(int(context.args[0]) - 1)
    except (IndexError, ValueError):
        await _repondre_cmd(update, context, "Écris /oublie suivi du numéro (voir /memoire).", False)
        return
    _save(MEMOIRE, items)
    await _repondre_cmd(update, context, f"C'est oublié : {x['fait']}.", False)


async def cmd_liste(update, context) -> None:
    if not _allowed(update):
        return
    nom = " ".join(context.args).strip().lower() or None
    data = listes()
    if not nom:
        if not any(data.values()):
            await _repondre_cmd(update, context, "Tes listes sont vides. Dis : « ajoute du lait à l'épicerie ».", False)
            return
        await _repondre_cmd(update, context, "\n\n".join(f"📝 {n} :\n" + "\n".join(f"• {i}" for i in v)
                                                          for n, v in data.items() if v), False)
        return
    await _repondre_cmd(update, context, liste_texte("épicerie" if "pic" in nom else "tâches" if "tâch" in nom
                                                     or "tach" in nom else nom), False)


async def cmd_epicerie(update, context) -> None:
    if _allowed(update):
        await _repondre_cmd(update, context, liste_texte("épicerie"), False)


async def cmd_taches(update, context) -> None:
    if _allowed(update):
        await _repondre_cmd(update, context, liste_texte("tâches"), False)


async def cmd_stop(update, context) -> None:
    if _allowed(update):
        stop_pc_voice()
        await update.effective_message.reply_text("🤫 D'accord.")


async def cmd_canadiens(update, context) -> None:
    if not _allowed(update):
        return
    try:
        texte = nhl_texte(await asyncio.to_thread(nhl_info, load_settings().get("equipe", "MTL")))
    except Exception as exc:  # noqa: BLE001
        await update.effective_message.reply_text(f"Résultats indisponibles : {exc}")
        return
    await _repondre_cmd(update, context, "🏒 " + (texte or "Pas de match trouvé."))


async def cmd_aide(update, context) -> None:
    await update.effective_message.reply_text(
        "Je suis Jarvis 🤖\n\n"
        "• Parle-moi en vocal 🎤 ou écris-moi.\n"
        "• /recap — tout ce qui se passe, en résumé\n"
        "• /agenda — tes prochains rendez-vous\n"
        "• /courriels — tes derniers courriels\n"
        "• /meteo — la météo\n"
        "• /nouvelles — les titres du moment\n"
        "• /cherche … — chercher sur Internet\n"
        "• /rappels — tes rappels (et /effacer N)\n"
        "• /liste, /epicerie, /taches — tes listes\n"
        "• /memoire — ce que je sais sur toi (et /oublie N)\n"
        "• /canadiens — résultats et prochain match\n"
        "• /stop — me faire taire sur l'ordi\n\n"
        "Exemples : « Qu'est-ce que j'ai demain ? », « Lis-moi mes courriels », "
        "« Est-ce qu'il va neiger demain ? », « Rappelle-moi dans 20 minutes de sortir le linge », "
        "« Souviens-toi que ma blonde s'appelle Julie », « Ajoute du lait et des œufs à l'épicerie »."
    )


async def collect_overview(s: dict) -> dict:
    """Tout ce qui se passe, en parallèle : agenda, courriels, météo, rappels, nouvelles."""
    now = datetime.now().astimezone()
    out: dict = {"erreurs": {}}

    async def agenda():
        evs = await asyncio.to_thread(fetch_events, s, 1)
        tomorrow = now.date() + timedelta(days=1)
        out["agenda"] = [
            {"quand": describe_event(e).split(" : ")[0], "titre": e["title"], "lieu": e["where"]}
            for e in evs
            if (e["all_day"] and e["start"].date() >= now.date()) or (not e["all_day"] and e["start"] >= now)
            if e["start"].date() <= tomorrow
        ]

    async def courriels():
        mails = await asyncio.to_thread(fetch_emails, s, 15)
        out["courriels"] = [{"de": m["from"], "objet": m["subject"], "categorie": m.get("categorie", "normal"),
                             "quand": fmt_when(m["date"]) if m["date"] else "", "debut": m["body"][:200]}
                            for m in mails if m["unread"] and m.get("categorie") != "pub"]
        out["pubs"] = sum(1 for m in mails if m["unread"] and m.get("categorie") == "pub")

    async def meteo():
        out["meteo"] = await asyncio.to_thread(weather_data, s)

    async def nouvelles():
        out["nouvelles"] = await asyncio.to_thread(fetch_news, s, 4)

    async def hockey():
        out["hockey"] = nhl_texte(await asyncio.to_thread(nhl_info, s.get("equipe", "MTL")))

    jobs = {"agenda": (s.get("ics_url"), agenda), "courriels": (s.get("email"), courriels),
            "meteo": (s.get("lat"), meteo), "nouvelles": (s.get("news_url"), nouvelles),
            "hockey": (s.get("equipe", "MTL"), hockey)}
    actifs = [(nom, fn) for nom, (ok, fn) in jobs.items() if ok]
    results = await asyncio.gather(*(fn() for _, fn in actifs), return_exceptions=True)
    for (nom, _), res in zip(actifs, results):
        if isinstance(res, Exception):
            out["erreurs"][nom] = str(res)

    limite = now + timedelta(hours=36)
    out["rappels"] = [{"quand": fmt_when(datetime.fromisoformat(r["when"])), "quoi": r["what"]}
                      for r in load_reminders() if datetime.fromisoformat(r["when"]) <= limite]
    out["heure"] = fmt_hour(now)
    out["listes"] = {n: v for n, v in listes().items() if v}
    out["anniversaires"] = [{"nom": a["nom"], "quand": fmt_day(a["date"])} for a in anniversaires_proches(7)]
    if "meteo" in out:
        out["alertes"] = alertes_meteo(out["meteo"], 0) + alertes_meteo(out["meteo"], 1)
    return out


def overview_text(o: dict) -> str:
    parts = []
    if "meteo" in o:
        w = o["meteo"]
        parts.append(f"MÉTÉO à {w['ville']} : {w['temp']} °C (ressenti {w['ressenti']} °C), {w['ciel']}. "
                     + " ".join(f"{j['nom']} : {j['ciel']}, {j['min']} à {j['max']} °C." for j in w["jours"][:2]))
    if "agenda" in o:
        parts.append("PROCHAINS RENDEZ-VOUS (aujourd'hui et demain) :\n"
                     + ("\n".join(f"- {e['quand']} : {e['titre']}" + (f" ({e['lieu']})" if e["lieu"] else "")
                                  for e in o["agenda"]) or "(aucun)"))
    if o.get("rappels") is not None:
        parts.append("RAPPELS À VENIR :\n" + ("\n".join(f"- {r['quand']} : {r['quoi']}" for r in o["rappels"])
                                             or "(aucun)"))
    if o.get("alertes"):
        parts.append("ALERTES MÉTÉO :\n" + "\n".join(f"- {a}" for a in o["alertes"]))
    if "courriels" in o:
        parts.append(f"COURRIELS NON LUS ({len(o['courriels'])}, sans compter {o.get('pubs', 0)} publicités) :\n"
                     + ("\n".join(f"- {'[IMPORTANT] ' if m.get('categorie') == 'important' else ''}De {m['de']} : "
                                  f"{m['objet']}" for m in o["courriels"][:8]) or "(aucun)"))
    if o.get("anniversaires"):
        parts.append("ANNIVERSAIRES À VENIR :\n" + "\n".join(f"- {a['nom']} : {a['quand']}" for a in o["anniversaires"]))
    if o.get("listes"):
        parts.append("LISTES : " + " ; ".join(f"{n} ({len(v)}) : {', '.join(v[:8])}" for n, v in o["listes"].items()))
    if o.get("hockey"):
        parts.append("CANADIENS : " + o["hockey"])
    if "nouvelles" in o:
        parts.append("NOUVELLES :\n" + "\n".join(f"- {t}" for t in o["nouvelles"]))
    return "\n\n".join(parts)


async def recap_text(o: dict, matin: bool = False, soir: bool = False) -> str:
    h = datetime.now().astimezone().hour
    salut = "Bonjour" if h < 12 else "Bon après-midi" if h < 18 else "Bonsoir"
    consigne = (
        f"Fais-moi un récap de ce qui se passe (il est {o['heure']}). Commence par « {salut} », puis en 4 à 7 "
        "phrases naturelles : la météo, mes prochains rendez-vous et rappels, les courriels non lus importants "
        "(qui et à propos de quoi) et 2 nouvelles. Ne mentionne pas ce qui est vide."
    )
    if matin:
        consigne = "C'est mon résumé du matin, souhaite-moi bonne journée. " + consigne
    if soir:
        consigne = (f"C'est mon résumé du soir (il est {o['heure']}). Commence par « Bonsoir », puis en 4 à 6 phrases : "
                    "ce qui m'attend DEMAIN (rendez-vous, rappels, météo et alertes), les anniversaires proches, "
                    "les courriels importants non lus et ce qui reste sur mes listes. Termine par une phrase "
                    "pour me souhaiter une bonne soirée. Ne mentionne pas ce qui est vide.")
    text = await llm([
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": "Voici mes vraies données :\n\n" + overview_text(o) + "\n\n" + consigne},
    ])
    return text or f"{salut} ! Rien de spécial à te signaler pour le moment."


async def morning_brief(bot, chat_id: str, s: dict) -> None:
    o = await collect_overview(s)
    await say(bot, chat_id, await recap_text(o, matin=True))


async def watcher(app) -> None:
    """Boucle de fond : rappels de rendez-vous, nouveaux courriels, résumé du matin."""
    if not ALLOWED:
        print("Pas d'Id Telegram dans la config : rappels désactivés.", flush=True)
        return
    chat_id = ALLOWED[0]
    reminded: set[str] = set()
    seen_uid = 0
    last_morning = None
    last_mail_check = 0.0
    last_nhl_check = 0.0
    etat = _load(ETAT, {})

    while True:
        s = load_settings()
        now = datetime.now().astimezone()
        try:
            if s.get("ics_url"):
                before = timedelta(minutes=int(s.get("reminder_minutes", 30)))
                for e in await asyncio.to_thread(fetch_events, s, 1):
                    key = f"{e['uid']}|{e['start'].isoformat()}"
                    if e["all_day"] or key in reminded:
                        continue
                    if now <= e["start"] <= now + before:
                        reminded.add(key)
                        mins = max(1, round((e["start"] - now).total_seconds() / 60))
                        where = f", à {e['where']}" if e["where"] else ""
                        await say(app.bot, chat_id,
                                  f"Petit rappel : {e['title']} à {fmt_hour(e['start'])}{where}, "
                                  f"dans {mins} minute{'s' if mins > 1 else ''}.")
        except Exception as exc:  # noqa: BLE001
            print(f"Agenda : {exc}", flush=True)

        try:
            items = load_reminders()
            due = [r for r in items if datetime.fromisoformat(r["when"]) <= now]
            if due:
                save_reminders([r for r in items if r not in due])
                for r in due:
                    await say(app.bot, chat_id, f"⏰ Rappel : {r['what']}.")
        except Exception as exc:  # noqa: BLE001
            print(f"Rappels : {exc}", flush=True)

        try:
            if s.get("email") and s.get("notify_new_email", True) and time.time() - last_mail_check > 180:
                last_mail_check = time.time()
                if not seen_uid:
                    seen_uid = await asyncio.to_thread(last_uid, s)
                else:
                    new = await asyncio.to_thread(fetch_emails, s, 5, seen_uid)
                    if new:
                        seen_uid = max(m["uid"] for m in new)
                        for m in reversed(new):
                            if m.get("categorie") == "pub" and not s.get("annoncer_pubs"):
                                continue
                            pre = "Courriel important" if m.get("categorie") == "important" else "Nouveau courriel"
                            await say(app.bot, chat_id, f"{pre} de {m['from']} : {m['subject']}.")
        except Exception as exc:  # noqa: BLE001
            print(f"Courriel : {exc}", flush=True)

        try:
            hm = s.get("morning", "")
            if re.fullmatch(r"\d{1,2}:\d{2}", hm or "") and last_morning != now.date():
                h, m = map(int, hm.split(":"))
                target = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if target <= now < target + timedelta(minutes=30):
                    last_morning = now.date()
                    await morning_brief(app.bot, chat_id, s)
                elif now >= target + timedelta(minutes=30):
                    last_morning = now.date()  # trop tard pour aujourd'hui
        except Exception as exc:  # noqa: BLE001
            print(f"Résumé du matin : {exc}", flush=True)

        aujourd = now.date().isoformat()

        def a_l_heure(cle: str, defaut: str) -> bool:
            """Vrai une seule fois par jour, dans la demi-heure qui suit l'heure réglée."""
            hm = s.get(cle, defaut)
            if not re.fullmatch(r"\d{1,2}:\d{2}", hm or "") or etat.get(cle) == aujourd:
                return False
            h, m = map(int, hm.split(":"))
            cible = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if cible <= now < cible + timedelta(minutes=30):
                etat[cle] = aujourd
                _save(ETAT, etat)
                return True
            return False

        try:  # résumé du soir
            if a_l_heure("soir", "21:00"):
                await say(app.bot, chat_id, await recap_text(await collect_overview(s), soir=True))
        except Exception as exc:  # noqa: BLE001
            print(f"Résumé du soir : {exc}", flush=True)

        try:  # alertes météo pour demain
            if s.get("lat") and a_l_heure("heure_alertes", "18:00"):
                alertes = alertes_meteo(await asyncio.to_thread(weather_data, s), 1)
                if alertes:
                    await say(app.bot, chat_id, "Alerte météo : " + " ".join(alertes))
        except Exception as exc:  # noqa: BLE001
            print(f"Alertes météo : {exc}", flush=True)

        try:  # anniversaires (le jour même, à 9 h)
            if a_l_heure("heure_anniv", "09:00"):
                for a in anniversaires_proches(0):
                    await say(app.bot, chat_id, f"🎂 C'est l'anniversaire de {a['nom']} aujourd'hui ! "
                                                "Pense à lui souhaiter.")
        except Exception as exc:  # noqa: BLE001
            print(f"Anniversaires : {exc}", flush=True)

        try:  # résultat du Canadien dès la fin du match
            if s.get("equipe", "MTL") and time.time() - last_nhl_check > 600:
                last_nhl_check = time.time()
                info = await asyncio.to_thread(nhl_info, s.get("equipe", "MTL"))
                d = info.get("dernier")
                if d and d["id"] != etat.get("nhl_annonce"):
                    if etat.get("nhl_annonce") is not None and now - d["debut"] < timedelta(hours=12):
                        await say(app.bot, chat_id, "🏒 " + nhl_phrase(d, "dernier"))
                    etat["nhl_annonce"] = d["id"]
                    _save(ETAT, etat)
        except Exception as exc:  # noqa: BLE001
            print(f"Canadiens : {exc}", flush=True)

        await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# Page « Jarvis » sur l'ordinateur : parler avec le micro du PC
# ---------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jarvis</title>
<style>
  :root { --bg:#03070f; --text:#d8f6ff; --muted:#6f9bb0; --cyan:#3fd8ff; --cyan-dim:#1b6f8a; --amber:#ffb547;
          --white:#e9fdff; --panel:#071524cc; --line:#123446; --niveau:0; }
  * { box-sizing:border-box; }
  html, body { height:100%; }
  body { margin:0; background:radial-gradient(ellipse at 50% 30%, #082036 0%, var(--bg) 62%); color:var(--text);
         font:15px/1.45 "Segoe UI", system-ui, sans-serif; display:flex; flex-direction:column; align-items:center; overflow-x:hidden; }
  body::before { content:""; position:fixed; inset:0; pointer-events:none; opacity:.25;
         background:repeating-linear-gradient(0deg, transparent 0 3px, #0d3a4f22 3px 4px); }
  header { width:100%; max-width:900px; padding:16px 16px 0; display:flex; justify-content:space-between; align-items:flex-start; gap:12px; }
  .logo { font-weight:600; letter-spacing:.42em; font-size:18px; color:var(--cyan); text-shadow:0 0 12px #3fd8ff88; }
  .sous { color:var(--muted); font-size:11px; letter-spacing:.25em; text-transform:uppercase; margin-top:4px; }
  .horloge { text-align:right; font-variant-numeric:tabular-nums; }
  .horloge b { font-size:22px; font-weight:500; color:var(--white); letter-spacing:.05em; }
  .horloge div { color:var(--muted); font-size:12px; text-transform:capitalize; }
  #reacteur { margin:6px 0 0; width:min(340px, 82vw); aspect-ratio:1; border:none; background:none; padding:0; cursor:pointer;
              position:relative; -webkit-tap-highlight-color:transparent; }
  #reacteur:focus-visible { outline:2px solid var(--cyan); outline-offset:4px; border-radius:50%; }
  #reacteur svg { width:100%; height:100%; overflow:visible; }
  .anneau { transform-origin:200px 200px; }
  .a1 { animation:tourne 40s linear infinite; } .a2 { animation:tourne 24s linear infinite reverse; }
  .a3 { animation:tourne 14s linear infinite; } .a4 { animation:tourne 60s linear infinite reverse; }
  @keyframes tourne { to { transform:rotate(360deg); } }
  .trait { stroke:var(--cyan); fill:none; transition:stroke .4s, opacity .4s; }
  #coeur { transform-origin:200px 200px; transform:scale(calc(1 + var(--niveau) * .35)); transition:transform .08s linear, fill .4s; fill:url(#lueur); }
  #halo { transform-origin:200px 200px; opacity:calc(.25 + var(--niveau) * .9); transition:opacity .08s; }
  body.repos .trait { opacity:.55; } body.repos #coeur { animation:respire 4s ease-in-out infinite; }
  @keyframes respire { 50% { transform:scale(1.04); } }
  body.ecoute .trait, body.suivi .trait { stroke:var(--cyan); opacity:1; filter:drop-shadow(0 0 6px #3fd8ff); }
  body.pense .trait { stroke:var(--amber); opacity:.95; } body.pense .a2 { animation-duration:2.5s; } body.pense .a3 { animation-duration:1.6s; }
  body.pense #coeur { fill:url(#lueurAmbre); animation:respire 1s ease-in-out infinite; }
  body.parle .trait { stroke:var(--white); opacity:1; filter:drop-shadow(0 0 8px #bff4ff); }
  body.parle #coeur { animation:voix .35s ease-in-out infinite alternate; }
  @keyframes voix { from { transform:scale(.96); } to { transform:scale(1.14); } }
  body.reveil #reacteur { animation:flash .5s ease-out; } @keyframes flash { 40% { filter:brightness(1.8); } }
  #statut { margin:2px 0 4px; color:var(--cyan); letter-spacing:.3em; font-size:12px; text-transform:uppercase; min-height:1.4em; text-align:center; }
  #aide { color:var(--muted); font-size:13px; margin:0 16px 10px; text-align:center; min-height:1.3em; }
  .commandes { display:flex; gap:8px; flex-wrap:wrap; justify-content:center; margin-bottom:10px; }
  .pilule { border:1px solid var(--line); background:var(--panel); color:var(--text); border-radius:999px; padding:6px 12px;
            font-size:13px; cursor:pointer; }
  .pilule[aria-pressed="true"] { border-color:var(--cyan); color:var(--cyan); box-shadow:0 0 10px #3fd8ff44 inset; }
  .pilule.alerte { border-color:var(--amber); color:var(--amber); }
  #fil { width:100%; max-width:760px; padding:0 16px; flex:1; display:flex; flex-direction:column; gap:8px; overflow-y:auto; min-height:120px; }
  .bulle { max-width:88%; padding:9px 13px; border-radius:4px 14px 14px 14px; background:var(--panel); border:1px solid var(--line);
           white-space:pre-wrap; animation:entre .25s ease-out; }
  .bulle.moi { align-self:flex-end; border-radius:14px 4px 14px 14px; border-color:#1d4f66; background:#0b2236cc; }
  .bulle.erreur { border-color:var(--amber); color:var(--amber); }
  .bulle .qui { display:block; font-size:10px; letter-spacing:.2em; color:var(--muted); text-transform:uppercase; margin-bottom:2px; }
  @keyframes entre { from { opacity:0; transform:translateY(6px); } }
  form { width:100%; max-width:760px; padding:12px 16px 18px; display:flex; gap:8px; }
  input { flex:1; min-width:0; padding:11px 13px; border-radius:10px; border:1px solid var(--line); background:#06111dcc; color:var(--text); font-size:15px; }
  input:focus { outline:1px solid var(--cyan); }
  form button { padding:0 16px; border-radius:10px; border:1px solid var(--cyan); background:#0b3346; color:var(--cyan); font-weight:600; cursor:pointer; }
  @media (prefers-reduced-motion: reduce) { .anneau, #coeur { animation:none !important; } }
</style></head>
<body class="repos">
<header>
  <div><div class="logo">J.A.R.V.I.S.</div><div class="sous" id="sous">Systèmes en ligne</div></div>
  <div class="horloge"><b id="heure">--:--</b><div id="date"></div></div>
</header>
<button id="reacteur" aria-label="Parler à Jarvis (ou le faire taire s'il parle)">
<svg viewBox="0 0 400 400" aria-hidden="true">
  <defs>
    <radialGradient id="lueur"><stop offset="0" stop-color="#ffffff"/><stop offset=".35" stop-color="#9ff0ff"/>
      <stop offset=".7" stop-color="#2bbfe8" stop-opacity=".55"/><stop offset="1" stop-color="#0a4a66" stop-opacity="0"/></radialGradient>
    <radialGradient id="lueurAmbre"><stop offset="0" stop-color="#fff"/><stop offset=".4" stop-color="#ffd08a"/>
      <stop offset=".75" stop-color="#ff9d2e" stop-opacity=".5"/><stop offset="1" stop-color="#5a3000" stop-opacity="0"/></radialGradient>
    <radialGradient id="flou"><stop offset="0" stop-color="#3fd8ff" stop-opacity=".35"/><stop offset="1" stop-color="#3fd8ff" stop-opacity="0"/></radialGradient>
  </defs>
  <circle id="halo" cx="200" cy="200" r="190" fill="url(#flou)"/>
  <g class="anneau a4"><circle class="trait" cx="200" cy="200" r="186" stroke-width="1" stroke-dasharray="2 10"/></g>
  <g class="anneau a1"><circle class="trait" cx="200" cy="200" r="168" stroke-width="3" stroke-dasharray="60 18 8 18"/></g>
  <g class="anneau a2"><circle class="trait" cx="200" cy="200" r="140" stroke-width="10" stroke-dasharray="30 12" opacity=".8"/></g>
  <g class="anneau a3"><circle class="trait" cx="200" cy="200" r="112" stroke-width="2" stroke-dasharray="120 40 10 40"/>
     <circle class="trait" cx="200" cy="200" r="100" stroke-width="1"/></g>
  <circle id="coeur" cx="200" cy="200" r="78"/>
</svg></button>
<div id="statut">En veille</div>
<p id="aide">Dites « Hey Jarvis », cliquez sur le réacteur ou appuyez sur Espace.</p>
<div class="commandes">
  <button class="pilule" id="mainsLibres" aria-pressed="true">👂 « Hey Jarvis » activé</button>
  <button class="pilule" id="micro" hidden>🎤 Activer le micro</button>
  <button class="pilule" id="taire">🤫 Silence</button>
</div>
<div id="fil" aria-live="polite"></div>
<form id="formulaire"><input id="texte" placeholder="Écrire à Jarvis…" autocomplete="off"><button>Envoyer</button></form>
<script>
const $ = id => document.getElementById(id), corps = document.body, fil = $('fil');
const libelles = { repos: 'En veille', ecoute: "J'écoute…", suivi: "Je vous écoute encore…", pense: 'Analyse en cours…', parle: 'Jarvis parle' };
let ws = null, flux = null, rec = null, etat = 'repos', dernierJarvis = null, mainsLibres = true;
try { mainsLibres = localStorage.getItem('jarvis-mains-libres') !== 'non'; } catch (e) {}

function horloge() { const d = new Date();
  $('heure').textContent = d.toLocaleTimeString('fr-CA', { hour: '2-digit', minute: '2-digit' });
  $('date').textContent = d.toLocaleDateString('fr-CA', { weekday: 'long', day: 'numeric', month: 'long' }); }
horloge(); setInterval(horloge, 10000);

function bulle(texte, qui, suite) {
  if (suite && qui === 'jarvis' && dernierJarvis) { dernierJarvis.lastChild.textContent += ' ' + texte; fil.scrollTop = fil.scrollHeight; return; }
  const d = document.createElement('div'); d.className = 'bulle ' + (qui === 'moi' ? 'moi' : qui === 'erreur' ? 'erreur' : '');
  const q = document.createElement('span'); q.className = 'qui'; q.textContent = qui === 'moi' ? 'Vous' : qui === 'erreur' ? 'Erreur' : 'Jarvis';
  d.appendChild(q); d.appendChild(document.createTextNode(texte)); fil.appendChild(d);
  while (fil.children.length > 40) fil.firstChild.remove();
  fil.scrollTop = fil.scrollHeight; dernierJarvis = qui === 'jarvis' ? d : null; }
function changeEtat(v) { etat = v; corps.className = v; $('statut').textContent = libelles[v] || v;
  if (v !== 'ecoute' && v !== 'suivi') corps.style.setProperty('--niveau', 0); }
function majMainsLibres() { $('mainsLibres').setAttribute('aria-pressed', mainsLibres);
  $('mainsLibres').textContent = mainsLibres ? '👂 « Hey Jarvis » activé' : '👂 « Hey Jarvis » désactivé';
  $('aide').textContent = mainsLibres ? 'Dites « Hey Jarvis », cliquez sur le réacteur ou appuyez sur Espace.' : 'Cliquez sur le réacteur ou appuyez sur Espace pour parler.';
  envoyer({ t: 'mains_libres', v: mainsLibres }); }
function envoyer(m) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(m)); }

async function demarrerMicro() {
  try {
    if (!flux) flux = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    $('micro').hidden = true;
  } catch (e) { $('micro').hidden = false; $('micro').classList.add('alerte'); $('aide').textContent = "Le micro n'est pas autorisé : cliquez sur « Activer le micro » puis sur Autoriser."; return; }
  if (rec && rec.state !== 'inactive') rec.stop();
  const types = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus'];
  rec = new MediaRecorder(flux, { mimeType: types.find(t => MediaRecorder.isTypeSupported(t)) });
  envoyer({ t: 'flux' });
  rec.ondataavailable = e => { if (e.data.size && ws && ws.readyState === 1) ws.send(e.data); };
  rec.start(100);
}

function connecter() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => { $('sous').textContent = 'Systèmes en ligne'; envoyer({ t: 'mains_libres', v: mainsLibres }); demarrerMicro(); };
  ws.onclose = () => { $('sous').textContent = 'Connexion perdue… reconnexion'; if (rec && rec.state !== 'inactive') rec.stop(); setTimeout(connecter, 2000); };
  ws.onmessage = e => { const m = JSON.parse(e.data);
    if (m.t === 'etat') changeEtat(m.v);
    else if (m.t === 'niveau') { if (etat === 'ecoute' || etat === 'suivi' || etat === 'repos') corps.style.setProperty('--niveau', etat === 'repos' ? m.v * .3 : m.v); }
    else if (m.t === 'moi') bulle(m.texte, 'moi');
    else if (m.t === 'jarvis') bulle(m.texte, 'jarvis', m.suite);
    else if (m.t === 'erreur') bulle(m.texte, 'erreur');
    else if (m.t === 'reveil') { corps.classList.add('reveil'); setTimeout(() => corps.classList.remove('reveil'), 500); } };
}

$('reacteur').onclick = () => envoyer({ t: etat === 'parle' || etat === 'pense' ? 'stop' : 'ecoute' });
$('taire').onclick = () => envoyer({ t: 'stop' });
$('micro').onclick = () => demarrerMicro();
$('mainsLibres').onclick = () => { mainsLibres = !mainsLibres; try { localStorage.setItem('jarvis-mains-libres', mainsLibres ? 'oui' : 'non'); } catch (e) {} majMainsLibres(); };
document.addEventListener('keydown', e => { if (e.code === 'Space' && document.activeElement.tagName !== 'INPUT') { e.preventDefault(); $('reacteur').click(); } });
$('formulaire').onsubmit = e => { e.preventDefault(); const t = $('texte'); if (t.value.trim()) { envoyer({ t: 'texte', v: t.value.trim() }); t.value = ''; } };

async function recap() {
  try { const r = await fetch('/recap', { method: 'POST', headers: { 'X-Jarvis': '1', 'Content-Type': 'application/json' }, body: JSON.stringify({ pc: true }) });
    const d = await r.json(); if (d.reponse) bulle('📋 ' + d.reponse, 'jarvis'); } catch (e) {}
}
majMainsLibres(); connecter(); recap();
</script></body></html>
"""


async def tts_base64(text: str) -> str | None:
    import base64

    try:
        with tempfile.TemporaryDirectory() as tmp:
            mp3 = await synth(text, os.path.join(tmp, "r.mp3"))
            return base64.b64encode(Path(mp3).read_bytes()).decode()
    except Exception as exc:  # noqa: BLE001
        print(f"Voix (page) indisponible, le navigateur parlera à la place : {exc}", flush=True)
        return None


class SessionMicro:
    """Une page Jarvis connectée : reçoit le micro (webm), détecte « Hey Jarvis », enregistre la phrase,
    puis répond à voix haute sur l'ordinateur."""

    TRAME = 1280  # 80 ms à 16 kHz

    def __init__(self, ws):
        self.ws = ws
        self.mains_libres = True
        self.mode = "veille"  # veille → capture → traitement (→ suivi)
        self.ffmpeg = None
        self.lecteur = None
        self.wake = None
        self.audio = bytearray()
        self.parle = False
        self.t_debut = 0.0
        self.t_dernier_son = 0.0
        self.t_parole = 0.0
        self.fond = 300.0
        self.cooldown = 0.0
        self.wake_pause = 0.0
        self.taches: set = set()

    # --- flux audio ---
    async def demarrer_flux(self) -> None:
        await self.arreter_flux()
        self.ffmpeg = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-loglevel", "error", "-fflags", "nobuffer", "-probesize", "4096",
            "-analyzeduration", "0", "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", "16000", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        self.lecteur = asyncio.create_task(self.lire_pcm(self.ffmpeg))

    async def arreter_flux(self) -> None:
        if self.ffmpeg:
            try:
                self.ffmpeg.stdin.close()
                self.ffmpeg.kill()
            except Exception:  # noqa: BLE001
                pass
        if self.lecteur:
            self.lecteur.cancel()
        self.ffmpeg = self.lecteur = None

    async def recevoir_audio(self, data: bytes) -> None:
        if self.ffmpeg is None or self.ffmpeg.returncode is not None:
            return
        try:
            self.ffmpeg.stdin.write(data)
            await self.ffmpeg.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass

    async def lire_pcm(self, proc) -> None:
        import numpy as np

        reste = b""
        n = 0
        while True:
            bloc = await proc.stdout.read(self.TRAME * 2 * 4)
            if not bloc:
                return
            reste += bloc
            while len(reste) >= self.TRAME * 2:
                trame, reste = reste[:self.TRAME * 2], reste[self.TRAME * 2:]
                x = np.frombuffer(trame, dtype=np.int16)
                rms = float(np.sqrt(np.mean(x.astype(np.float32) ** 2)))
                n += 1
                if n % 2 == 0:
                    await self.envoyer({"t": "niveau", "v": round(min(1.0, rms / 6000), 3)})
                await self.trame(x, rms)

    # --- logique ---
    async def trame(self, x, rms: float) -> None:
        now = time.time()
        if self.mode in ("veille", "suivi"):
            self.fond = rms if rms < self.fond else self.fond * 0.995 + rms * 0.005
        if self.mains_libres:
            if self.wake is None:
                self.wake = await asyncio.to_thread(charger_mot_reveil)
            if self.wake is not False:
                # On analyse toujours le son (le détecteur garde ainsi un historique à jour),
                # mais on ne réagit qu'en veille, en suivi, ou pour couper Jarvis quand il parle.
                score = max(self.wake.predict(x).values())
                peut = self.mode in ("veille", "suivi") or (self.mode == "traitement" and _etat["v"] == "parle")
                if score > 0.5 and peut and now > self.wake_pause:
                    self.wake_pause = now + 2.5
                    stop_pc_voice()
                    for t in list(self.taches):
                        t.cancel()
                    await self.commencer_capture(bip=True)
                    return
        if self.mode == "suivi" and now > self.cooldown:
            self.mode = "veille"
            await changer_etat("repos")
        if self.mode in ("capture", "suivi"):
            seuil = max(600.0, self.fond * 3)
            if self.mode == "suivi":
                if rms > seuil * 1.3:
                    self.mode = "capture"
                    self.audio = bytearray()
                    self.parle, self.t_debut = False, now
                    await changer_etat("ecoute")
                else:
                    return
            self.audio += x.tobytes()
            if rms > seuil:
                if not self.parle:
                    self.parle, self.t_parole = True, now
                self.t_dernier_son = now
            fin_phrase = self.parle and now - self.t_dernier_son > 1.2 and now - self.t_parole > 0.4
            trop_long = now - self.t_debut > 25
            rien = not self.parle and now - self.t_debut > 7
            if rien:
                self.mode = "veille"
                await changer_etat("repos")
            elif fin_phrase or trop_long:
                self.mode = "traitement"
                await self.lancer(self.traiter_audio(bytes(self.audio)))

    async def commencer_capture(self, bip: bool = False) -> None:
        self.mode = "capture"
        self.audio = bytearray()
        self.parle, self.t_debut = False, time.time()
        await changer_etat("ecoute")
        await self.envoyer({"t": "reveil"})
        if bip:
            await self.lancer(speak_on_pc("<bip>", wait=True))

    async def traiter_audio(self, pcm: bytes) -> None:
        import wave

        await changer_etat("pense")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "phrase.wav")
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(pcm)
            question = await asyncio.to_thread(transcribe, path)
        if not question:
            await diffuser({"t": "jarvis", "texte": "Je n'ai pas compris, pouvez-vous répéter ?"})
            await speak_on_pc("Je n'ai pas compris, pouvez-vous répéter ?", wait=True)
            await self.apres_reponse()
            return
        await self.traiter_texte(question)

    async def traiter_texte(self, question: str) -> None:
        self.mode = "traitement"
        await diffuser({"t": "moi", "texte": question})
        await changer_etat("pense")
        try:
            r = intention(question)
            if r is not None:
                await diffuser({"t": "jarvis", "texte": r})
                if not MOTS_STOP.match(question):
                    await speak_on_pc(r, wait=True)
            else:
                await dire_phrases(ask_jarvis_stream(conversation_id(), question))
        except Exception as exc:  # noqa: BLE001
            msg = f"L'IA ne répond pas ({exc})."
            await diffuser({"t": "erreur", "texte": msg})
        await self.apres_reponse()

    async def apres_reponse(self) -> None:
        # Après une réponse, on écoute encore quelques secondes sans « Hey Jarvis » (conversation naturelle).
        if self.mains_libres:
            self.mode, self.cooldown = "suivi", time.time() + 6
            await changer_etat("suivi")
        else:
            self.mode = "veille"
            await changer_etat("repos")

    # --- messages de la page ---
    async def commande(self, msg: dict) -> None:
        t = msg.get("t")
        if t == "ecoute":
            stop_pc_voice()
            await self.commencer_capture(bip=False)
        elif t == "stop":
            stop_pc_voice()
            self.mode = "veille"
            await changer_etat("repos")
        elif t == "texte" and str(msg.get("v", "")).strip():
            stop_pc_voice()
            await self.lancer(self.traiter_texte(str(msg["v"]).strip()[:2000]))
        elif t == "mains_libres":
            self.mains_libres = bool(msg.get("v"))
        elif t == "flux":
            await self.demarrer_flux()

    async def envoyer(self, msg: dict) -> None:
        try:
            await self.ws.send_json(msg)
        except Exception:  # noqa: BLE001
            pass

    async def lancer(self, coro) -> None:
        t = asyncio.create_task(coro)
        self.taches.add(t)
        t.add_done_callback(self.taches.discard)


_mot_reveil: dict = {}


def charger_mot_reveil():
    """Modèle « Hey Jarvis » (openWakeWord), un par page. False s'il n'est pas installé."""
    if _mot_reveil.get("absent"):
        return False
    try:
        import warnings

        warnings.filterwarnings("ignore", module="onnxruntime")
        import openwakeword

        m = openwakeword.Model(wakeword_model_paths=[openwakeword.models["hey_jarvis"]["model_path"]])
        if not _mot_reveil.get("annonce"):
            print("👂 « Hey Jarvis » prêt.", flush=True)
            _mot_reveil["annonce"] = True
        return m
    except Exception as exc:  # noqa: BLE001
        print(f"« Hey Jarvis » indisponible ({exc}). Installe-le : uv pip install openwakeword", flush=True)
        _mot_reveil["absent"] = True
        return False


def build_web_app():
    from aiohttp import web

    allowed_hosts = {f"localhost:{WEB_PORT}", f"127.0.0.1:{WEB_PORT}", f"[::1]:{WEB_PORT}"}

    @web.middleware
    async def securite(request, handler):
        # Seulement depuis cet ordinateur, et pas depuis un autre site web.
        origin = request.headers.get("Origin")
        if request.host not in allowed_hosts or (origin and origin.split("//")[-1] not in allowed_hosts):
            return web.Response(status=403, text="Accès refusé")
        if request.method == "POST" and request.headers.get("X-Jarvis") != "1":
            return web.Response(status=403, text="Accès refusé")
        return await handler(request)

    async def index(request):
        return web.Response(text=PAGE, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def reponse(question: str):
        if not question:
            texte = "Je n'ai pas compris, tu peux répéter ?"
        else:
            try:
                texte = await handle_question(question)
            except Exception as exc:  # noqa: BLE001
                texte = f"L'IA ne répond pas ({exc}). Est-ce qu'Ollama tourne ?"
        return web.json_response({"question": question, "reponse": texte, "audio": await tts_base64(texte)})

    async def parler(request):
        data = await request.read()
        if len(data) < 2000:
            return await reponse("")
        ext = ".ogg" if "ogg" in request.content_type else ".webm"
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "micro" + ext)
            Path(path).write_bytes(data)
            question = await asyncio.to_thread(transcribe, path)
        return await reponse(question)

    async def ecrire(request):
        body = await request.json()
        return await reponse(str(body.get("texte", "")).strip()[:2000])

    cache: dict = {}

    async def recap(request):
        """Récap de ce qui se passe. Avec {"pc": true}, Jarvis le dit sur les haut-parleurs de l'ordi
        (pas besoin de cliquer : le navigateur n'a rien à jouer)."""
        body = await request.json() if request.can_read_body else {}
        if not (cache.get("texte") and time.time() - cache.get("t", 0) < 600):
            try:
                cache["texte"] = await recap_text(await collect_overview(load_settings()))
                cache["t"] = time.time()
            except Exception as exc:  # noqa: BLE001
                return web.json_response({"reponse": f"Je n'arrive pas à préparer le récap ({exc}).", "audio": None})
        texte = cache["texte"]
        if body.get("pc"):
            try:
                await speak_on_pc(texte)
                return web.json_response({"reponse": texte, "audio": None, "parle_pc": True})
            except Exception as exc:  # noqa: BLE001
                print(f"Récap sur les haut-parleurs impossible : {exc}", flush=True)
        return web.json_response({"reponse": texte, "audio": await tts_base64(texte)})

    app = web.Application(middlewares=[securite], client_max_size=20 * 1024 * 1024)
    async def stop(request):
        stop_pc_voice()
        return web.json_response({"ok": True})

    async def ws_handler(request):
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=8 * 1024 * 1024)
        await ws.prepare(request)
        HUB.add(ws)
        session = SessionMicro(ws)
        await ws.send_json({"t": "etat", "v": _etat["v"]})
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.BINARY:
                    await session.recevoir_audio(msg.data)
                elif msg.type == web.WSMsgType.TEXT:
                    try:
                        await session.commande(json.loads(msg.data))
                    except json.JSONDecodeError:
                        pass
        finally:
            HUB.discard(ws)
            await session.arreter_flux()
        return ws

    app.add_routes([web.get("/", index), web.post("/parler", parler), web.post("/ecrire", ecrire),
                    web.post("/recap", recap), web.post("/stop", stop), web.get("/ws", ws_handler)])
    return app


async def start_web() -> None:
    from aiohttp import web

    runner = web.AppRunner(build_web_app())
    await runner.setup()
    try:
        await web.TCPSite(runner, WEB_HOST, WEB_PORT).start()
        print(f"🎤 Page Jarvis : http://localhost:{WEB_PORT}  (ouvre-la dans ton navigateur)", flush=True)
    except OSError as exc:
        print(f"Page Jarvis indisponible (port {WEB_PORT} occupé ?) : {exc}", flush=True)


def ensure_ollama() -> subprocess.Popen | None:
    try:
        httpx.get(f"{OLLAMA}/api/tags", timeout=3)
        return None
    except httpx.HTTPError:
        pass
    ollama = shutil.which("ollama") or "/usr/local/bin/ollama"
    if not os.path.exists(ollama):
        raise SystemExit("Ollama introuvable. Réinstalle OpenJarvis ou Ollama (https://ollama.com).")
    print("Démarrage d'Ollama…", flush=True)
    log = open(HOME / "ollama.log", "a")
    proc = subprocess.Popen([ollama, "serve"], stdout=log, stderr=subprocess.STDOUT)
    for _ in range(60):
        time.sleep(1)
        try:
            httpx.get(f"{OLLAMA}/api/tags", timeout=3)
            return proc
        except httpx.HTTPError:
            if proc.poll() is not None:
                break
    raise SystemExit(f"Ollama ne démarre pas : voir {HOME / 'ollama.log'}")


def main() -> None:
    from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

    if not TOKEN:
        raise SystemExit(f"Pas de token Telegram dans {CONFIG} (section [channel.telegram]).")
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg manquant : sudo apt install -y ffmpeg")
    s = load_settings()
    if not s:
        print("Astuce : lance « ~/.jarvis-voix/bin/python ~/jarvis_voix.py reglages » "
              "pour brancher ton courriel et ton agenda.\n", flush=True)

    proc = ensure_ollama()
    tasks = []

    async def prechauffer():
        """Charge l'IA dans la carte graphique et la reconnaissance vocale dès le démarrage."""
        try:
            await asyncio.to_thread(whisper)
            await llm([{"role": "user", "content": "Réponds seulement : OK."}])
            print("⚡ IA et reconnaissance vocale prêtes.", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"Préchauffage : {exc}", flush=True)

    async def post_init(app) -> None:
        tasks.append(asyncio.create_task(watcher(app)))
        await start_web()
        tasks.append(asyncio.create_task(prechauffer()))
        if load_settings().get("pc_voice") and load_settings().get("son_demarrage", True):
            async def bonjour():
                try:
                    await speak_on_pc("<carillon>", wait=True)
                    salut = "Jarvis en ligne. Tous les systèmes sont opérationnels" + (
                        ", Monsieur." if load_settings().get("majordome") else ".")
                    await speak_on_pc(salut, wait=True)
                except Exception as exc:  # noqa: BLE001
                    print(f"Son de démarrage : {exc}", flush=True)
            tasks.append(asyncio.create_task(bonjour()))

    try:
        app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
        app.add_handler(CommandHandler(["start", "aide", "help"], cmd_aide))
        app.add_handler(CommandHandler(["courriels", "courriel", "mails"], cmd_courriels))
        app.add_handler(CommandHandler("agenda", cmd_agenda))
        app.add_handler(CommandHandler("rappels", cmd_rappels))
        app.add_handler(CommandHandler("recap", cmd_recap))
        app.add_handler(CommandHandler(["memoire", "memo"], cmd_memoire))
        app.add_handler(CommandHandler("oublie", cmd_oublie))
        app.add_handler(CommandHandler(["liste", "listes"], cmd_liste))
        app.add_handler(CommandHandler(["epicerie", "courses"], cmd_epicerie))
        app.add_handler(CommandHandler(["taches", "todo"], cmd_taches))
        app.add_handler(CommandHandler(["stop", "chut"], cmd_stop))
        app.add_handler(CommandHandler(["canadiens", "ch", "hockey"], cmd_canadiens))
        app.add_handler(CommandHandler("effacer", cmd_effacer))
        app.add_handler(CommandHandler("meteo", cmd_meteo))
        app.add_handler(CommandHandler("nouvelles", cmd_nouvelles))
        app.add_handler(CommandHandler("cherche", cmd_cherche))
        app.add_handler(
            MessageHandler((filters.TEXT & ~filters.COMMAND) | filters.VOICE | filters.AUDIO, on_message)
        )
        branche = [n for n, ok in (("courriel", s.get("email")), ("agenda", s.get("ics_url")),
                                   ("météo", s.get("lat")), ("nouvelles", s.get("news_url")),
                                   ("voix sur l'ordi", s.get("pc_voice"))) if ok]
        print(f"✅ Jarvis Voix est prêt ({', '.join(branche) or 'sans courriel ni agenda'}). "
              "Parle-lui sur Telegram ! (Ctrl+C pour arrêter)", flush=True)
        app.run_polling()
    finally:
        if proc is not None:
            proc.terminate()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("reglages", "réglages", "config"):
        reglages()
    elif len(sys.argv) > 1 and sys.argv[1] == "voix":
        choisir_voix()
    elif len(sys.argv) > 1 and sys.argv[1] in ("diagnostic", "diag", "test"):
        diagnostic()
    elif len(sys.argv) > 1 and sys.argv[1] in ("modele", "modèle", "ia"):
        choisir_modele()
    elif len(sys.argv) > 1 and sys.argv[1] in ("accelerer", "accélérer"):
        accelerer()
    else:
        main()
