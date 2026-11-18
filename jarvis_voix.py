"""Jarvis Voix — ton assistant dans Telegram, qui te parle.

- Tu lui envoies un vocal (ou du texte), il te répond en texte + vocal.
- Il lit tes courriels (Outlook/Hotmail via IMAP) quand tu lui demandes.
- Il connaît ton agenda (Google Agenda via l'adresse secrète iCal).
- Il te rappelle tes rendez-vous avant qu'ils commencent, t'annonce les
  nouveaux courriels et te fait un résumé de ta journée le matin.
- Rappels perso (« rappelle-moi demain à 9 h d'appeler maman »), météo,
  nouvelles, recherche sur Internet, et il peut parler sur les haut-parleurs
  de l'ordinateur.

Usage :
    python jarvis_voix.py            # lance l'assistant
    python jarvis_voix.py reglages   # configure courriel / agenda / rappels

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
WHISPER_MODEL = os.environ.get("JARVIS_WHISPER", "small")

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


def weather_text(s: dict) -> str:
    r = httpx.get("https://api.open-meteo.com/v1/forecast", timeout=20, params={
        "latitude": s["lat"], "longitude": s["lon"], "timezone": "auto", "forecast_days": 3,
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
    })
    r.raise_for_status()
    d = r.json()
    c = d["current"]
    lines = [f"MÉTÉO à {s.get('city_label') or s.get('city')} :",
             f"Maintenant : {round(c['temperature_2m'])} °C (ressenti {round(c['apparent_temperature'])} °C), "
             f"{METEO_CODES.get(c['weather_code'], 'temps variable')}, vent {round(c['wind_speed_10m'])} km/h."]
    dd = d["daily"]
    for i, jour in enumerate(dd["time"]):
        nom = ["Aujourd'hui", "Demain", "Après-demain"][i] if i < 3 else jour
        pluie = dd["precipitation_probability_max"][i]
        lines.append(f"{nom} : {METEO_CODES.get(dd['weather_code'][i], 'variable')}, "
                     f"min {round(dd['temperature_2m_min'][i])} °C, max {round(dd['temperature_2m_max'][i])} °C"
                     + (f", {pluie} % de risque de précipitations." if pluie is not None else "."))
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


async def speak(text: str, workdir: str) -> str:
    import edge_tts

    mp3 = os.path.join(workdir, "reponse.mp3")
    ogg = os.path.join(workdir, "reponse.ogg")
    await edge_tts.Communicate(text, VOICE).save(mp3)
    await asyncio.to_thread(
        subprocess.run,
        ["ffmpeg", "-y", "-loglevel", "error", "-i", mp3, "-c:a", "libopus", "-b:a", "48k", ogg],
        check=True,
    )
    return ogg


_pc_lock = asyncio.Lock()
_bg_tasks: set = set()


async def speak_on_pc(text: str, wait: bool = False) -> None:
    """Dit le texte sur les haut-parleurs de l'ordinateur (Windows via WSL, ou Linux)."""
    import edge_tts

    out_dir = Path(tempfile.gettempdir()) / "jarvis-voix"
    out_dir.mkdir(exist_ok=True)
    stamp = f"{time.time_ns()}"
    mp3, wav = out_dir / f"{stamp}.mp3", out_dir / f"{stamp}.wav"
    await edge_tts.Communicate(text, VOICE).save(str(mp3))
    await asyncio.to_thread(subprocess.run, ["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3), str(wav)],
                            check=True)
    mp3.unlink(missing_ok=True)
    if shutil.which("powershell.exe"):
        win = subprocess.run(["wslpath", "-w", str(wav)], capture_output=True, text=True, check=True).stdout.strip()
        cmd = ["powershell.exe", "-NoProfile", "-Command",
               f"(New-Object Media.SoundPlayer '{win}').PlaySync()"]
        cwd = "/mnt/c" if os.path.isdir("/mnt/c") else None
    elif shutil.which("paplay"):
        cmd, cwd = ["paplay", str(wav)], None
    elif shutil.which("aplay"):
        cmd, cwd = ["aplay", "-q", str(wav)], None
    else:
        raise RuntimeError("aucun lecteur audio trouvé")

    async def play():
        async with _pc_lock:  # une phrase à la fois
            await asyncio.to_thread(subprocess.run, cmd, cwd=cwd, capture_output=True, timeout=300)
        wav.unlink(missing_ok=True)

    if wait:
        await play()
    else:
        task = asyncio.create_task(play())
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

    if MOTS_RAPPEL.search(question):
        parsed = parse_rappel(question)
        if parsed is None:
            await say(context.bot, chat_id, "Je veux bien, mais pour quand ? Dis par exemple : "
                      "rappelle-moi demain à 9 h d'appeler maman.", reply_to=msg)
        else:
            when, what = parsed
            add_reminder(when, what)
            await say(context.bot, chat_id, f"C'est noté : je te rappellerai {fmt_when(when)} : {what}.",
                      reply_to=msg)
        return

    try:
        answer = await ask_jarvis(chat_id, question)
    except Exception as exc:  # noqa: BLE001
        await msg.reply_text(f"L'IA ne répond pas ({exc}). Est-ce qu'Ollama tourne ?")
        return
    await say(context.bot, chat_id, answer, reply_to=msg)


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


async def cmd_aide(update, context) -> None:
    await update.effective_message.reply_text(
        "Je suis Jarvis 🤖\n\n"
        "• Parle-moi en vocal 🎤 ou écris-moi.\n"
        "• /agenda — tes prochains rendez-vous\n"
        "• /courriels — tes derniers courriels\n"
        "• /meteo — la météo\n"
        "• /nouvelles — les titres du moment\n"
        "• /cherche … — chercher sur Internet\n"
        "• /rappels — tes rappels (et /effacer N)\n\n"
        "Exemples : « Qu'est-ce que j'ai demain ? », « Lis-moi mes courriels », "
        "« Est-ce qu'il va neiger demain ? », « Rappelle-moi dans 20 minutes de sortir le linge »."
    )


async def morning_brief(bot, chat_id: str, s: dict) -> None:
    extra = []
    if s.get("ics_url"):
        try:
            today = datetime.now().astimezone().date()
            evs = [e for e in await asyncio.to_thread(fetch_events, s, 1) if e["start"].date() == today]
            extra.append("AGENDA D'AUJOURD'HUI :\n" + events_text(evs))
        except Exception as exc:  # noqa: BLE001
            extra.append(f"AGENDA : illisible ({exc})")
    if s.get("email"):
        try:
            mails = [m for m in await asyncio.to_thread(fetch_emails, s, 15) if m["unread"]]
            extra.append(f"COURRIELS NON LUS ({len(mails)}) :\n" + emails_text(mails[:8]))
        except Exception as exc:  # noqa: BLE001
            extra.append(f"COURRIELS : illisibles ({exc})")
    if s.get("lat"):
        try:
            extra.append(await asyncio.to_thread(weather_text, s))
        except Exception as exc:  # noqa: BLE001
            extra.append(f"MÉTÉO : illisible ({exc})")
    if s.get("news_url"):
        try:
            extra.append("NOUVELLES :\n" + "\n".join(f"- {t}" for t in await asyncio.to_thread(fetch_news, s, 4)))
        except Exception as exc:  # noqa: BLE001
            extra.append(f"NOUVELLES : illisibles ({exc})")
    today_rem = [r for r in load_reminders() if datetime.fromisoformat(r["when"]).date() == datetime.now().astimezone().date()]
    if today_rem:
        extra.append("RAPPELS D'AUJOURD'HUI :\n" + "\n".join(
            f"- {fmt_hour(datetime.fromisoformat(r['when']))} : {r['what']}" for r in today_rem))
    text = await llm([
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": "Voici mes vraies données :\n\n" + "\n\n".join(extra)
         + "\n\nFais-moi mon résumé du matin : souhaite-moi bonne journée, donne la météo, "
           "dis-moi mes rendez-vous et rappels d'aujourd'hui, les courriels importants et 2 ou 3 nouvelles."},
    ])
    await say(bot, chat_id, text or "Bonjour ! Je n'ai rien de spécial à te signaler ce matin.")


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

    try:
        app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
        app.add_handler(CommandHandler(["start", "aide", "help"], cmd_aide))
        app.add_handler(CommandHandler(["courriels", "courriel", "mails"], cmd_courriels))
        app.add_handler(CommandHandler("agenda", cmd_agenda))
        app.add_handler(CommandHandler("rappels", cmd_rappels))
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
    else:
        main()
