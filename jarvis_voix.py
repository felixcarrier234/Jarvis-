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
            out.append({
                "uid": uid,
                "from": sender,
                "subject": _dec(msg.get("Subject")) or "(sans objet)",
                "date": when,
                "unread": "\\Seen" not in flags,
                "body": _body(msg)[:400],
            })
        return out
    finally:
        try:
            box.logout()
        except Exception:  # noqa: BLE001
            pass


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
        lines.append(f"- [{etat}] {when} — De : {m['from']} — Objet : {m['subject']}\n  Début : {m['body'][:250]}")
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
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
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
        "accès. Tu peux aussi créer des rappels : l'utilisateur doit dire « rappelle-moi … ». "
        f"Nous sommes {JOURS[now.weekday()]} {now.day} {MOIS[now.month - 1]} {now.year}, "
        f"il est {fmt_hour(now)}."
    )
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
    if MOTS_WEB.search(question):
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


async def ask_jarvis(chat_id: int, question: str) -> str:
    s = load_settings()
    extra = await gather_context(question, s)
    h = history[chat_id]
    content = question
    if extra:
        content = (
            "Voici mes vraies données (utilise-les pour me répondre, n'invente rien d'autre) :\n\n"
            f"{extra}\n\nMa question : {question}"
        )
    messages = [{"role": "system", "content": system_prompt()}, *h, {"role": "user", "content": content}]
    answer = await llm(messages) or "Désolé, je n'ai pas de réponse."
    h.append({"role": "user", "content": question})
    h.append({"role": "assistant", "content": answer})
    return answer


# ---------------------------------------------------------------------------
# Voix
# ---------------------------------------------------------------------------

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


async def speak_on_pc(text: str, wait: bool = False, s: dict | None = None) -> None:
    """Dit le texte sur les haut-parleurs de l'ordinateur (Windows via WSL, ou Linux).
    Avec wait=True, attend la fin et lève une erreur claire si le son n'a pas pu être joué."""
    out_dir = await asyncio.to_thread(_win_temp) or Path(tempfile.gettempdir()) / "jarvis-voix"
    out_dir.mkdir(exist_ok=True)
    wav = out_dir / f"{time.time_ns()}.wav"
    await synth(text, str(wav), s)
    cmd, cwd = _play_cmd(wav)

    async def play():
        try:
            async with _pc_lock:  # une phrase à la fois
                r = await asyncio.to_thread(subprocess.run, cmd, cwd=cwd, capture_output=True, text=True,
                                            timeout=300, stdin=subprocess.DEVNULL)
            if r.returncode != 0:
                raise RuntimeError(f"le lecteur audio a échoué : {(r.stderr or r.stdout).strip()[-400:]}")
        finally:
            wav.unlink(missing_ok=True)

    if wait:
        await play()
    else:
        async def play_logged():
            try:
                await play()
            except Exception as exc:  # noqa: BLE001
                print(f"Voix sur l'ordinateur : {exc}", flush=True)
        task = asyncio.create_task(play_logged())
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)


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
    """Rappel perso, sinon question à Jarvis. Retourne le texte de la réponse."""
    if MOTS_RAPPEL.search(question):
        parsed = parse_rappel(question)
        if parsed is None:
            return "Je veux bien, mais pour quand ? Dis par exemple : rappelle-moi demain à 9 h d'appeler maman."
        when, what = parsed
        add_reminder(when, what)
        return f"C'est noté : je te rappellerai {fmt_when(when)} : {what}."
    return await ask_jarvis(conversation_id(), question)


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
        "• /rappels — tes rappels (et /effacer N)\n\n"
        "Exemples : « Qu'est-ce que j'ai demain ? », « Lis-moi mes courriels », "
        "« Est-ce qu'il va neiger demain ? », « Rappelle-moi dans 20 minutes de sortir le linge »."
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
        out["courriels"] = [{"de": m["from"], "objet": m["subject"],
                             "quand": fmt_when(m["date"]) if m["date"] else "", "debut": m["body"][:200]}
                            for m in mails if m["unread"]]

    async def meteo():
        out["meteo"] = await asyncio.to_thread(weather_data, s)

    async def nouvelles():
        out["nouvelles"] = await asyncio.to_thread(fetch_news, s, 4)

    jobs = {"agenda": (s.get("ics_url"), agenda), "courriels": (s.get("email"), courriels),
            "meteo": (s.get("lat"), meteo), "nouvelles": (s.get("news_url"), nouvelles)}
    actifs = [(nom, fn) for nom, (ok, fn) in jobs.items() if ok]
    results = await asyncio.gather(*(fn() for _, fn in actifs), return_exceptions=True)
    for (nom, _), res in zip(actifs, results):
        if isinstance(res, Exception):
            out["erreurs"][nom] = str(res)

    limite = now + timedelta(hours=36)
    out["rappels"] = [{"quand": fmt_when(datetime.fromisoformat(r["when"])), "quoi": r["what"]}
                      for r in load_reminders() if datetime.fromisoformat(r["when"]) <= limite]
    out["heure"] = fmt_hour(now)
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
    if "courriels" in o:
        parts.append(f"COURRIELS NON LUS ({len(o['courriels'])}) :\n"
                     + ("\n".join(f"- De {m['de']} : {m['objet']}" for m in o["courriels"][:8]) or "(aucun)"))
    if "nouvelles" in o:
        parts.append("NOUVELLES :\n" + "\n".join(f"- {t}" for t in o["nouvelles"]))
    return "\n\n".join(parts)


