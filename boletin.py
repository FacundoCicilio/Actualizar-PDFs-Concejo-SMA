# Ordenanzas promulgadas del Boletín Oficial Municipal (http://boletinoficial.sma.gob.ar), una por número, con la
# sesión en que se sancionaron. Las lee instagram_publisher.py para destacados y para la puesta al día; "Qué se aprobó"
# sigue solo con Prensa. El sitio es un WordPress viejo sin API: los boletines semanales son PDF enlazados en la portada.
# Se guarda en el dataset privado de HF solo el texto anonimizado; de las adjudicaciones a particulares, ni eso.
import hashlib
import re
from datetime import date

import pymupdf
import requests

import instagram_carousel as ic
import prensa
from actas_parser import MONTHS, normalize_text
from hf_dataset_store import DATASET_REPO_ID, load_json, save_json

BASE = "http://boletinoficial.sma.gob.ar"
STORE_PATH = "boletin/boletines.json"
RECHECK = 3  # los últimos boletines guardados se revisan por si los reemplazaron con el mismo nombre

LINK_PAT = re.compile(r"""href=["'](https?://boletinoficial\.sma\.gob\.ar/wp-content/uploads/\d{4}/\d{2}/bom-(\d{3,4})[^"'/]*\.pdf)["']""", re.I)
NOISE_PAT = re.compile(r"^\s*(?:BOLET[IÍ]N OFICIAL MUNICIPAL N.*|“DONAR ORGANOS.*|“TEN[EÉ]S DERECHO.*|\d{1,3})\s*$", re.M)
_MES = "|".join(MONTHS)
SESSION_HEAD_PAT = re.compile(rf"SESI[OÓ]N\s+N[º°o]\s*(\d{{1,2}})\s+DEL\s+CONCEJO\s+DELIBERANTE[\s\S]{{0,120}}?DE\s+FECHA\s+"
                              rf"(\d{{1,2}})\s+DE\s+({_MES})\s+DEL?\s+(\d{{4}})", re.I)
ORD_HEAD_PAT = re.compile(r"ORDENANZA\s+N[º°o]\s*(\d{1,2})\.?(\d{3})\s*/\s*(\d{2,4})\s+PROMULGADA\s+POR\s+DECRETO\s+N[º°o]\s*"
                          r"(\d+)(?:\s*/\s*\d{2,4})?\s+DE\s+FECHA\s+(\d{1,3})/(\d{1,2})/(\d{4})", re.I)  # "028/10/2026" (sic, bom-826)
# Respaldo si el boletín no trae el título de la sesión antes de la ordenanza.
SESSION_TEXT_PAT = re.compile(rf"Sesi[oó]n\s+(Ordinaria|Especial)\s+N[º°o]?\s*(\d{{1,2}})\s+del?\s+(?:d[ií]a\s+)?(\d{{1,2}})\s+de\s+"
                              rf"({_MES})\s+del?\s+(\d{{4}})", re.I)
END_PAT = re.compile(r"^\s*(?:ANEXO\b|RESUMEN AUDIENCIA|DECRETOS?\s*$|RESOLUCI[OÓ]N(?:ES)?\s+N[º°])", re.M | re.I)
LAST_ARTICLE_PAT = re.compile(r"Comun[ií]quese al Departamento Ejecutivo Municipal\.?-?", re.I)
ARTICLE1_PAT = re.compile(r"ART[ÍI]CULO\s*1\s*[º°]?\s*\.?\s*-?\s*(.+?)(?=ART[ÍI]CULO\s*2\b|$)", re.I | re.S)
DNI_PAT = re.compile(r"\b(?:D\.?\s*N\.?\s*I\.?|L\.?E\.?|L\.?C\.?)\s*:?\s*(?:N[º°o]\.?\s*)?[A-Z]?[\d.,\s]{6,12}\d", re.I)
# CUIT/CUIL: 20, 23, 24 y 27 son personas; 30, 33 y 34, empresas e instituciones (esas no hacen particular a la norma).
CUIT_PAT = re.compile(r"\bC\.?U\.?I\.?[TL]\.?\s*:?\s*(?:N[º°o]\.?\s*)?(\d{2})\s*-?\s*[\d.]{7,10}\s*-?\s*\d", re.I)
PERSON_CUIT = {"20", "23", "24", "27"}
ADJ_PAT = re.compile(r"adjud[ií]c|usufructo", re.I)  # "ADJUDÍCASE"
CHACRA_PAT = re.compile(r"\bChacra\s+(\d+)", re.I)
LOTE_PAT = re.compile(r"\bLotes?\s+([A-Z0-9][\w-]*)", re.I)


def discover(html):
    """{número: url} de los boletines enlazados en la portada (también las "edición especial"; no los decretos sueltos)."""
    return {int(n): url for url, n in LINK_PAT.findall(html)}


