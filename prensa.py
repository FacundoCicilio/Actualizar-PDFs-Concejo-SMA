# Notas de la sección Prensa del Concejo (WordPress, https://prensacd.cdsma.gob.ar/prensa/), agrupadas por sesión.
# Las lee instagram_publisher.py para los posts "Qué se aprobó" y "Destacado". Se guardan en el dataset privado de HF.
import html
import re
from datetime import date, datetime

import requests

import instagram_carousel as ic
from actas_parser import MONTHS, infer_impact
from hf_dataset_store import DATASET_REPO_ID, load_json, save_json

API = "https://prensacd.cdsma.gob.ar/wp-json/wp/v2/posts"
CATEGORY = 3  # "Prensa"
NOTES_PATH = "prensa/notas.json"
DESTACADO_TAGS = {"💰 Bolsillo", "🚰 Servicios", "🏗 Infraestructura"}

SESSION_PAT = re.compile(r"(?P<n1>\d{1,2})\s*[ªº°]?\s*Sesi[oó]n\s+(?P<k1>Ordinaria|Especial)"
                         r"|Sesi[oó]n\s+(?P<k2>Ordinaria|Especial)\s+N\.?\s*[°º]?\s*(?P<n2>\d{1,2})", re.I)
YEAR_PAT = re.compile(r"^\s*(?:del\s+año|de)\s+(\d{4})")
DATE_PAT = re.compile(r"realizada\s+(?:el|este)\s+(?:[a-zé]+\s+)?(\d{1,2})\s+de\s+([a-z]+)", re.I)
# Verbos que confirman que el Concejo lo votó a favor: en el título o en la misma oración que la sesión.
APPROVED_PAT = re.compile(r"\b(?:aprob[oó]|fue\s+aprobad[oa]|otorg[oó]|declar[oó]|fue\s+declarad[oa]|distingui[oó]"
                          r"|sancion[oó]|autoriz[oó]|exim(?:i[oó]|e)|ratific[oó])\b", re.I)
NOT_APPROVED_PAT = re.compile(r"\btratar[aá]\b|\brechaz|\bEstudiantil\b", re.I)
EXCLUDED_TITLE_PAT = re.compile(r"Audiencia\s+P[uú]blica|Estudiantil", re.I)


def plain(rendered):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", rendered or ""))).strip()


def fetch():
    """Últimas 100 notas de Prensa como {id: nota}. Lanza requests.RequestException o ValueError si el sitio falla."""
    r = requests.get(API, timeout=30, params={"categories": CATEGORY, "per_page": 100,
                                              "_fields": "id,date,modified,link,title,content"})
    r.raise_for_status()
    return {str(p["id"]): {"id": p["id"], "date": p["date"], "modified": p["modified"], "link": p["link"],
                           "title": plain(p["title"]["rendered"]), "text": plain(p["content"]["rendered"])}
            for p in r.json()}


def load_notes(save=True):
    """Notas guardadas más las nuevas o editadas del sitio. Con save, guarda en el dataset solo si algo cambió."""
    stored = load_json(DATASET_REPO_ID, NOTES_PATH) or {}
    fresh = {k: n for k, n in fetch().items() if stored.get(k, {}).get("modified") != n["modified"]}
    if fresh:
        stored.update(fresh)
        if save:
            save_json(DATASET_REPO_ID, NOTES_PATH, stored, f"Prensa: {len(fresh)} notas nuevas o editadas")
    return stored


def _sentence(text, start, end):
    a = text.rfind(". ", 0, start)
    b = text.find(". ", end)
    return text[a + 2 if a >= 0 else 0: b if b >= 0 else len(text)]


def session_of(note):
    """{key, number, kind, year, date} de la primera sesión que menciona una nota que confirma una aprobación, o None
    (sin sesión, "tratará", "rechazó", Concejo Estudiantil, audiencias públicas)."""
    if EXCLUDED_TITLE_PAT.search(note["title"]):
        return None
    text = note["text"]
    m = SESSION_PAT.search(text)
    if not m:
        return None
    sentence = _sentence(text, m.start(), m.end())
    if not APPROVED_PAT.search(f"{note['title']}. {sentence}") or NOT_APPROVED_PAT.search(sentence):
        return None
    number, kind = (m.group("n1"), m.group("k1")) if m.group("n1") else (m.group("n2"), m.group("k2"))
    kind = kind.capitalize()
    y = YEAR_PAT.search(text[m.end():])
    year = int(y.group(1)) if y else int(note["date"][:4])
    d = DATE_PAT.search(sentence)
    session_date = None
    if d and MONTHS.get(d.group(2).lower()):
        try:
            session_date = date(year, MONTHS[d.group(2).lower()], int(d.group(1)))
        except ValueError:
            pass
    return {"key": session_key(number, year, kind), "number": str(int(number)), "kind": kind, "year": year,
            "date": session_date}


def session_key(number, year, kind="Ordinaria"):
    return f"{year}-{kind[0].upper()}{int(number)}"


def sessions(notes):
    """{clave: {key, number, kind, year, date, notes}} solo con notas aprobadas, de la más vieja a la más nueva.
    Fecha: la que dice alguna nota ("realizada el jueves 8 de octubre"); si ninguna, la de la primera nota publicada."""
    out = {}
    for note in sorted(notes.values(), key=lambda n: n["date"]):
        s = session_of(note)
        if not s:
            continue
        cur = out.setdefault(s["key"], {**s, "notes": []})
        cur["date"] = cur["date"] or s["date"]
        cur["notes"].append({**note, "impact_tags": impact_tags(note)})
    for s in out.values():
        s["date"] = s["date"] or datetime.fromisoformat(s["notes"][0]["date"]).date()
    return out


def impact_tags(note):
    # título y comienzo: el resto de la nota suele traer fundamentos que suman etiquetas por palabras sueltas
    return infer_impact(f"{note['title']}. {note['text'][:400]}")["impact_tags"]


def rank(notes):
    """Primero las de alcance general, después el resto; dentro de cada grupo, mayor impacto primero."""
    return sorted(notes, key=lambda n: (ic.is_relevant(n), ic.impact_score(n)), reverse=True)


def is_destacado(note):
    return ic.is_relevant(note) and bool(DESTACADO_TAGS & set(note["impact_tags"]))


def note_text(note):
    """Lo que se manda a Claude: anonimizado, como en el orden del día."""
    return ic.anonymize(f"{note['title']}\n\n{note['text']}")


def session_doc(s):
    """La sesión con la forma que usan las placas (session_number, session_kind, date dd/mm/aaaa)."""
    return {"session_number": s["number"], "session_kind": s["kind"], "session_year": s["year"],
            "date": s["date"].strftime("%d/%m/%Y")}
