# Control diario: cada PDF de la página del Concejo tiene que estar en el índice, y cada expediente de esos PDF tiene que
# haber quedado como ítem. Si algo falla, manda un mail y sale con error (el run queda en rojo aunque el mail no salga).
# Además cruza con lamontana.com.ar, el sitio de La Montaña y FM de la Montaña: una nota que menciona algo aprobado por el Concejo sin ningún ítem parecido de las sesiones
# recientes va al mail como advertencia, sin poner el run en rojo (es un sitio de terceros y la señal es de texto).
# shortcut: repite el mail cada día hasta que se resuelva, para no guardar estado; si molesta, recordar lo ya avisado.
# shortcut: corre dentro del cron, así que no avisa si el cron mismo se desactiva; para eso haría falta un control externo.
import os
import re
import smtplib
import sys
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from huggingface_hub import hf_hub_download

from actas_parser import extract_text_from_pdf_bytes, normalize_text, parse_date_any
from auto_fetch import fetch_pdf_links, fetch_special_media_links, session
from hf_dataset_store import DATASET_REPO_ID, load_index, load_processed_json, log

LA_MONTANA = "https://www.lamontana.com.ar/"
NEWS_WINDOW = timedelta(days=3)      # el mismo aviso sale a lo sumo 3 días seguidos
SESSIONS_WINDOW = timedelta(days=21)  # sesiones contra las que se compara una nota
MATCH_MIN = 0.5                       # parte de las palabras del titular que tiene que tener un ítem
STOPWORDS = {"aprob", "sancio", "concej", "deliber", "martin", "andes", "sobre", "desde", "hasta", "entre", "tras",
             "nuevo", "nueva", "actual", "ordena", "proyec", "sesion", "vecino"}
NEWS_URL_PAT = re.compile(r"/noticias/(\d{4})/(\d{2})/(\d{2})/(\d+)-")
APROBADO_PAT = re.compile(r"Concejo Deliberante[^.]{0,120}\b(?:aprob|sancion)|\b(?:aprob|sancion)[^.]{0,120}Concejo Deliberante",
                          re.IGNORECASE)

# Con o sin guiones: la S.O. 25/2026 trae "1445/2026   Tema:" y las anteriores "1445/2026 --- Tema:".
EXP_TEMA_PAT = re.compile(r"^\s*(\d{1,6}(?:-\d{1,6})?/\d{4})\s*[—–-]*\s*Tema(?:\s*\d+)?:[ \t]*(\S.*)$", re.IGNORECASE | re.MULTILINE)
TOKEN_WARNING = timedelta(days=7)  # anticipación del aviso de vencimiento del token de Instagram
PREFIX_LEN = 30  # la línea del tema suele seguir en la siguiente: alcanza con comparar el comienzo


def missing_links(links, index):
    """PDF de la página que no están en el índice."""
    known = {r.get("source_url") for r in index}
    return [u for u in links if u not in known]


def lost_expedientes(text, items):
    """Expedientes del PDF cuyo tema no aparece en ningún ítem. Se compara el texto, no el número: la clave de un ítem
    puede ser el expediente municipal (05001-112/2024) y no el del proyecto (429/2026)."""
    parsed = normalize_text(" ".join(f"{i.get('key')} {i.get('title')}" for i in items))
    return [e for e, tema in dict.fromkeys(EXP_TEMA_PAT.findall(text))
            if e not in parsed and normalize_text(tema)[:PREFIX_LEN] not in parsed]


def recent_news(urls, today):
    """Notas de los últimos días, sin repetir."""
    out = {}
    for u in urls:
        m = NEWS_URL_PAT.search(u)
        if m and today - NEWS_WINDOW <= date(*map(int, m.groups()[:3])) <= today:
            out.setdefault(m.group(4), u)  # por id: la misma nota aparece con más de una URL
    return list(out.values())


def keywords(text):
    """Raíces de 6 letras, para que "tarifas" y "tarifario" coincidan."""
    return {w[:6] for w in re.findall(r"[a-zñ]{5,}", normalize_text(text))} - STOPWORDS


def has_item(title, items):
    """Hay un ítem que comparte al menos MATCH_MIN de las palabras del titular."""
    words = keywords(title)
    return not words or any(len(words & keywords(f"{i.get('title')} {i.get('que_dice')}")) / len(words) >= MATCH_MIN
                            for i in items)


def recent_items(index, today):
    items = []
    for r in index:
        dt = parse_date_any(r.get("date"))
        if dt != datetime.min and timedelta(0) <= today - dt.date() <= SESSIONS_WINDOW:
            items += (load_processed_json(DATASET_REPO_ID, r["doc_id"]) or {}).get("items") or []
    return items