def probe_urls(last, today):
    """Dónde estaría el boletín siguiente si la portada todavía no lo lista: carpeta de este mes y del anterior."""
    prev = date(today.year - (today.month == 1), (today.month - 2) % 12 + 1, 1)
    return [f"{BASE}/wp-content/uploads/{d:%Y/%m}/bom-{last + 1}{suffix}.pdf"
            for d in (today, prev) for suffix in ("", "-edicion-especial")]


def pdf_text(data):
    """Texto en orden de lectura: los bloques ordenados por posición (el orden interno del PDF pone los títulos de las
    ordenanzas después de su articulado, y get_text(sort=True) pierde espacios en las líneas justificadas)."""
    pages = []
    for page in pymupdf.open(stream=data, filetype="pdf"):
        pages.append("".join(b[4] for b in page.get_text("blocks", sort=True) if b[6] == 0))
    return clean("\n".join(pages))


def clean(text):
    """Sin pie de página (número de boletín, leyendas, número de página): corta el articulado en el medio."""
    return NOISE_PAT.sub("", text)


def _date(d, mes, y):
    try:
        return date(int(y), MONTHS[mes.lower()], int(d))
    except (KeyError, ValueError):
        return None


def _session(number, kind, d):
    return {"key": prensa.session_key(number, d.year, kind), "number": str(int(number)), "kind": kind, "year": d.year,
            "date": d.isoformat()}


def scrub(text):
    """Sin documentos de identidad ni nombres de particulares (lo que ya hace la anonimización de las notas)."""
    return re.sub(r"\s+", " ", ic.anonymize(CUIT_PAT.sub("", DNI_PAT.sub("", text)))).strip()


def is_particular(body):
    """Una persona con nombre o documento en el articulado: licencias, excepciones o adjudicaciones a un particular."""
    return leaks(body)


def parse(text, numero, url):
    """Ordenanzas de un boletín: [{numero, label, decreto, promulgada, session, boletin, url, ...}]. Las de particulares
    no guardan texto: las adjudicaciones guardan solo la chacra y cuántos lotes (salen agregadas); el resto, nada."""
    heads = [("s", m) for m in SESSION_HEAD_PAT.finditer(text)] + [("o", m) for m in ORD_HEAD_PAT.finditer(text)]
    heads.sort(key=lambda h: h[1].start())
    out, session = [], None
    for i, (kind, m) in enumerate(heads):
        if kind == "s":
            d = _date(m.group(2), m.group(3), m.group(4))
            especial = "ESPECIAL" in text[max(0, m.start() - 60):m.start()].upper()
            session = _session(m.group(1), "Especial" if especial else "Ordinaria", d) if d else None
            continue
        end = heads[i + 1][1].start() if i + 1 < len(heads) else len(text)
        body = text[m.end():end]
        cut = END_PAT.search(body)
        body = body[:cut.start()] if cut else body
        last = LAST_ARTICLE_PAT.search(body)
        body = body[:last.end()] if last else body
        num, year = int(m.group(1) + m.group(2)), int(m.group(3)) % 100
        try:
            promulgada = date(int(m.group(7)), int(m.group(6)), int(m.group(5))).isoformat()
        except ValueError:
            promulgada = None
        o = {"numero": num, "label": f"{num // 1000}.{num % 1000:03d}/{year:02d}", "decreto": int(m.group(4)),
             "promulgada": promulgada, "boletin": numero, "url": url, "session": session}
        if session is None:
            s = SESSION_TEXT_PAT.search(body)
            d = s and _date(s.group(3), s.group(4), s.group(5))
            if d:
                o["session"] = _session(s.group(2), s.group(1).capitalize(), d)
        o["a_revisar"] = o["session"] is None  # sin sesión no se publica: queda listada en la vista previa
        if is_particular(body):
            o["particular"] = True
            if ADJ_PAT.search(body) and CHACRA_PAT.search(body):
                o["adjudicacion"] = {"chacra": CHACRA_PAT.search(body).group(1),
                                     "lotes": max(1, len(set(LOTE_PAT.findall(body))))}
        else:
            o["text"] = scrub(body)
        out.append(o)
    return out


# ------------------------------------------------------------------ descarga y guardado
def _get(url, method="GET"):
    return requests.request(method, url, timeout=60, allow_redirects=True)


def load(today, save=True):
    """Boletines y ordenanzas guardados más los nuevos del sitio. Lanza requests.RequestException si el sitio falla.
    Devuelve {"boletines": {n: {url, size, sha256}}, "ordenanzas": {numero: ordenanza}}."""
    store = load_json(DATASET_REPO_ID, STORE_PATH) or {"boletines": {}, "ordenanzas": {}}
    r = _get(BASE + "/")
    r.raise_for_status()
    found = discover(r.text)
    known = {int(k) for k in store["boletines"]}
    last = max(known | set(found), default=0)
    if last:  # la portada a veces tarda en listar el último: se prueba el número siguiente
        for url in probe_urls(last, today):
            if _get(url, "HEAD").status_code == 200:
                found[last + 1] = url
                break
    todo = {n: u for n, u in found.items() if n not in known}
    for n in sorted(known)[-RECHECK:]:  # reemplazado con el mismo nombre: cambia el tamaño
        b = store["boletines"][str(n)]
        h = _get(b["url"], "HEAD")
        if h.status_code == 200 and h.headers.get("Content-Length") not in (None, str(b["size"])):
            todo[n] = b["url"]
    changed = False
    for n, url in sorted(todo.items()):
        resp = _get(url)
        resp.raise_for_status()
        sha = hashlib.sha256(resp.content).hexdigest()
        if store["boletines"].get(str(n), {}).get("sha256") == sha:
            continue
        for o in parse(pdf_text(resp.content), n, url):
            store["ordenanzas"][str(o["numero"])] = o
        store["boletines"][str(n)] = {"url": url, "size": len(resp.content), "sha256": sha}
        changed = True
    if changed and save:
        save_json(DATASET_REPO_ID, STORE_PATH, store, f"Boletín Oficial: {len(todo)} boletines nuevos o reemplazados")
    return store