async def recap_text(o: dict, matin: bool = False) -> str:
    h = datetime.now().astimezone().hour
    salut = "Bonjour" if h < 12 else "Bon après-midi" if h < 18 else "Bonsoir"
    consigne = (
        f"Fais-moi un récap de ce qui se passe (il est {o['heure']}). Commence par « {salut} », puis en 4 à 7 "
        "phrases naturelles : la météo, mes prochains rendez-vous et rappels, les courriels non lus importants "
        "(qui et à propos de quoi) et 2 nouvelles. Ne mentionne pas ce qui est vide."
    )
    if matin:
        consigne = "C'est mon résumé du matin, souhaite-moi bonne journée. " + consigne
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
                            await say(app.bot, chat_id, f"Nouveau courriel de {m['from']} : {m['subject']}.")
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

        await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# Page « Jarvis » sur l'ordinateur : parler avec le micro du PC
# ---------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jarvis</title>
<style>
  :root { --bg:#0b1020; --panel:#121a33; --text:#e8ecff; --muted:#8d97c7; --accent:#4cc9ff; --me:#1f2b55; --warn:#ffb74d; }
  * { box-sizing:border-box; }
  body { margin:0; min-height:100vh; background:radial-gradient(circle at 50% 0%, #17224a, var(--bg) 60%);
         color:var(--text); font:16px/1.45 system-ui, "Segoe UI", sans-serif; display:flex; flex-direction:column; align-items:center; }
  header { width:100%; max-width:760px; padding:20px 16px 0; display:flex; justify-content:space-between; align-items:center; }
  h1 { font-size:20px; letter-spacing:.2em; margin:0; font-weight:600; }
  #etat { color:var(--muted); font-size:14px; }
  #orbe { margin:28px 0 12px; width:190px; height:190px; border-radius:50%; border:none; cursor:pointer; position:relative;
          background:radial-gradient(circle at 35% 30%, #9be7ff, var(--accent) 35%, #0a6aa8 70%, #062a4a);
          box-shadow:0 0 40px #4cc9ff55, inset 0 0 30px #00000066; transition:transform .2s, box-shadow .3s; }
  #orbe:focus-visible { outline:3px solid #fff; outline-offset:6px; }
  #orbe.ecoute { animation:pulse 1.4s ease-in-out infinite; }
  #orbe.parle { box-shadow:0 0 80px #4cc9ffaa, inset 0 0 30px #00000066; animation:parle .5s ease-in-out infinite alternate; }
  #orbe.pense { animation:tourne 1.2s linear infinite; filter:saturate(.6); }
  #orbe.arret { filter:grayscale(.7) brightness(.7); }
  #niveau { position:absolute; inset:-10px; border-radius:50%; border:3px solid #4cc9ff; opacity:0; transition:opacity .1s, transform .1s; pointer-events:none; }
  @keyframes pulse { 50% { transform:scale(1.06); box-shadow:0 0 60px #4cc9ff88, inset 0 0 30px #00000066; } }
  @keyframes parle { to { transform:scale(1.08); } }
  @keyframes tourne { to { filter:saturate(.6) hue-rotate(360deg); } }
  #consigne { color:var(--muted); margin:0 16px 18px; text-align:center; min-height:1.4em; }
  .ecouter { margin-top:8px; background:none; border:1px solid var(--accent); color:var(--accent); border-radius:10px; padding:4px 10px; cursor:pointer; }
  #fil { width:100%; max-width:760px; padding:0 16px; flex:1; display:flex; flex-direction:column; gap:10px; overflow-y:auto; }
  .bulle { max-width:85%; padding:10px 14px; border-radius:14px; background:var(--panel); white-space:pre-wrap; }
  .moi { align-self:flex-end; background:var(--me); }
  .erreur { color:var(--warn); }
  form { width:100%; max-width:760px; padding:14px 16px 20px; display:flex; gap:8px; }
  input { flex:1; min-width:0; padding:12px 14px; border-radius:12px; border:1px solid #2a3770; background:#0e1530; color:var(--text); font-size:16px; }
  button.envoyer { padding:0 18px; border-radius:12px; border:none; background:var(--accent); color:#04233a; font-weight:600; cursor:pointer; }
</style></head>
<body>
<header><h1>JARVIS</h1><span id="etat">prêt</span></header>
<button id="orbe" class="arret" aria-label="Commencer ou arrêter la conversation"><span id="niveau"></span></button>
<p id="consigne">Clique sur le cercle (ou appuie sur Espace) pour me parler.</p>
<div id="fil" aria-live="polite"></div>
<form id="formulaire"><input id="texte" placeholder="…ou écris ta question ici" autocomplete="off">
<button class="envoyer">Envoyer</button></form>
<script>
const orbe = document.getElementById('orbe'), niveau = document.getElementById('niveau'),
      etat = document.getElementById('etat'), consigne = document.getElementById('consigne'),
      fil = document.getElementById('fil');
let actif = false, flux = null, ctx = null, analyseur = null, enregistreur = null, morceaux = [],
    occupe = false, lecteur = new Audio(), fond = 0.01;

let recap = null;
async function chargerRecap() {
  const attente = document.createElement('div'); attente.className = 'bulle'; attente.textContent = '📋 Je prépare ton récap…';
  fil.appendChild(attente); mode('pense', 'je prépare ton récap');
  try { recap = await appel('/recap', { method: 'POST', body: JSON.stringify({ pc: true }), headers: { 'Content-Type': 'application/json' } }); }
  catch (e) { attente.textContent = "📋 Je n'ai pas pu préparer le récap (" + e.message + ')'; mode('arret', 'prêt'); return; }
  attente.textContent = '📋 ' + recap.reponse;
  if (!actif) mode('arret', recap.parle_pc ? 'je te parle' : 'prêt');
  if (!recap.parle_pc && recap.audio && !occupe) { occupe = true; jouer(recap, () => { occupe = false; if (actif) ecouter(); else mode('arret', 'prêt'); }); }
  const b = document.createElement('button'); b.className = 'ecouter'; b.textContent = '🔊 Réécouter';
  b.onclick = async () => { if (occupe) return; occupe = true;
    let r = recap; if (!r.audio) { try { r = await appel('/recap', { method: 'POST', body: '{}', headers: { 'Content-Type': 'application/json' } }); } catch (e) {} }
    jouer(r, () => { occupe = false; if (actif) ecouter(); else mode('arret', 'prêt'); }); };
  attente.appendChild(document.createElement('br')); attente.appendChild(b);
}
function jouer(r, fin) {
  mode('parle', 'je parle');
  if (r.audio) { lecteur.src = 'data:audio/mpeg;base64,' + r.audio; lecteur.onended = fin; lecteur.onerror = fin; lecteur.play().catch(fin); }
  else if (r.reponse) { const u = new SpeechSynthesisUtterance(r.reponse); u.lang = 'fr-FR'; u.onend = fin; u.onerror = fin; speechSynthesis.speak(u); }
  else fin();
}

function mode(m, txt) { orbe.className = m; etat.textContent = txt; }
function ajoute(texte, classe) { const d = document.createElement('div'); d.className = 'bulle ' + (classe || '');
  d.textContent = texte; fil.appendChild(d); fil.scrollTop = fil.scrollHeight; }

async function demarrer() {
  try {
    flux = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
  } catch (e) { ajoute("Je n'ai pas accès au micro. Autorise-le dans le navigateur (icône 🔒 ou 🎤 dans la barre d'adresse).", 'erreur'); return; }
  ctx = new AudioContext(); analyseur = ctx.createAnalyser(); analyseur.fftSize = 1024;
  ctx.createMediaStreamSource(flux).connect(analyseur);
  actif = true; consigne.textContent = "Je t'écoute… Parle normalement, je réponds quand tu t'arrêtes. Clique encore pour arrêter.";
  ecouter();
}
function arreter() {
  actif = false; if (enregistreur && enregistreur.state !== 'inactive') enregistreur.stop();
  if (flux) flux.getTracks().forEach(t => t.stop()); if (ctx) ctx.close(); flux = ctx = null;
  lecteur.pause(); speechSynthesis.cancel(); mode('arret', 'en pause');
  consigne.textContent = 'Clique sur le cercle (ou appuie sur Espace) pour parler à Jarvis.';
}
function volume() { const d = new Float32Array(analyseur.fftSize); analyseur.getFloatTimeDomainData(d);
  let s = 0; for (const v of d) s += v * v; return Math.sqrt(s / d.length); }

async function ecouter() {
  if (!actif || occupe) return;
  mode('ecoute', "j'écoute");
  enregistreur = new MediaRecorder(flux); morceaux = [];
  enregistreur.ondataavailable = e => { if (e.data.size) morceaux.push(e.data); };
  let parle = false, debut = 0, dernierSon = 0; const t0 = performance.now();
  enregistreur.onstop = async () => {
    niveau.style.opacity = 0;
    if (!actif) return;
    if (!parle) { ecouter(); return; }
    await envoyer(new Blob(morceaux, { type: enregistreur.mimeType }));
  };
  enregistreur.start(250);
  const boucle = () => {
    if (!actif || enregistreur.state === 'inactive') return;
    const v = volume(), t = performance.now();
    // Bruit de fond estimé en continu : descend vite, remonte lentement (la voix ne le fausse pas).
    fond = v < fond ? v : fond * 0.998 + v * 0.002;
    const seuil = Math.max(0.012, fond * 3);
    niveau.style.opacity = Math.min(1, v / (seuil * 3)); niveau.style.transform = `scale(${1 + Math.min(.25, v * 3)})`;
    if (v > seuil) { if (!parle) { parle = true; debut = t; } dernierSon = t; }
    if (parle && t - dernierSon > 1300 && t - debut > 400) return enregistreur.stop();   // fin de phrase
    if (parle && t - debut > 30000) return enregistreur.stop();                          // 30 s max
    if (!parle && t - t0 > 15000) return enregistreur.stop();                            // relance l'écoute
    requestAnimationFrame(boucle);
  };
  boucle();
}

async function appel(url, options) {
  const r = await fetch(url, { ...options, headers: { ...(options.headers || {}), 'X-Jarvis': '1' } });
  if (!r.ok) throw new Error('erreur ' + r.status);
  return r.json();
}
async function envoyer(blob) {
  occupe = true; mode('pense', 'je réfléchis…');
  try { repondre(await appel('/parler', { method: 'POST', body: blob, headers: { 'Content-Type': blob.type || 'audio/webm' } })); }
  catch (e) { ajoute('Oups : ' + e.message, 'erreur'); occupe = false; ecouter(); }
}
async function ecrire(texte) {
  occupe = true; ajoute(texte, 'moi'); mode('pense', 'je réfléchis…');
  try { repondre(await appel('/ecrire', { method: 'POST', body: JSON.stringify({ texte }), headers: { 'Content-Type': 'application/json' } }), true); }
  catch (e) { ajoute('Oups : ' + e.message, 'erreur'); occupe = false; ecouter(); }
}
function repondre(r, dejaAffiche) {
  if (r.question && !dejaAffiche) ajoute(r.question, 'moi');
  if (r.reponse) ajoute(r.reponse);
  jouer(r, () => { occupe = false; if (actif) ecouter(); else mode('arret', 'prêt'); });
}

orbe.onclick = () => actif ? arreter() : demarrer();
document.addEventListener('keydown', e => { if (e.code === 'Space' && document.activeElement.tagName !== 'INPUT') { e.preventDefault(); orbe.click(); } });
document.getElementById('formulaire').onsubmit = e => { e.preventDefault(); const t = document.getElementById('texte');
  if (t.value.trim() && !occupe) { ecrire(t.value.trim()); t.value = ''; } };
chargerRecap();
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
    app.add_routes([web.get("/", index), web.post("/parler", parler), web.post("/ecrire", ecrire),
                    web.post("/recap", recap)])
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

    async def post_init(app) -> None:
        tasks.append(asyncio.create_task(watcher(app)))
        await start_web()

    try:
        app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
        app.add_handler(CommandHandler(["start", "aide", "help"], cmd_aide))
        app.add_handler(CommandHandler(["courriels", "courriel", "mails"], cmd_courriels))
        app.add_handler(CommandHandler("agenda", cmd_agenda))
        app.add_handler(CommandHandler("rappels", cmd_rappels))
        app.add_handler(CommandHandler("recap", cmd_recap))
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
    else:
        main()