def check_news(index, today):
    """Advertencias del cruce con La Montaña. Si el sitio falla o cambia no hay advertencias: se avisa solo en el log."""
    try:
        urls = []
        for page, params in ((LA_MONTANA, None), (urljoin(LA_MONTANA, "buscador/"), {"s": "concejo deliberante"})):
            r = session.get(page, params=params, timeout=30)
            r.raise_for_status()
            urls += [urljoin(LA_MONTANA, a["href"]) for a in BeautifulSoup(r.text, "html.parser").find_all("a", href=True)]
        items = recent_items(index, today)
        out = []
        for u in recent_news(urls, today):
            r = session.get(u, timeout=30)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            title = (soup.title.string if soup.title else "").split("|")[0].strip()
            if APROBADO_PAT.search(soup.get_text(" ", strip=True)) and not has_item(title, items):
                out.append(f"La Montaña / FM de la Montaña menciona algo del Concejo sin ítem parecido: {title} ({u})")
        return out
    except requests.RequestException as e:
        log(f"⚠️ No se pudo leer La Montaña ({type(e).__name__}): sigo sin ese cruce.")
        return []


def check_links(year, index):
    page_url = f"https://prensacd.cdsma.gob.ar/ordenes-del-dia-{year}/"
    try:
        links = fetch_pdf_links(page_url)
    except requests.RequestException as e:
        return [f"No se pudo leer {page_url} ({type(e).__name__})."]
    if not links:
        return [f"{page_url} no tiene ningún PDF: ¿cambió la página?"]
    links += [u for u in fetch_special_media_links(year) if u not in links]
    return [f"PDF sin indexar: {u}" for u in missing_links(links, index)]


def check_items(year, index):
    problems = []
    for r in index:
        if str(r.get("session_year")) != str(year):
            continue
        label = f"Sesión {r.get('session_kind')} N° {r.get('session_number')}"
        items = (load_processed_json(DATASET_REPO_ID, r["doc_id"]) or {}).get("items") or []
        path = hf_hub_download(DATASET_REPO_ID, r["pdf_path"], repo_type="dataset", token=os.getenv("HF_TOKEN") or None)
        with open(path, "rb") as f:
            lost = lost_expedientes(extract_text_from_pdf_bytes(f.read()), items)
        if not items:
            problems.append(f"{label}: 0 ítems procesados.")
        elif lost:
            problems.append(f"{label}: expedientes que no quedaron como ítem: {', '.join(lost)}.")
    return problems


def check_token_expiry(today):
    """Instagram no informa cuándo vence el token: la fecha se carga a mano al generarlo, en IG_TOKEN_EXPIRES."""
    try:
        expires = date.fromisoformat(os.getenv("IG_TOKEN_EXPIRES", "").strip())
    except ValueError:
        return ["Falta o no es válido el secret IG_TOKEN_EXPIRES (AAAA-MM-DD): no se puede avisar cuándo vence el token de Instagram."]
    if expires - today > TOKEN_WARNING:
        return []
    return [f"El token de Instagram vence el {expires:%d/%m/%Y}: generá uno nuevo y actualizá IG_ACCESS_TOKEN e IG_TOKEN_EXPIRES."]


def send_alert(problems):
    """True si el mail salió. Las credenciales son secrets de GitHub (contraseña de aplicación de Gmail)."""
    user, password, to = (os.getenv(k, "").strip() for k in ("ALERT_SMTP_USER", "ALERT_SMTP_PASSWORD", "ALERT_EMAIL_TO"))
    if not (user and password and to):
        log("⚠️ Faltan los secrets ALERT_SMTP_USER, ALERT_SMTP_PASSWORD o ALERT_EMAIL_TO: no se manda el mail.")
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = f"Actas SMA: {len(problems)} problema(s) en el control diario", user, to
    msg.set_content("\n".join(f"- {p}" for p in problems))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        log(f"⚠️ No se pudo mandar el mail ({type(e).__name__}).")
        return False
    return True


def main():
    now = datetime.now()
    index = load_index(DATASET_REPO_ID)
    problems = check_links(now.year, index) + check_items(now.year, index)
    warnings = check_news(index, now.date()) + check_token_expiry(now.date())
    for p in problems:
        log(f"❌ {p}")
    for w in warnings:
        log(f"⚠️ {w}")
    if problems or warnings:
        send_alert(problems + warnings)
    if not problems:
        log(f"✅ Control OK: índice al día con la página de {now.year}.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