# ------------------------------------------------------------------ como notas de Prensa (mismo criterio de destacado)
def as_note(o):
    """La ordenanza con la forma de una nota de Prensa, para usar prensa.themes, is_destacado, rank y las placas."""
    m = ARTICLE1_PAT.search(o["text"])
    note = {"id": f"ord-{o['numero']}", "date": o["promulgada"], "link": o["url"],
            "title": ic.shorten((m.group(1) if m else o["text"]).strip(" .-"), 300), "text": o["text"],
            "ordenanza": {"label": o["label"], "boletin": o["boletin"]}}
    note["impact_tags"] = prensa.impact_tags(note)
    return note


def session_of(o):
    s = o.get("session")
    return s and {**s, "date": date.fromisoformat(s["date"])}


# Palabras que comparten casi todas las normas: no sirven para decir que una nota y una ordenanza hablan de lo mismo.
COMMON = set(normalize_text(
    "concejo deliberante municipal municipalidad departamento ejecutivo intendente ordenanza ordenanzas articulo san "
    "martin andes provincia neuquen ciudad comuniquese autorizase apruebase convenio secretaria direccion sesion "
    "ordinaria especial presente anexo forma parte adenda sancion aprobo autorizo aprobada aprueba autoriza decreto "
    "recursos centro gobierno mesa asociacion civil nacional consejo desarrollo provincial sostenible planificacion "
    "subsecretaria comision barrio chacra lote lotes manzana junta juntas vecinal vecinales").split())


def tokens(text):
    """Datos distintivos: cifras de 3 o más dígitos (montos, superficies, normas) y nombres propios. Las de 1 o 2
    (números de sesión, días, porcentajes chicos) coinciden por casualidad."""
    nums = {n for n in ic.numbers(text) if len(n) >= 3 and not re.fullmatch(r"20\d\d|0+", n)}
    names = {normalize_text(w) for w in re.findall(r"\b[A-ZÁÉÍÓÚÑ][a-záéíóúñ]{3,}\b", text)}
    return nums | (names - COMMON)


def decided(aud_notes, ord_notes):
    """La ordenanza que resolvió lo que trató una audiencia pública, o None. Además de 3 datos en común, al menos una
    cifra (la ordenanza anterior, la superficie, la nomenclatura): los nombres solos se repiten entre clubes y juntas."""
    mine = set().union(*(tokens(f"{n['title']} {n['text']}") for n in aud_notes))
    best = max(((mine & tokens(f"{o['title']} {o['text']}"), o) for o in ord_notes), key=lambda x: len(x[0]), default=(set(), None))
    shared, o = best
    return o if len(shared) >= DUP_MIN and any(t.isdigit() for t in shared) else None


DUP_MIN, DOUBT_MIN = 3, 2  # datos en común: desde 3 es la misma decisión; con 2, dudoso (no se publica)


def match(o_note, notes):
    """("duplicado" | "dudoso" | "nueva", nota de Prensa más parecida o None)."""
    mine = tokens(f"{o_note['title']} {o_note['text']}")
    best = max(((len(mine & tokens(f"{n['title']} {n['text']}")), n) for n in notes), key=lambda x: x[0], default=(0, None))
    if best[0] >= DUP_MIN:
        return "duplicado", best[1]
    return ("dudoso", best[1]) if best[0] >= DOUBT_MIN else ("nueva", None)


def adjudicaciones(ords):
    """Adjudicaciones a particulares, solo agregadas por chacra: [{chacra, lotes, labels}]. Sin lote ni manzana."""
    by = {}
    for o in ords:
        if o.get("adjudicacion"):
            g = by.setdefault(o["adjudicacion"]["chacra"], {"chacra": o["adjudicacion"]["chacra"], "lotes": 0, "labels": []})
            g["lotes"] += o["adjudicacion"]["lotes"]
            g["labels"].append(o["label"])
    return list(by.values())


def leaks(text):
    """Control final antes de publicar: un documento de identidad o un nombre de particular en el texto que sale."""
    return (bool(DNI_PAT.search(text)) or any(m.group(1) in PERSON_CUIT for m in CUIT_PAT.finditer(text))
            or ic.anonymize(text) != text)
