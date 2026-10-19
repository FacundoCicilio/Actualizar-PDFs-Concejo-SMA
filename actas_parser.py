# Parser único de órdenes del día / actas: lo usan app.py y auto_fetch.py.
import re
import hashlib
import unicodedata
from collections import Counter
from datetime import datetime

import fitz  # PyMuPDF

from hf_dataset_store import PROCESSOR_VERSION, normalize_session_number

def normalize_space(s):
    s = (s or "").replace("\u00a0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def find_first(patterns, text, flags=re.IGNORECASE):
    for p in patterns:
        m = re.search(p, text, flags)
        if m:
            return m.group(1).strip()
    return None




def sha256_bytes(data):
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def _strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def normalize_text(s):
    if not s: return ""
    s = str(s).lower()
    s = _strip_accents(s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def normalize_name(s):
    s = normalize_text(s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


BOILERPLATE_PATTERNS = [
    r"Donar\s+Órganos\s+y\s+Sangre\s+es\s+Donar\s+Vida",
    r"Donar\s+Organos\s+y\s+Sangre\s+es\s+Donar\s+Vida",
    r"Inf[oó]rmese,?\s*sea\s+donante",
    r"\bsea\s+donante\b",
    r"(?:https?:\/\/)?(?:www\.)?incucai\.gov\.ar",
    r"CONCEJO\s+DELIBERANTE\s+SAN\s+MARTIN\s+DE\s+LOS\s+ANDES",
    r"CONCEJO\s+DELIBERANTE",
    r"P[áa]gina\s+\d+\s+de\s+\d+",
    r"\bP[áa]g\.\s*\d+\b",
    r"^P[áa]gina\s*\d+$",  # pie "Página2": si queda, corta el ítem que sigue en la página siguiente
]


def remove_boilerplate_lines(text):
    lines = (text or "").splitlines()
    counts = Counter(ln.strip() for ln in lines if ln.strip())
    cleaned = []
    for ln in lines:
        ln_strip = ln.strip()
        if not ln_strip:
            cleaned.append("")
            continue
        if any(re.search(p, ln_strip, re.IGNORECASE) for p in BOILERPLATE_PATTERNS):
            continue
        # Encabezado de página (lema del año, "SAN MARTIN DE LOS ANDES"): se repite igual en cada hoja.
        # shortcut: un PDF de 2 páginas no llega a 3 repeticiones y su encabezado queda; bajar el umbral si molesta.
        if counts[ln_strip] >= 3:
            continue
        ln_strip = re.sub(r"^[\-\•\●\·\*]+\s*", "", ln_strip).strip()
        cleaned.append(ln_strip)
    return "\n".join(cleaned)


def remove_boilerplate_global(text):
    t = text or ""
    for p in BOILERPLATE_PATTERNS:
        t = re.sub(p, " ", t, flags=re.IGNORECASE)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n\s+\n", "\n\n", t)
    return t.strip()


def is_boilerplate_text(s):
    return any(re.search(p, s or "", re.IGNORECASE) for p in BOILERPLATE_PATTERNS)


# ============================================================
# FILTROS DE BASURA (nuevo)
# ============================================================
# Encabezado de cada dictamen: "1. 05001-82/2024 28/09/2026 …". Variantes reales: "3.05001-544/2006" (sin espacio),
# "05001-210 /2026", "(s/e)" antes de la fecha, y el nombre del concejal antes de "Miembro Informante".
HEADER_PAT = re.compile(
    r"^(?:\d{1,2}\.\s*)?(?:\d{5,6}[-/]\d{1,6}\s*/\d{4}|EE-\d{4}-\d+\S*)\s+(?:\(s/e\)\s+)?\d{2}/\d{2}/\d{4}\b",
    re.IGNORECASE,
)
MIEMBRO_PAT = re.compile(r"^Miembro\s+Informante:", re.IGNORECASE)
# Títulos de sección/comisión que no son parte del ítem anterior: "D) Comisiones", "De Vivienda y Hábitat",
# "Por Acuerdo Parlamentario", "Solicitud Banca del vecino", "ANEXO 62".
SECTION_PAT = re.compile(
    r"^(?:[A-Z]\)\s.*"
    r"|De\s+[A-ZÁÉÍÓÚÑ]\w*(?:\s+(?:[A-ZÁÉÍÓÚÑ]\w*|y|e|de|del))*"
    r"|(?:Por\s+)?Acuerdo\s+Parlamentario"
    r"|Solicitud\s+(?:de\s+)?Banca\s+del\s+[Vv]ecino"
    r"|ANEXOS?\s+[\d\s,y–-]+)$"
)

BASURA_PATTERNS = [HEADER_PAT, MIEMBRO_PAT]

def is_basura(s):
    s = (s or "").strip()
    for p in BASURA_PATTERNS:
        if p.search(s):
            return True
    return False

# Prefijos de encabezado que el PDF repite antes del tema: "--- Tema:", "Proyecto 1 -", "Proyecto 1)", "Dictamen s/".
LEADING_NOISE_PAT = re.compile(
    r"^(?:[\s—–-]+|Tema:\s*|Proyecto\s+\d+\s*[\)\-–—:.]\s*|Dictamen\s+s/\s*|\d+/\d{4}\s*[-—–]+\s*)",
    re.IGNORECASE,
)

def clean_que_dice(s):
    s = normalize_space(s)
    prev = None
    while s != prev:  # se combinan en cualquier orden ("-- Tema: Proyecto 1 - Dictamen s/ ...")
        prev, s = s, LEADING_NOISE_PAT.sub("", s, count=1)
    s = re.sub(r"\s*\.-\s*De\s+\w+\s*$", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"\s*\.-\s*$", "", s).strip()
    # Quitar "Página N" al final
    s = re.sub(r"\s*Página\s*\d+.*$", "", s, flags=re.IGNORECASE).strip()
    return s

# Patrón SMA: "1. expediente   fecha   Miembro Informante: Concejal X"
MI_PAT = re.compile(
    r"^(\d{1,2})\.\s+((?:\d{5,6}[-/]\d{1,6}/\d{4}|EE-\d{4}-\d+\S*))\s+\d{2}/\d{2}/\d{4}\s+Miembro\s+Informante:",
    re.IGNORECASE
)
TEMA_LINE_PAT = re.compile(r"^\d+/\d{4}\s*---\s*Tema:\s*(.+)$", re.IGNORECASE)


def extract_text_from_pdf_bytes(pdf_bytes, max_pages=80):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    out = []
    n_pages = min(len(doc), max_pages)
    for i in range(n_pages):
        page = doc.load_page(i)
        words = page.get_text("words") or []
        if not words: continue
        words.sort(key=lambda w: (round(w[1], 1), round(w[0], 1)))
        line_bins = {}
        for w in words:
            bin_y = int(round(w[1] / 3.0))
            line_bins.setdefault(bin_y, []).append(w)
        for bin_y in sorted(line_bins.keys()):
            line_words = sorted(line_bins[bin_y], key=lambda w: w[0])
            txt = " ".join([w[4] for w in line_words]).strip()
            if txt: out.append(txt)
        out.append("")
    doc.close()
    return "\n".join(out)


MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}


def parse_spanish_date_any(s):
    s = (s or "").strip()
    m = re.search(r"\b(\d{1,2})\s+de\s+([A-Za-zÁÉÍÓÚáéíóúñÑ]+)\s+de\s+(20\d{2})\b", s, re.IGNORECASE)
    if not m: return None
    d, mn, y = int(m.group(1)), m.group(2).lower(), int(m.group(3))
    mo = MONTHS.get(mn)
    if not mo: return None
    try: return datetime(y, mo, d)
    except: return None


def extract_cover_lines(pdf_bytes):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    if len(doc) == 0: return []
    page = doc.load_page(0)
    words = page.get_text("words") or []
    doc.close()
    if not words: return []
    words.sort(key=lambda w: (round(w[1], 1), round(w[0], 1)))
    line_bins = {}
    for w in words:
        bin_y = int(round(w[1] / 3.0))
        line_bins.setdefault(bin_y, []).append(w)
    lines = []
    for bin_y in sorted(line_bins.keys()):
        line_words = sorted(line_bins[bin_y], key=lambda w: w[0])
        txt = " ".join([w[4] for w in line_words]).strip()
        if txt: lines.append(txt)
    return lines


def extract_box_header_info(pdf_bytes):
    lines = extract_cover_lines(pdf_bytes)
    if not lines: return {}
    page_text = "\n".join(lines)
    stype = None
    if re.search(r"\bORDEN\s+DEL\s+D[IÍ]A\b", page_text, re.IGNORECASE): stype = "ORDEN DEL DIA"
    elif re.search(r"\bACTA\b", page_text, re.IGNORECASE): stype = "ACTA"
    mnum = re.search(r"\bSESIO[NÓO]\s+(?:ORDINARIA|EXTRAORDINARIA|ESPECIAL)\s+N(?:ro|°|º|o)?[:\.]?\s*0*(\d{1,3})\b", page_text, re.IGNORECASE)
    sn = str(int(mnum.group(1))) if mnum else None
    my = re.search(r"\bA[ñn]o\s+(20\d{2})\b", page_text, re.IGNORECASE)
    sy = int(my.group(1)) if my else None
    dt = parse_spanish_date_any(page_text)
    date_str = None
    if dt:
        date_str = dt.strftime("%d/%m/%Y")
        if sy is None: sy = dt.year
    out = {}
    if stype: out["session_type"] = stype
    if sn: out["session_number"] = sn
    if sy: out["session_year"] = sy
    if date_str: out["date"] = date_str
    return out


def infer_num_year_from_filename(filename):
    if not filename: return (None, None)
    f = filename.lower()
    m = re.search(r"\bno[-_\s]*0*(\d{1,3})[-_](\d{2})\b", f)
    if m: return (int(m.group(1)), 2000 + int(m.group(2)))
    m = re.search(r"\bno[-_\s]*0*(\d{1,3})[-_](20\d{2})\b", f)
    if m: return (int(m.group(1)), int(m.group(2)))
    m = re.search(r"\b[-_](\d{1,3})[-_](20\d{2})\b", f)
    if m: return (int(m.group(1)), int(m.group(2)))
    m = re.search(r"\b(\d{1,3})[-_](\d{2})\b", f)
    if m: return (int(m.group(1)), 2000 + int(m.group(2)))
    return (None, None)




def parse_date_any(s):
    if not s: return datetime.min
    s = s.strip()
    m = re.match(r"(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{2,4})", s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100: y += 2000
        try: return datetime(y, mo, d)
        except: return datetime.min
    dt = parse_spanish_date_any(s)
    if dt: return dt
    return datetime.min


def fmt_date_short(dt, fallback="s/f"):
    if dt == datetime.min: return fallback
    return dt.strftime("%d/%m/%Y")


TOPIC_RULES = [
    ("Agua/Servicios", ["agua", "cloaca", "gas", "energía", "luz", "residuos", "basura", "saneamiento"]),
    ("Tarifas", ["tarifa", "tasas", "impuesto", "tribut", "arancel", "reajuste", "actualización"]),
    ("Obra pública", ["obra", "pavimento", "asfalto", "vereda", "plaza", "puente", "licitación", "contratación"]),
    ("Seguridad", ["seguridad", "tránsito", "control", "inspección", "alcoholemia", "emergencia"]),
    ("Comercio", ["comercio", "habilitación", "local", "feria", "gastronomía", "turismo"]),
    ("Ambiente", ["ambiente", "bosque", "incendio", "impacto", "contaminación", "reserva"]),
    ("Vivienda/Urbanismo", ["vivienda", "urban", "lote", "zonificación", "catastro", "edificación", "loteo"]),
    ("Educación/Cultura", ["educación", "escuela", "cultura", "biblioteca", "museo", "deporte"]),
]


def _matches(t_norm, keys):
    # sin tildes en ambos lados: el texto de los PDFs a veces viene sin acentos ("energia", "actualizacion")
    return any(normalize_text(k) in t_norm for k in keys)


def classify_topics(text):
    t = normalize_text(text)
    return [label for label, keys in TOPIC_RULES if _matches(t, keys)]


IMPACT_RULES = [
    ("💰 Bolsillo", ["tarifa", "tasa", "impuesto", "tribut", "arancel", "reajuste", "actualización", "módulo", "valor", "exención", "exime", "deuda"]),
    ("🏗 Infraestructura", ["obra", "licitación", "contratación", "pavimento", "asfalto", "vereda", "plaza", "puente"]),
    ("🚦 Tránsito/Seguridad", ["tránsito", "circulación", "corte", "estacionamiento", "control", "inspección", "alcoholemia", "seguridad"]),
    ("🚰 Servicios", ["agua", "cloaca", "gas", "energía", "luz", "residuos", "basura", "saneamiento"]),
    ("🏪 Comercio", ["habilitación", "comercio", "local comercial", "feria", "gastronomía", "licencia comercial"]),
    ("🏘 Urbanismo", ["lote", "loteo", "urban", "zonificación", "catastro", "edificación", "vivienda", "medianera", "retiro de frente"]),
    ("🌿 Ambiente", ["ambiente", "bosque", "incendio", "impacto", "contaminación", "reserva"]),
    ("📚 Educación", ["escuela", "alumno", "estudiante", "educativ", "educación", "epet", "cpem", "jardín de infantes"]),
    ("🎭 Cultura/Eventos", ["jornadas", "congreso", "evento", "festival", "encuentro", "capacitación", "conferencia", "interés municipal", "interés cultural"]),
]


IMPACT_MESSAGES = {
    "💰 Bolsillo": "Puede impactar en montos a pagar (tasas/tarifas/condiciones).",
    "🏗 Infraestructura": "Puede implicar obras, mejoras o cortes asociados.",
    "🚦 Tránsito/Seguridad": "Puede cambiar circulación, controles o normas.",
    "🚰 Servicios": "Puede afectar servicios básicos (agua/cloaca/energía/residuos).",
    "🏪 Comercio": "Puede afectar habilitaciones o comercios.",
    "🏘 Urbanismo": "Puede afectar loteos, zonificación o desarrollo urbano.",
    "🌿 Ambiente": "Puede afectar regulaciones ambientales o áreas naturales.",
    "📚 Educación": "Puede afectar escuelas, alumnos o programas educativos.",
    "🎭 Cultura/Eventos": "Declaración de interés o apoyo a evento/actividad cultural.",
}


# Declaraciones de interés y distinciones (sobre texto sin tildes). \w* tolera "Declararación".
RECONOCIMIENTO_PAT = re.compile(r"\bdeclar\w*\s+de\s+interes\b|\bdistin(?:cion|guio|guid[oa]s?)\b|\bembajador"
                                r"|\b(?:ciudadan|vecin)[oa]s?\s+(?:ilustre|destacad)|\bvisitantes?\s+ilustres?\b"
                                r"|\bllevara\s+el\s+nombre|\bdesigna\w*\s+con\s+el\s+nombre|\bimpon\w*\s+el\s+nombre")


def infer_impact(text):
    t = normalize_text(text)
    if RECONOCIMIENTO_PAT.search(t):
        # un reconocimiento no cambia tasas, obras ni escuelas aunque las nombre de paso ("Asociación Puentes de Luz",
        # "su labor junto a instituciones educativas")
        hits = ["🎭 Cultura/Eventos"]
    else:
        t = re.sub(r"\bno\s+arancelad[oa]s?\b", " ", t)  # "Seminario No Arancelado" no es Bolsillo
        hits = [label for label, keys in IMPACT_RULES if _matches(t, keys)]
    if not hits:
        return {"impact_tags": [], "impact_text": "🧩 Informativo: no se ve un impacto directo claro (puede ser interno/administrativo)."}
    top = hits[:2]
    text_out = " ".join([f"{h}: {IMPACT_MESSAGES.get(h, '')}".strip() for h in top]).strip()
    return {"impact_tags": hits[:4], "impact_text": text_out}


def build_que_dice(raw):
    s = normalize_space(raw)
    if not s: return ""
    if is_basura(s): return ""
    t = s.lower()  # la acción se detecta en el texto original: "Dictamen s/" se borra al limpiar
    s = clean_que_dice(s)
    if not s: return ""
    action = ""
    if "declaración de interés" in t or "declaracion de interes" in t:
        action = "Declaración de Interés"
    elif "dictamen" in t:
        action = "Dictamen"
    elif "decreto" in t:
        action = "Decreto"
    elif "nota" in t:
        action = "Nota"
    elif "habilitación" in t or "habilitacion" in t or "licencia comercial" in t:
        action = "Habilitación"
    elif "convenio" in t:
        action = "Convenio"
    elif "obra" in t or "licitación" in t or "licitacion" in t:
        action = "Obra pública"
    elif "regularización" in t or "regularizacion" in t:
        action = "Regularización"
    elif "autorización" in t or "autorizacion" in t:
        action = "Autorización"
    elif "pedido" in t or "solicita" in t:
        action = "Pedido"
    main = s if len(s) <= 260 else s[:260].rsplit(" ", 1)[0] + "…"
    if action and not normalize_text(main).startswith(normalize_text(action)):  # evita "Decreto: Decreto N°…"
        return f"{action}: {main}".strip()
    return main.strip()


ORG_HINTS = [
    " S.A.", " SA", " S.R.L.", " SRL", " SAS", " S.A.S.", " COOP", " COOPERATIVA", " ASOCIACIÓN", " FUNDACIÓN",
    " CLUB", " CÁMARA", " CAMARA", " MUNICIPALIDAD", " PROVINCIA", " MINISTERIO", " ENTE", " DIRECCIÓN",
    " DIRECCION", " HOSPITAL", " ESCUELA", " UNIVERSIDAD",
]
TITLE_PREFIXES = r"(?:Sr\.|Sra\.|Señor|Señora|Dr\.|Dra\.|Ing\.|Arq\.|Agrim\.|Lic\.|Prof\.|Abog\.|Cdor\.|Téc\.|Tec\.)"
PERSON_STOP_PHRASES = {
    "Acuerdo Parlamentario", "Ad Referéndum", "Ad Referendum", "Asuntos Entrados", "Asunto Entrado",
    "Miembro Informante", "Miembros Informantes", "Periodo Ordinario", "Período Ordinario",
    "Sala Investigadora", "Solicitud Banca", "Solicitud de Banca", "Banca Automática",
    "Comisiones", "Comisión", "Convenio Marco", "Interés Municipal", "Orden del Día", "ORDEN DEL DÍA",
}
NON_PERSON_CONTAINS = {
    "comision", "comisiones", "asuntos", "entrados", "referendum", "referéndum",
    "acuerdo", "parlamentario", "orden del dia", "orden del día", "decretos",
    "tema", "proyecto", "expediente", "dictamen", "homenajes", "de hacienda",
    "de gobierno", "de turismo", "de transito", "de tránsito", "de obras", "de planeamiento",
    "banca", "periodo", "período",
}
NON_PERSON_LEADING_WORDS = {
    "Lago", "Laguna", "Cerro", "Barrio", "Ruta", "Avenida", "Avda", "Av.", "Plaza",
    "Parque", "Costanera", "Centro", "Municipalidad", "Concejo", "Provincia",
    "Ministerio", "Dirección", "Direccion", "Secretaría", "Secretaria",
    "Junta", "Juzgado", "Hospital", "Escuela", "Universidad",
    "Departamento", "Comision", "Comisión", "Comisiones", "Sala",
    "Concejal", "Concejala",
}
TRAILING_NOISE = {"Dictamen", "Tema", "Proyecto", "Expediente", "Decreto", "Ordenanza", "Nota", "Solicitud", "Banca", "Informe", "Tratamiento"}


def _clean_name_candidate(s):
    s = (s or "").strip()
    s = re.sub(r"^[\-\•\●\·\*]+\s*", "", s).strip()
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip(" ,;:.")


def _strip_trailing_noise(name):
    toks = (name or "").split()
    while toks and toks[-1] in TRAILING_NOISE:
        toks = toks[:-1]
    return " ".join(toks)


def _looks_like_person(name):
    name = _clean_name_candidate(name)
    name = _strip_trailing_noise(name)
    if not name: return False
    if is_boilerplate_text(name): return False
    if name in PERSON_STOP_PHRASES: return False
    low = normalize_text(name)
    if any(tok in low for tok in NON_PERSON_CONTAINS): return False
    first = name.split(" ", 1)[0]
    if first in NON_PERSON_LEADING_WORDS: return False
    toks = name.split()
    if len(toks) < 2: return False
    if len(name) < 7 or len(name) > 80: return False
    if re.search(r"\bDE\s+(HACIENDA|GOBIERNO|TURISMO|TRANSITO|TRÁNSITO|OBRAS|PLANEAMIENTO)\b", name.upper()): return False
    if name.isupper() and len(name) > 18: return False
    return True


def extract_entities(text):
    t = text or ""
    persons = set()
    for m in re.finditer(rf"\bIntendente\s+Municipal(?:\s+{TITLE_PREFIXES})?\s+([A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñÑ]+(?:\s+[A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñÑ]+){{1,4}})\b", t):
        cand = _strip_trailing_noise(_clean_name_candidate(m.group(1)))
        if _looks_like_person(cand): persons.add(cand)
    for m in re.finditer(r"\bvecin[oa]\s+([A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñÑ]+(?:\s+[A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñÑ]+){1,4})\b", t):
        cand = _strip_trailing_noise(_clean_name_candidate(m.group(1)))
        if _looks_like_person(cand): persons.add(cand)
    for m in re.finditer(rf"\b{TITLE_PREFIXES}\s+([A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñÑ]+(?:\s+[A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñÑ]+){{1,5}})\b", t):
        cand = _strip_trailing_noise(_clean_name_candidate(m.group(1)))
        if _looks_like_person(cand): persons.add(cand)
    for m in re.finditer(r"\b([A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñÑ]+)\s*,\s*([A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñÑ]+(?:\s+[A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñÑ]+){0,2})\b", t):
        cand = _strip_trailing_noise(_clean_name_candidate(f"{m.group(2)} {m.group(1)}"))
        if _looks_like_person(cand): persons.add(cand)
    orgs = set()
    for ln in (t.splitlines() or []):
        ln_strip = (ln or "").strip()
        if not ln_strip or is_boilerplate_text(ln_strip): continue
        up = ln_strip.upper()
        if any(h.strip().upper() in up for h in ORG_HINTS):
            frag = normalize_space(ln_strip)
            if 5 < len(frag) < 140: orgs.add(frag)
    return {"persons": sorted(persons), "orgs": sorted(orgs)}


def parse_metadata(text, filename, box):
    t = text or ""
    head = "\n".join(t.splitlines()[:200])
    session_type = box.get("session_type")
    session_number = box.get("session_number")
    session_year = box.get("session_year")
    date_str = box.get("date")
    f_num, f_year = infer_num_year_from_filename(filename or "")
    if session_number is None and f_num is not None: session_number = str(f_num)
    if session_year is None and f_year is not None: session_year = f_year
    if session_type is None:
        session_type = find_first([r"(Orden\s+del\s+D[ií]a)", r"(Acta)", r"(Sesión\s+Ordinaria)", r"(Sesión\s+Extraordinaria)"], head)
    if date_str is None:
        date_str = find_first([
            r"San\s+Mart[ií]n\s+de\s+los\s+Andes\s*,?\s*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})",
            r"San\s+Mart[ií]n\s+de\s+los\s+Andes\s*,?\s*(\d{1,2}\s+de\s+[A-Za-zÁÉÍÓÚáéíóúñÑ]+\s+de\s+20\d{2})",
            r"(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})",
            r"(\d{1,2}\s+de\s+[A-Za-zÁÉÍÓÚáéíóúñÑ]+\s+de\s+20\d{2})",
        ], head, flags=re.IGNORECASE | re.DOTALL)
        dt = parse_date_any(date_str)
        if dt != datetime.min: date_str = dt.strftime("%d/%m/%Y")
    if session_year is None:
        dt = parse_date_any(date_str)
        if dt != datetime.min: session_year = dt.year
    sn_int = normalize_session_number(session_number)
    session_number = str(sn_int) if sn_int is not None else None
    time_str = find_first([r"(?:Hora|HORA)\s*(?:de\s*inicio\s*)?:?\s*(\d{1,2}:\d{2})", r"(?:Siendo|SIENDO)\s+las\s+(\d{1,2}:\d{2})"], head, flags=re.IGNORECASE)
    decrees = sorted(set(re.findall(r"(?:Decreto|DECRETO)\s*(?:N[°º]\s*)?([A-Z0-9\-\./]+)", t, re.IGNORECASE)))
    exped = sorted(set(re.findall(r"(?:Expediente|EXPEDIENTE)\s*(?:N[°º]\s*)?([A-Z0-9\-\./]+)", t, re.IGNORECASE)))
    council = sorted(set(re.findall(r"(?:Concejal(?:a)?|CONCEJAL(?:A)?)(?:\s+informante)?\s*[:\-]?\s+([A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñÑ\.\- ]{2,60})", t, re.IGNORECASE)))
    return {
        "session_type": session_type, "session_number": session_number,
        "session_year": session_year, "date": date_str, "time": time_str,
        "decretos": decrees[:300], "expedientes": exped[:300], "council_members": council[:300],
        "filename_inferred_number": f_num, "filename_inferred_year": f_year,
    }


def extract_items(text):
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    items = []

    exp_tema_pat = re.compile(r"^(\d{1,6}(?:[-]\d{1,6})?/\d{4})\s*[—–-]\s*Tema:\s*(.*)$", re.IGNORECASE)
    exp_pat = re.compile(r"^(\d{1,6}(?:[-]\d{1,6})?/\d{4})\s*[—–-]\s*(.*)$", re.IGNORECASE)
    num_pat = re.compile(r"^(\d{1,2})[\.\)]\s+(.*)$")

    # Texto superpuesto en el PDF: "2175/2025 --- Tema: Dictamen s/ Regularización" seguido de "2175/2025 --- Tema: Dictamen s/".
    # La segunda es un prefijo de la primera; si queda, parte el ítem en dos.
    lines = [ln for k, ln in enumerate(lines) if not (k and exp_pat.match(ln) and lines[k - 1].startswith(ln))]

    def is_new_item(line):
        if MI_PAT.match(line): return None
        for typ, p in [("exp_tema", exp_tema_pat), ("exp", exp_pat), ("num", num_pat)]:
            m = p.match(line)
            if m: return (typ, m)
        return None

    def ends_item(line):
        return bool(HEADER_PAT.match(line) or SECTION_PAT.match(line) or is_new_item(line))

    i = 0
    while i < len(lines):
        # Patrón SMA con Miembro Informante
        mi_match = MI_PAT.match(lines[i])
        if mi_match:
            exp_ref = mi_match.group(2).strip()
            tema_text = ""
            j = i + 1
            while j < len(lines) and j < i + 5:
                tm = TEMA_LINE_PAT.match(lines[j])
                if tm:
                    tema_text = tm.group(1).strip()
                    j += 1
                    while j < len(lines):
                        if ends_item(lines[j]): break
                        if not MIEMBRO_PAT.match(lines[j]):  # entre proyectos de un mismo ítem
                            tema_text += " " + lines[j]
                        j += 1
                    break
                j += 1
            if tema_text:
                full = normalize_space(tema_text)
                full = remove_boilerplate_global(full)
                if full:
                    qd = build_que_dice(full)
                    if qd:
                        impact = infer_impact(full)
                        items.append({"key": exp_ref, "title": full[:2000], "que_dice": qd,
                                      "impact_text": impact["impact_text"], "impact_tags": impact["impact_tags"],
                                      "decreto": None, "expediente": exp_ref, "topics": classify_topics(full)})
            i = j
            continue

        hit = is_new_item(lines[i])
        if not hit:
            i += 1
            continue

        typ, m = hit
        key = (m.group(1) or "").strip()
        first = (m.group(2) or "").strip()

        if is_basura(first):
            i += 1
            continue

        parts = [first] if first else []
        j = i + 1
        while j < len(lines):
            if ends_item(lines[j]): break
            if not MIEMBRO_PAT.match(lines[j]):  # entre proyectos de un mismo ítem
                parts.append(lines[j])
            j += 1

        full = normalize_space(" ".join(parts))
        full = remove_boilerplate_global(full)
        if not full or is_basura(full):
            i = j
            continue

        qd = build_que_dice(full)
        if not qd:
            i = j
            continue

        dec_inside = find_first([r"(?:Decreto)\s*(?:N[°º]\s*)?([A-Z0-9\-\./]+)"], full)
        exp_inside = find_first([r"(?:Expediente)\s*(?:N[°º]\s*)?([A-Z0-9\-\./]+)"], full)
        impact = infer_impact(full)

        items.append({"key": key if key else None, "title": full[:2000], "que_dice": qd,
                       "impact_text": impact["impact_text"], "impact_tags": impact["impact_tags"],
                       "decreto": dec_inside, "expediente": exp_inside or (key if typ in ("exp", "exp_tema") else None),
                       "topics": classify_topics(full)})
        i = j

    for idx, it in enumerate(items, start=1):
        if not it.get("key"): it["key"] = str(idx)
    return items[:800]


def ensure_items_schema(items):
    if not items or not isinstance(items, list): return []
    if items and isinstance(items[0], str):
        fixed = []
        for idx, s in enumerate(items, start=1):
            qd = build_que_dice(s)
            if not qd: continue
            imp = infer_impact(s)
            fixed.append({"key": str(idx), "title": s, "que_dice": qd,
                          "impact_text": imp["impact_text"], "impact_tags": imp["impact_tags"],
                          "decreto": None, "expediente": None, "topics": classify_topics(s)})
        return fixed
    fixed_items = []
    for idx, it in enumerate(items, start=1):
        if not isinstance(it, dict): continue
        key = it.get("key") or it.get("id") or it.get("nro") or str(idx)
        title = it.get("title") or it.get("texto") or it.get("descripcion") or ""
        qd = it.get("que_dice") or it.get("qué_dice") or build_que_dice(title)
        if not qd: continue
        base = {**it, "key": str(key), "title": title, "que_dice": qd}
        if "impact_text" not in base:
            imp = infer_impact(title)
            base["impact_text"] = imp["impact_text"]
            base["impact_tags"] = imp["impact_tags"]
        fixed_items.append(base)
    return fixed_items


def build_summary_citizen(meta, topics, items):
    parts = []
    head = []
    dt = parse_date_any(meta.get("date"))
    date_disp = fmt_date_short(dt, fallback=(meta.get("date") or "s/f"))
    head.append(f"**Fecha:** {date_disp}")
    if meta.get("time"): head.append(f"**Hora:** {meta['time']}")
    stype = meta.get("session_type") or "ORDEN DEL DIA"
    sn = meta.get("session_number") or "s/n"
    sy = meta.get("session_year") or "s/año"
    head.append(f"**Documento:** {stype} N° {sn} (Año {sy})")
    parts.append(" · ".join(head))
    if topics: parts.append(f"**Temas:** {', '.join(topics)}")
    parts.append("")
    if items:
        parts.append(f"**Ítems detectados:** {len(items)}")
        parts.append("")
        parts.append("**Puntos (claro y directo):**")
        for it in items[:10]:
            qd = (it.get("que_dice") or "").strip()
            imp = (it.get("impact_text") or "").strip()
            if qd:
                parts.append(f"- **{it.get('key')}**: {qd}")
                if imp: parts.append(f"  - _Impacto_: {imp}")
    else:
        parts.append("No se detectaron ítems claros (PDF escaneado o texto muy roto).")
    return "\n".join(parts)


def process_pdf(pdf_bytes, filename):
    box = extract_box_header_info(pdf_bytes)
    raw_text = extract_text_from_pdf_bytes(pdf_bytes)
    raw_text = remove_boilerplate_lines(raw_text)
    raw_text = remove_boilerplate_global(raw_text)
    text = normalize_space(raw_text)
    meta = parse_metadata(text, filename=filename, box=box)
    topics = classify_topics(text)
    items = extract_items(text)
    entities = extract_entities(text)
    return {
        "processor_version": PROCESSOR_VERSION,
        "source": {"filename": filename, "processed_at": datetime.utcnow().isoformat() + "Z"},
        "box_meta": box, **meta, "topics": topics, "items": items, "entities": entities,
        "summary_citizen": build_summary_citizen(meta, topics, items),
        "text_preview": text[:12000],
    }
