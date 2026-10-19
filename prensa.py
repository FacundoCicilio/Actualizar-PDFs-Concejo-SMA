# Notas de la sección Prensa del Concejo (WordPress, https://prensacd.cdsma.gob.ar/prensa/), agrupadas por sesión.
# Las lee instagram_publisher.py para los posts "Qué se aprobó" y "Destacado". Se guardan en el dataset privado de HF.
import calendar
import html
import re
from datetime import date, datetime

import requests

import instagram_carousel as ic
from actas_parser import MONTHS, infer_impact, normalize_text
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
# Conflictos políticos del Concejo con el intendente: la cuenta no los publica, por neutralidad.
POLITICAL_TITLE_PAT = re.compile(r"juicio\s+pol[ií]tico|contra\s+el\s+intendente|destituci[oó]n", re.I)
# Temas de fondo que van como destacado aunque no tengan etiqueta de Bolsillo/Servicios/Obras: (patrón sobre el título y
# el comienzo sin tildes, peso para ordenar). Bolsillo pesa 5, Servicios y Obras 4 (ic.IMPACT_WEIGHT y OBRA_WEIGHT).
THEMES = {
    "Presupuesto": (re.compile(r"presupuesto|endeudamiento|prestamo|financiamiento"), 5),
    "Tierras": (re.compile(r"\bcesion|comodato|donacion|\bventa de (?:un |una |los |las )?(?:lote|tierra|inmueble|parcela)"
                           r"|adjudicacion|desafectacion|permuta|regularizacion dominial"), 4),
    "Concesiones": (re.compile(r"concesion|licitacion|pliego"), 4),
    "Normas urbanas": (re.compile(r"codigo de edificacion|codigo urbano|zonificacion|ordenamiento territorial|factibilidad"), 3),
    "Tránsito": (re.compile(r"transporte|colectivo|\btaxi|remis|estacionamiento|reductor|lomos? de burro"), 3),
}
# "Contratación" sola no alcanza para Obras (la adhesión a una ley de capacitación habla de contratar profesionales).
OBRA_PAT = re.compile(r"\bobras?\b|licitacion|pavimento|asfalto|cordon cuneta|red de agua|cloaca")
OBRA_WEIGHT = 4
# En las notas de tierras, lote/chacra/manzana/donación describen el bien, no un trámite de un particular.
TIERRAS_INDIVIDUAL_OK = re.compile(r"lotes?\s+\d|manzana\s+\w|chacra\s+\d|a favor de|donaci[oó]n|regularizaci[oó]n dominial", re.I)
_MES = "|".join(MONTHS)
# Fin de vigencia o de plazo: "hasta el 30 de septiembre de 2026", "del 1 al 30 de septiembre", "vence el 30 de …",
# y solo con mes: "hasta diciembre de 2026", "de agosto a diciembre de 2026" (fin de ese mes).
END_DAY_PAT = re.compile(rf"\b(?:hasta\s+el|vence\s+el|del?\s+\d{{1,2}}\s+al)\s+(\d{{1,2}})\s+de\s+({_MES})(?:\s+de\s+(\d{{4}}))?", re.I)
END_MONTH_PAT = re.compile(rf"\b(?:hasta|de\s+(?:{_MES})\s+a)\s+({_MES})\s+de\s+(\d{{4}})", re.I)


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
    (sin sesión, "tratará", "rechazó", Concejo Estudiantil, audiencias públicas, conflictos políticos)."""
    if EXCLUDED_TITLE_PAT.search(note["title"]) or POLITICAL_TITLE_PAT.search(note["title"]):
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


def lead(note, chars=400):
    return normalize_text(f"{note['title']}. {note['text'][:chars]}")


def themes(note):
    """Temas de fondo de la nota, el de mayor peso primero."""
    t = lead(note)
    return sorted((name for name, (pat, _) in THEMES.items() if pat.search(t)), key=lambda k: -THEMES[k][1])


def destacado_tags(note):
    """Etiquetas que cuentan para el destacado: Obras solo si la nota habla de obras, no de una contratación cualquiera."""
    tags = set(note["impact_tags"]) & DESTACADO_TAGS
    if "🏗 Infraestructura" in tags and not OBRA_PAT.search(lead(note)):
        tags.discard("🏗 Infraestructura")
    return tags


def weight(note):
    tags = destacado_tags(note)
    w = [ic.IMPACT_WEIGHT.get(t, 0) for t in note["impact_tags"]] + [THEMES[t][1] for t in themes(note)]
    return max(w + ([OBRA_WEIGHT] if "🏗 Infraestructura" in tags else []), default=0)


def general(note):
    """Alcance general: el de ic.is_relevant o un tema de fondo (en tierras, sin descartar por lote/chacra/donación)."""
    if not themes(note):
        return ic.is_relevant(note)
    title = TIERRAS_INDIVIDUAL_OK.sub(" ", note["title"]) if "Tierras" in themes(note) else note["title"]
    return not ic.INDIVIDUAL_PAT.search(title)


def rank(notes):
    """Primero las de alcance general, después el resto; dentro de cada grupo, mayor peso (tema o impacto) primero."""
    return sorted(notes, key=lambda n: (general(n), weight(n), len(n["impact_tags"])), reverse=True)


def is_destacado(note):
    return general(note) and bool(destacado_tags(note) or themes(note))


def group_key(note):
    """Tema para agrupar en la puesta al día cuando sobran notas: el tema de fondo o, si no, la etiqueta principal."""
    if themes(note):
        return themes(note)[0]
    tags = sorted(destacado_tags(note), key=lambda t: -ic.IMPACT_WEIGHT.get(t, 0))
    return ic.strip_emoji(tags[0]) if tags else "Otros"


# ------------------------------------------------------------------ audiencias públicas (no son aprobaciones)
AUDIENCIA_NUM_PAT = re.compile(r"Audiencia\s+P[uú]blica\s+N\.?\s*[°º]?\s*(\d{1,3})", re.I)
AUDIENCIA_DATE_PAT = re.compile(rf"(?:realizar[aá]|realiz[oó]|celebr[oó]|llev[oó] a cabo)[^.]{{0,70}}?\b(\d{{1,2}}) de ({_MES})"
                                rf"(?: de (\d{{4}}))?", re.I)
DONE_PAT = re.compile(r"\b(?:se realiz[oó]|realiz[oó]|celebr[oó]|llev[oó] a cabo)\b[^.]{0,40}Audiencia", re.I)
POSTPONED_PAT = re.compile(r"\bposterg", re.I)
AUDIENCIA_THEME_PAT = re.compile(r"comodato|donacion|cesion|concesion|licitacion|desafectacion|venta de|permuta")


def audiencia_subject(note):
    """Tema de la audiencia según el título ("…por la donación de un lote al Obispado…"): respaldo si Haiku no sirve."""
    m = re.search(r"\b(?:por|sobre)\s+(?:el\s+|la\s+|un\s+|una\s+)?(.+)$", note["title"], re.I)
    s = (m.group(1) if m else note["title"]).strip(" .")
    return s[:1].upper() + s[1:]


def audiencias(notes, start, today):
    """Audiencias públicas sobre tierras y concesiones que ya se hicieron entre start y today, una por número
    (convocatoria y realización juntas). Las postergadas sin nota de realización no van.
    Devuelve [{numero, fecha, notes}] ordenadas por fecha; notes: la convocatoria primero (describe el tema)."""
    by_num = {}
    for n in sorted(notes.values(), key=lambda n: n["date"]):
        m = AUDIENCIA_NUM_PAT.search(f"{n['title']} {n['text']}")
        if m and re.search(r"Audiencia\s+P[uú]blica", n["title"], re.I):
            by_num.setdefault((int(n["date"][:4]), int(m.group(1))), []).append(n)
    out = []
    for (year, num), ns in by_num.items():
        done = any(DONE_PAT.search(f"{n['title']}. {n['text'][:300]}") for n in ns)
        if any(POSTPONED_PAT.search(n["title"]) for n in ns) and not done:
            continue
        if not any(AUDIENCIA_THEME_PAT.search(lead(n, 700)) for n in ns):
            continue
        fechas = []
        for n in ns:
            d = AUDIENCIA_DATE_PAT.search(n["text"])
            if d:
                try:
                    fechas.append(date(int(d.group(3) or year), MONTHS[d.group(2).lower()], int(d.group(1))))
                except ValueError:
                    pass
        # sin fecha en el texto ("este lunes"): a más tardar, el día de la nota de realización
        fecha = max(fechas) if fechas else max(datetime.fromisoformat(n["date"]).date() for n in ns)
        if start <= fecha <= today:  # ya ocurrió: la fecha programada pasó (y no se postergó) o hay nota de realización
            out.append({"numero": num, "fecha": fecha, "notes": ns})
    return sorted(out, key=lambda a: a["fecha"])


def vigencia_hasta(note):
    """Última fecha de fin de vigencia o de plazo que informa la nota, o None si no informa ninguna. Se toma la más
    tardía: si alguna medida sigue vigente, la nota todavía sirve como destacado."""
    text, year = note["text"], int(note["date"][:4])
    ends = []
    for m in END_DAY_PAT.finditer(text):
        try:
            ends.append(date(int(m.group(3) or year), MONTHS[m.group(2).lower()], int(m.group(1))))
        except ValueError:  # "31 de septiembre"
            pass
    for m in END_MONTH_PAT.finditer(text):
        y, mo = int(m.group(2)), MONTHS[m.group(1).lower()]
        ends.append(date(y, mo, calendar.monthrange(y, mo)[1]))
    return max(ends, default=None)


def vencida(note, today):
    """El plazo que informa la nota ya terminó: no se publica como destacado (sigue en "Qué se aprobó")."""
    end = vigencia_hasta(note)
    return end is not None and end < today


def note_text(note):
    """Lo que se manda a Claude: anonimizado, como en el orden del día."""
    return ic.anonymize(f"{note['title']}\n\n{note['text']}")


def session_doc(s):
    """La sesión con la forma que usan las placas (session_number, session_kind, date dd/mm/aaaa)."""
    return {"session_number": s["number"], "session_kind": s["kind"], "session_year": s["year"],
            "date": s["date"].strftime("%d/%m/%Y")}
