# Borrador semanal del newsletter (Substack) armado desde las fuentes oficiales de los 7 días que terminan el día de la
# corrida, no desde lo que publicó Instagram: órdenes del día (PDF), notas de Prensa del Concejo y Boletín Oficial.
# Una sesión que ya se realizó va en "Qué se aprobó" si Prensa publicó lo aprobado; si no, se dice que todavía no lo
# publicó, con el link al orden del día. Si publicó solo una parte, cuántos puntos quedan sin resultado y los de alcance
# general, con su expediente. Solo las sesiones por venir van en "Qué va a tratar". Claude Haiku lo redacta
# leyendo también los newsletters anteriores (Issues "newsletter-borrador" y newsletters/*.md), para contar qué hay
# de nuevo en un tema
# que ya salió. Los Anexos del Digesto entran la semana en que se capturan. Queda en un Issue en markdown sin tablas. Si falla una fuente
# auxiliar (newsletters anteriores, Digesto) el borrador sale igual con un aviso en el Issue; termina con código 1 solo si
# no se puede generar el borrador.
#   python newsletter.py [--hasta 2026-11-20] [--sin-issue]
import argparse
import json
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import unquote

import anthropic
import requests

import boletin
import digesto
import instagram_carousel as ic
import instagram_publisher as ip
import prensa
from hf_dataset_store import DATASET_REPO_ID, load_index, load_json, load_processed_json

ARG = timezone(timedelta(hours=-3))
LABEL = ("newsletter-borrador", "0E8A16")
MAX_TEXT = 1500  # por nota u ordenanza: con un Boletín de 18 ordenanzas, más largo corta la respuesta (max_tokens)
MAX_ANEXO = 4000  # el Anexo es el dato nuevo (cronograma del agua): no se corta a mitad
PREVIOS = 4  # newsletters anteriores que lee Haiku para no repetir noticias
# Reglas editoriales: no van las declaraciones de interés ni los aniversarios.
EXCLUIR_PAT = re.compile(r"declar\w*\s+de\s+inter[eé]s|de\s+inter[eé]s\s+(?:municipal|cultural|social|educativ|deportiv|"
                         r"tur[ií]stic|legislativ|comunitari)|aniversario|\bpor\s+sus\s+\d+\s+años|\bcumpl\w*\s+\d+\s+años"
                         r"|beneplácito", re.I)
# "autorizó la venta de alcohol y lo declaró de interés": hay una decisión además de la declaración, entonces va.
DECISION_PAT = re.compile(r"autoriz|aprob|modific|otorg|establec|ratific|exim|derog|adjudic|convoc|cre[óa]|design", re.I)
# Verbos de una nota de Prensa que confirman que el Concejo decidió: los de Instagram más designaciones y derogaciones.
DECIDIO_PAT = re.compile(prensa.APPROVED_PAT.pattern + r"|\b(?:design[oó]|eligi[oó]|derog[oó]|modific[oó]|cre[oó])\b", re.I)
NEWSLETTERS_DIR = "newsletters"  # los números que no pasaron por un Issue (el #1); el feed de Substack da 403 en Actions
# Una viñeta del borrador: "- **Tema**: texto". El tema va en negrita o hasta los dos puntos.
ITEM_PAT = re.compile(r"^\s*-\s+(?:\*\*(?P<b>[^*]+)\*\*|(?P<c>[^:\n]+)):?(?P<t>[^\n]*)", re.M)
TABLE_PAT = re.compile(r"^\s*\|.*\|\s*$", re.M)
URL_PAT = re.compile(r"https?://[^\s)\]>\"']+")
PENDIENTE = "Se realizó el {fecha}; el Concejo todavía no publicó lo aprobado."
PARCIAL = "De los {total} puntos del orden del día de la {ses}, el Concejo publicó el resultado de {pub}."
SIN_RESULTADO = "punto del orden del día de una sesión realizada, sin resultado publicado"
# Puntos reales del orden del día: con expediente. Los "1", "2"... son restos del PDF (fechas, miembro informante).
PUNTO_PAT = re.compile(r"^(?:\d+[-/]\d+|E[EX]-)")
NUM_TOKEN_PAT = re.compile(r"\d+(?:[.,]\d+)*")
RELLENO_PAT = re.compile(r"\b(?:ver|v[ée]ase|como\s+(?:se\s+dijo|en\s+el))\b[^.\n]{0,30}\b(?:arriba|abajo|anterior|"
                         r"bloque|secci[oó]n|[íi]tem|punto|apartado)\b|\b[íi]dem\b", re.I)
MAX_BOLETIN, MAX_PALABRAS = 4, 60  # la sección del Boletín: agrupada por tema, viñetas cortas (salvo la del Anexo)
# Comentarios sobre el propio newsletter: no cuentan nada de los documentos.
AUTOCOMENTARIO_PAT = re.compile(r"\best[ea]\s+(?:resumen|newsletter|n[uú]mero|edici[oó]n|entrega)\s+(?:incluye|repasa|cubre|"
                                r"suma|trae)|\bcomo\s+(?:ya\s+)?(?:dijimos|contamos|adelantamos|informamos|mencionamos)|"
                                r"\bestaba\s+pendiente|\bya\s+(?:contamos|dijimos)|\btambi[eé]n\s+incluimos", re.I)
PORCENTAJE_PAT = re.compile(r"(\d+(?:,\d+)?)\s*%")
# Personas que nombra un documento: con título ("Sr. Oscar Alfredo CAAMAÑO") o en una lista de designados ("Los
# titulares son Martín Jorge Rodríguez, Andrés Freisztav y …"). Se controlan por el apellido (la última palabra).
_TOK = r"[A-ZÁÉÍÓÚÑ](?:[a-záéíóúñ]+|[A-ZÁÉÍÓÚÑ]+|\.)"
_NOM = _TOK + r"(?:\s+(?:de\s+|del\s+)?" + _TOK + r"){1,5}"
NOMBRES_PAT = re.compile(rf"(?:\b(?:Sra?|Srta|Sres|Sras)\.\s+|\b(?:titulares|suplentes|integrantes|designad[oa]s)\s+"
                         rf"(?:son|es|a|:)\s+)(?P<n>{_NOM}(?:\s*(?:,|\by\b)\s+{_NOM})*)")
BOLETIN_SEC_PAT = re.compile(r"^##[^\n]*Bolet[íi]n[^\n]*\n(?P<s>.*?)(?=^#|\Z)", re.M | re.S | re.I)
# Palabras que no identifican un tema: el título tiene que compartir una de las demás con lo aprobado en la semana.
GENERICAS = {"concejo", "deliberante", "municipal", "sesión", "ordinaria", "aprobó", "designó", "autorizó", "durante",
             "andes", "martín", "integrantes", "nuevo", "nueva"}
SYSTEM = """Redactás el newsletter semanal del Observatorio Actas SMA sobre el Concejo Deliberante de San Martín de los Andes.
Reglas, sin excepción:
- Usás SOLO la información de los documentos oficiales que te paso. No agregues datos, cifras, fechas, nombres ni contexto que no estén ahí. Si un documento no dice algo (desde cuándo, a quién afecta), no lo completes.
- Imparcial: sin opiniones, sin adjetivos valorativos, sin sección de opinión, sin especular sobre motivos ni consecuencias.
- Incluí TODOS los documentos que te paso, cada uno con su link [texto](url) copiado exacto. Si un destacado y un ítem de "Qué se aprobó" son la misma norma, contalo una vez con ambos links.
- "aprobado" es lo que el Concejo ya decidió. "orden del día (a tratar)" es de una sesión que todavía no se hizo: en futuro, nunca lo des por aprobado.
- "sesión realizada, sin lo aprobado publicado": la sesión ya se hizo. No digas qué va a tratar ni qué aprobó. Escribí exactamente la oración del campo "texto" con el link al orden del día.
- "sesión realizada, resultado publicado en parte": debajo de "Qué se aprobó" de esa sesión escribí exactamente la oración del campo "texto", en negrita, con el link al orden del día. Después, los "punto del orden del día de una sesión realizada, sin resultado publicado": cada uno en una viñeta con su expediente ("expte. 1926/2026"), contando qué se proponía, sin decir que se aprobó ni que se va a tratar.
- El título es lo más relevante que el Concejo decidió en la sesión de esta semana (lo "aprobado" de esa sesión). Nunca las ordenanzas del Boletín Oficial ni un orden del día, si hubo una sesión con lo aprobado publicado.
- Ordenanzas del Boletín: agrupadas por tema en 3 o 4 viñetas como máximo (por ejemplo tarifas y servicios, obras y convenios, excepciones y condonaciones, subsidios y otras), cada una de 60 palabras como máximo con el link al Boletín. Nombrá por su número solo las más relevantes. Las excepciones y condonaciones van con el nombre de cada beneficiario tal como está en el documento y el número de la ordenanza, sin detalles (por ejemplo: "condonación de tasas a Juan Ejemplo (15.000/26); excepción de taxi a Ana Muestra (15.001/26)"). El Anexo del Digesto va en la viñeta de su ordenanza, con los porcentajes con "%" y el total como en el documento (esa viñeta no tiene tope de palabras). La cantidad de ordenanzas te la doy yo: no las cuentes vos.
- Si un documento trae el campo "con_nombre" (designados en un cargo, beneficiarios de una excepción o una condonación), escribí todos esos nombres tal cual, junto a su ordenanza o su tema. Es obligatorio aunque la viñeta sea corta.
- Nada de comentarios sobre el propio newsletter, sobre números anteriores ni sobre cuándo se publicó algo ("estaba pendiente", "como dijimos", "ya contamos", "también incluimos"): contá solo lo que dicen los documentos.
- Ningún número (cantidades, montos, porcentajes, fechas) que no esté escrito en los documentos o en este mensaje: no sumes, no cuentes, no redondees.
- Anexo del Digesto: el ítem cuenta qué dice el cronograma del Anexo I (porcentajes, tramos y fechas tal como figuran) y aclara que no salió en el Boletín Oficial.
- Cada ítem cuenta algo propio: nada de ítems de relleno que remiten a otro ("ver el ítem de arriba", "ídem").
- Privacidad: {privacidad}
- Newsletters anteriores: si un tema ya salió en uno anterior, el título y el ítem dicen qué hay de nuevo (por ejemplo "se aprobó lo que estaba en el orden del día", "se publicó en el Boletín Oficial", "se realizó la audiencia"), no repiten la noticia. El título del newsletter no puede ser un tema que ya fue título antes, salvo para contar la novedad.
- Breve: cada ítem en una o dos oraciones (qué decide, a quién afecta, desde cuándo). Las ordenanzas del Boletín de un mismo tema van juntas en un ítem.
- Markdown simple: títulos con #/##, párrafos, viñetas con "- ", **negrita** y links. PROHIBIDO usar tablas.
- Español rioplatense claro, para vecinos sin formación legal.

Estructura (la de los números anteriores):
# <Título factual: lo más relevante que aprobó el Concejo en la sesión de la semana, sin adjetivos>
*<Bajada de una línea>*

<Párrafo de apertura: qué semana cubre y qué sesiones hubo, según los documentos.>

## Qué se aprobó en la Sesión N° X (fecha)
- **<Tema corto>**: qué decide, a quién afecta y desde cuándo si el documento lo dice. [Fuente](url)
**De los N puntos del orden del día de la Sesión N° X, el Concejo publicó el resultado de M.** [Orden del día](url)   (si hay resultado publicado en parte)
Entre ellos:
- **<Tema corto>** (expte. N): qué se proponía.

## Sesión N° X (fecha)   (sesión realizada, sin lo aprobado publicado)
Se realizó el <fecha>; el Concejo todavía no publicó lo aprobado. [Orden del día](url)

## Ordenanzas publicadas en el Boletín Oficial   (si hay)
- **<Tema del grupo>**: qué deciden, en 60 palabras como máximo. [Boletín Oficial N° X](url)   (3 o 4 viñetas, no más)

## Qué va a tratar el Concejo: Sesión N° Y (fecha)   (solo sesiones que todavía no se hicieron)
- igual formato, en futuro o "se propone"

## Fuentes (links)
- <documento>: url

Si te sirvió este trabajo, pasale el link a algún vecino. Y si querés recibir el próximo directamente en tu mail, suscribite acá abajo. Es gratis.

— Facundo Cicilio · Observatorio Actas SMA

Devolvé solo el markdown del newsletter, sin comentarios antes ni después.""".replace("{privacidad}", ic.PRIVACIDAD)


def excluded(title, text=""):
    return bool(EXCLUIR_PAT.search(f"{title}. {text}")) and not DECISION_PAT.search(title)


def nombres(text):
    """Nombres completos de las personas que nombra un documento, sin repetir. Un nombre que es el comienzo de otro
    ("Sr. Leandro Alberto" en el título cortado de una ordenanza, "Leandro Alberto GARCIA" en el texto) no cuenta."""
    todos = [" ".join(n.split()) for p in (NOMBRES_PAT, ic.PERSONA_PAT) for m in p.finditer(text or "")
             for n in re.split(r",|\by\b", m.group("n")) if n.strip()]
    return list(dict.fromkeys(n for n in todos if not any(o != n and o.startswith(n) for o in todos)))


def privado(text):
    """El criterio de Instagram (ic.con_nombre): con nombre quien tiene un cargo, recibe plata pública o un trato
    distinto al general; sin nombre quien hizo un trámite común; nunca documentos, números sueltos ni domicilio."""
    return boletin.depersonalize(text or "")


def ddmmyyyy(s):
    try:
        return datetime.strptime(s or "", "%d/%m/%Y").date()
    except ValueError:
        return None


def ses_label(doc):
    return f"{ic.session_title(doc)} ({ic.date_long(doc['date'])})"


def corte(desde):
    """Desde cuándo entra lo del Boletín y del Digesto: el viernes anterior a las 9, cuando corrió el newsletter
    anterior. Lo que salió ese viernes a la tarde (el Boletín 831, el 13/11 a las 14) entra en esta semana."""
    return datetime.combine(desde - timedelta(days=1), datetime.min.time(), ARG).replace(hour=9)


def en_rango(momento, desde, hasta):
    return momento is not None and corte(desde) < momento < datetime.combine(hasta + timedelta(days=1), datetime.min.time(), ARG)


def boletin_date(url):
    """Cuándo el Boletín Oficial subió el PDF (Last-Modified del sitio), o None si no responde."""
    try:
        lm = requests.head(url, timeout=30, allow_redirects=True).headers.get("Last-Modified")
        return parsedate_to_datetime(lm).astimezone(ARG) if lm else None
    except (requests.RequestException, TypeError, ValueError):
        return None


def sesiones(index, notes, desde, hasta):
    """[(doc de sesión, estado, notas aprobadas publicadas hasta `hasta`)]: estado "aprobado", "pendiente" (se realizó y
    Prensa no publicó lo aprobado) o "a tratar". Entran las sesiones de la semana, las que tienen notas aprobadas
    publicadas en la semana y las que vienen (orden del día ya publicado)."""
    por_sesion = {}  # clave -> {doc, url del orden del día, notas}
    for r in index.values():
        d = ddmmyyyy(r.get("date"))
        if d and r.get("session_number") and desde <= d <= hasta + timedelta(days=14):
            k = prensa.session_key(r["session_number"], d.year, r.get("session_kind") or "Ordinaria")
            por_sesion[k] = {"doc": {"session_number": str(r["session_number"]), "session_kind": r.get("session_kind"),
                                     "date": r["date"]}, "fecha": d, "od": r, "notas": []}
    for n in sorted(notes.values(), key=lambda n: n["date"]):
        pub = datetime.fromisoformat(n["date"]).date()
        s = prensa.session_of(n, DECIDIO_PAT) if pub <= hasta else None
        if not s:
            continue
        cur = por_sesion.get(s["key"])
        if cur is None and desde <= pub:  # nota de la semana de una sesión anterior (sin orden del día en el rango)
            fecha = s["date"] or pub
            cur = por_sesion[s["key"]] = {"doc": {"session_number": s["number"], "session_kind": s["kind"],
                                                  "date": fecha.strftime("%d/%m/%Y")}, "fecha": fecha, "od": None, "notas": []}
        if cur is not None:
            cur["notas"].append(n)
    out = []
    for k, c in sorted(por_sesion.items(), key=lambda kc: kc[1]["fecha"]):
        semana = [n for n in c["notas"] if desde <= datetime.fromisoformat(n["date"]).date()]
        if c["notas"]:
            if semana:  # si todo lo aprobado salió en semanas anteriores, ya estuvo en otro newsletter
                out.append((c, "aprobado", semana))
        # shortcut: una sesión del mismo día de la corrida cuenta como por venir; el cron corre el viernes y sesiona el jueves.
        elif c["fecha"] < hasta:
            if c["fecha"] >= desde:
                out.append((c, "pendiente", []))
        else:
            out.append((c, "a tratar", []))
    return out



def sin_resultado(doc, notas, ses, url):
    """Los puntos del orden del día que ninguna nota de Prensa cubre: la oración con cuántos hay y de cuántos se
    publicó el resultado (PARCIAL, cifras del código) y los de alcance general (ic.select_items), cada uno con su
    expediente. [] si Prensa cubrió todo."""
    puntos = [it for it in doc.get("items") or [] if PUNTO_PAT.match(it.get("key") or "")]
    resto = [it for it in puntos
             if boletin.match({"title": it["key"], "text": it.get("que_dice") or ""}, notas)[0] == "nueva"]
    if not resto:
        return []
    out = [{"tipo": "sesión realizada, resultado publicado en parte", "sesion": ses, "titulo": "Orden del día",
            "texto": PARCIAL.format(total=len(puntos), ses=ses.split(" (")[0], pub=len(puntos) - len(resto)), "url": url}]
    for it in ic.select_items(doc):
        text = privado(ic.official_text(it))
        if it in resto and not excluded(text):
            out.append({"tipo": SIN_RESULTADO, "sesion": ses, "expediente": it["key"], "titulo": it["key"], "texto": text,
                        "url": url})
    return out


def auxiliar(nombre, fn, default, avisos):
    """fn() o default si falla: una fuente auxiliar caída no frena el borrador, queda como aviso en el Issue."""
    try:
        return fn()
    except Exception as e:  # cualquier falla de red, de HF o de formato
        avisos.append(f"No se pudo leer {nombre} ({type(e).__name__}: {e}).")
        print(f"Aviso: {avisos[-1]}")
        return default


def collect(desde, hasta, avisos):
    """(sesiones de la semana, material): material = [{tipo, sesion, titulo, texto, url}] desde documentos oficiales."""
    index = {r["doc_id"]: r for r in load_index(DATASET_REPO_ID) if r.get("doc_id")}
    notes = prensa.load_notes(save=False)  # el sitio de Prensa, no solo lo guardado: lo que se publicó hasta hoy
    # con el criterio de privacidad vigente aunque el cron todavía no haya rehecho el store (STORE_VERSION)
    ords = boletin.load(hasta, save=False)["ordenanzas"]
    anexos = auxiliar("los Anexos del Digesto", lambda: load_json(DATASET_REPO_ID, digesto.STORE_PATH), None, avisos) or {}
    material, held, usadas = [], [], set()
    for c, estado, notas in sesiones(index, notes, desde, hasta):
        ses = ses_label(c["doc"])
        if c["fecha"] <= hasta and desde <= c["fecha"]:
            held.append(ses)
        if estado == "aprobado":
            for n in notas:
                usadas.add(str(n["id"]))
                if not excluded(n["title"], n["text"][:300]):
                    material.append({"tipo": "aprobado", "sesion": ses, "titulo": privado(n["title"]),
                                     "texto": privado(prensa.note_text(n))[:MAX_TEXT], "url": n["link"]})
            if c["od"]:  # sesión de la semana: Prensa suele publicar solo parte de lo que se trató
                material += sin_resultado(load_processed_json(DATASET_REPO_ID, c["od"]["doc_id"]) or {}, notas, ses,
                                          c["od"]["source_url"])
        elif estado == "pendiente":
            material.append({"tipo": "sesión realizada, sin lo aprobado publicado", "sesion": ses, "titulo": "Orden del día",
                             "texto": PENDIENTE.format(fecha=ic.date_long(c["doc"]["date"])), "url": c["od"]["source_url"]})
        else:
            doc = load_processed_json(DATASET_REPO_ID, c["od"]["doc_id"]) or {}
            for it in ic.select_items(doc):  # los mismos puntos de alcance general que el carrusel
                text = privado(ic.official_text(it))
                if not excluded(text):
                    material.append({"tipo": "orden del día (a tratar, no aprobado)", "sesion": ses,
                                     "titulo": it.get("key", ""), "texto": text, "url": c["od"]["source_url"]})
    for n in sorted(notes.values(), key=lambda n: n["date"]):  # otras notas de la semana (sin sesión aprobada)
        pub = datetime.fromisoformat(n["date"]).date()
        if (desde <= pub <= hasta and str(n["id"]) not in usadas and prensa.general(n)
                and not prensa.POLITICAL_TITLE_PAT.search(n["title"]) and not excluded(n["title"], n["text"][:300])):
            material.append({"tipo": "nota de Prensa del Concejo", "sesion": "", "titulo": privado(n["title"]),
                             "texto": privado(prensa.note_text(n))[:MAX_TEXT], "url": n["link"]})
    recientes = {o["url"] for o in ords.values() if (o.get("promulgada") or "") >= (desde - timedelta(days=60)).isoformat()}
    de_la_semana = {u for u in recientes if en_rango(boletin_date(u), desde, hasta)}
    for k, o in sorted(ords.items()):
        # Como Instagram: las de un particular entran solo si se nombra (ic.con_nombre), con nombre y sin números.
        if o["url"] not in de_la_semana or o.get("a_revisar") or "text" not in o or (
                o.get("particular") and not ic.con_nombre(o["text"])):
            continue
        n = boletin.as_note(o)
        if excluded(n["title"]):
            continue
        material.append({"tipo": f"ordenanza {o['label']} (Boletín Oficial N° {o['boletin']})",
                         "ordenanza": o["label"], "boletin": o["boletin"],
                         "sesion": ses_label({"session_number": o["session"]["number"], "session_kind": o["session"]["kind"],
                                              "date": date.fromisoformat(o["session"]["date"]).strftime("%d/%m/%Y")}),
                         "titulo": privado(n["title"]), "texto": privado(prensa.note_text(n))[:MAX_TEXT], "url": o["url"]})
    for k, a in sorted(anexos.items()):  # la semana en que se capturó, aunque el Boletín sea anterior
        cap = datetime.fromisoformat(a["capturado"]) if a.get("capturado") else None
        if a.get("anexo") and not a.get("anexo_en_boletin") and en_rango(cap, desde, hasta):
            material.append({"tipo": f"Anexo de la ordenanza {a['label']} (Digesto del Concejo, capturado el "
                                     f"{cap:%d/%m/%Y}; NO está en el Boletín Oficial N° {a['boletin']})", "sesion": "",
                             "titulo": f"Anexo {a['label']}", "texto": privado(a["anexo"])[:MAX_ANEXO], "url": a["pdf"]})
    seen, unique = set(), []
    for m in material:  # el mismo punto puede llegar dos veces
        if (m["url"], m["titulo"]) not in seen:
            seen.add((m["url"], m["titulo"]))
            if ic.con_nombre(m["texto"]) and nombres(m["texto"]):  # Haiku sabe de entrada a quién nombrar
                m["con_nombre"] = nombres(m["texto"])
            unique.append(m)
    return held, unique


def archivados(desde, carpeta=NEWSLETTERS_DIR):
    """[(fecha, título, texto)] de newsletters/AAAA-MM-DD.md anteriores a desde: "# título" y el texto."""
    out = []
    for f in sorted(Path(carpeta).glob("*.md")):
        d = date.fromisoformat(f.stem)
        if d < desde:
            titulo, _, texto = f.read_text(encoding="utf-8").partition("\n")
            out.append((d, f"{titulo.lstrip('# ').strip()} (publicado en Substack el {d:%d/%m/%Y})", texto.strip()))
    return out


def anteriores(desde, avisos):
    """Newsletters anteriores a la semana, del más nuevo al más viejo: [(título, texto)]. Los archivados en newsletters/
    y los borradores de los Issues "newsletter-borrador" (sin los "no hay borrador")."""
    out = archivados(desde)
    for i in auxiliar("los Issues de newsletters anteriores", lambda: ip.gh(
            "GET", "/issues", params={"labels": LABEL[0], "state": "all", "per_page": 50}), [], avisos):
        m = re.search(r"al (\d{2}/\d{2}/\d{4})", i["title"])
        fin = m and datetime.strptime(m.group(1), "%d/%m/%Y").date()
        if fin and fin < desde and (i["body"] or "").startswith("#"):
            out.append((fin, i["title"], (i["body"] or "").split("\n---\n")[0].strip()))
    return [(t, b) for _, t, b in sorted(out, key=lambda x: x[0], reverse=True)[:PREVIOS]]


def conteos(material):
    """{N° de Boletín: cuántas de sus ordenanzas hay en el material}: la cifra la pone el código, no Haiku."""
    return dict(Counter(m["boletin"] for m in material if m.get("ordenanza")))


def problems(draft, material, held=(), contexto=""):
    """Lo que hace inservible un borrador: tablas, links que no vienen del material, documentos que faltan, números que
    no están en los documentos, un título que no sale de lo aprobado en la sesión de la semana (held), la sección del
    Boletín sin agrupar, documentos o números de una persona, declaraciones de interés o aniversarios, ítems de relleno,
    o una sesión ya realizada contada como por venir. contexto: el resto del mensaje a Haiku (fechas de la semana)."""
    given = {unquote(m["url"]) for m in material}
    used = {unquote(u.rstrip(".,;:")) for u in URL_PAT.findall(draft)}
    out = ["tiene una tabla (Substack no las soporta)"] if TABLE_PAT.search(draft) else []
    out += [f"link que no está en los documentos: {u}" for u in sorted(used - given)]
    out += [f"falta el documento: {u}" for u in sorted(given - used)]
    for m in material:
        if m.get("tipo") == SIN_RESULTADO and m["expediente"] not in draft:
            out.append(f"falta el punto del orden del día expte. {m['expediente']} de la {m['sesion'].split(' (')[0]}")
    cuentas = {str(n) for n in conteos(material).values()}
    # "1.000" y "1000" son el mismo número; "79,5" y "79" no
    en_material = {t.replace(".", "") for t in NUM_TOKEN_PAT.findall(json.dumps(material, ensure_ascii=False) + contexto)} | cuentas
    # shortcut: solo cifras en dígitos; "veintidós ordenanzas" en letras pasa sin control.
    for line in URL_PAT.sub("", draft).splitlines():
        for m in NUM_TOKEN_PAT.finditer(line):
            # "15 ordenanzas" es una cantidad; "Boletín Oficial N° 831 ordenanzas…", no
            de_ordenanzas = re.match(r"\s*ordenanzas", line[m.end():], re.I) and not re.search(r"N[°º]\s*$", line[:m.start()])
            validas = en_material
            if de_ordenanzas:  # "X y otras 37 ordenanzas" con 38 en el Boletín
                otras = re.search(r"\botras\s+$", line[:m.start()], re.I)
                validas = {str(int(n) - 1) for n in cuentas} if otras else cuentas
            if m.group(0).replace(".", "") not in validas:
                out.append(f"{'el título' if line.startswith('# ') else 'el texto'} dice {m.group(0)}"
                           f"{' ordenanzas' if de_ordenanzas else ''} y eso no está en los documentos"
                           + (f" (son {', '.join(sorted(cuentas))})" if de_ordenanzas else ""))
    semana = [m["titulo"] for m in material if m.get("tipo") == "aprobado" and m.get("sesion") in held]
    claves = {w for t in semana for w in re.findall(r"\w{5,}", t.casefold())} - GENERICAS
    for t in re.findall(r"^# (.*)$", draft, re.M) if semana else []:
        if re.search(r"bolet[íi]n|ordenanza", t, re.I) or not claves & set(re.findall(r"\w{5,}", t.casefold())):
            out.append(f"el título \"{t}\" tiene que ser lo aprobado en la sesión de la semana ({'; '.join(semana)}), "
                       "no el Boletín Oficial ni otro tema")
    for s in BOLETIN_SEC_PAT.finditer(draft):
        vinetas = re.findall(r"^\s*-\s.*$", s.group("s"), re.M)
        if len(vinetas) > MAX_BOLETIN:
            out.append(f"la sección del Boletín tiene {len(vinetas)} viñetas: agrupá las ordenanzas por tema en 3 o 4")
        anexos = [unquote(m["url"]) for m in material if m.get("tipo", "").startswith("Anexo")]
        for v in vinetas:
            palabras = len(re.sub(r"\]\([^)]*\)", "]", v).split()) - 1  # sin el guion ni las URLs
            if palabras > MAX_PALABRAS and not any(a in unquote(v) for a in anexos):  # el Anexo no se recorta
                out.append(f"la viñeta del Boletín \"{v.strip()[:60]}…\" tiene {palabras} palabras: máximo {MAX_PALABRAS}. "
                           "Sacá montos, fechas y detalles, o pasá ordenanzas a otra viñeta; los nombres quedan")
    for line in draft.splitlines():
        if AUTOCOMENTARIO_PAT.search(line):
            out.append(f"\"{line.strip()[:80]}\" comenta el propio newsletter: contá solo lo que dicen los documentos")
    for m in material:  # el Anexo dice los porcentajes con "%" y el total, como el documento ("79 %")
        total = PORCENTAJE_PAT.search(m["texto"]) if m.get("tipo", "").startswith("Anexo") else None
        lineas = [ln for ln in draft.splitlines() if unquote(m["url"]) in unquote(ln)] if total else []
        if total and not any(re.search(rf"(?<![\d,]){re.escape(total.group(1))}\s*%", ln) for ln in lineas):
            out.append(f"el ítem del Anexo ({m['titulo']}) tiene que decir el total del {total.group(1)} % y los "
                       "porcentajes con \"%\", como el documento")
    texto = draft.casefold()
    for m in material:  # quien se nombra (ic.con_nombre) va con todos sus nombres; se controla el apellido
        falta = [n for n in nombres(m.get("texto")) if n.split()[-1].casefold() not in texto] if ic.con_nombre(m.get("texto")) else []
        if falta:
            doc = f"la ordenanza {m['ordenanza']}" if m.get("ordenanza") else f"\"{m['titulo'][:70]}\""
            out.append(f"{doc} va con el nombre de {', '.join(falta)}: escribilo junto a la ordenanza o el tema")
    found = ic.ids(draft)
    if found:  # el prompt lo pide, el código lo garantiza (como check_caption de Instagram)
        out.append(f"trae documentos o números que no se publican ({', '.join(found)}): sacalos y dejá solo la palabra")
    for line in re.findall(r"^#.*$", draft, re.M):
        if excluded(line.lstrip("# ")):
            out.append(f"el título \"{line.lstrip('# ')}\" es una declaración de interés o un aniversario: no va")
    for m in ITEM_PAT.finditer(draft):
        tema = (m.group("b") or m.group("c") or "").strip()
        if excluded(tema) or excluded(m.group("t")):  # "**Parque Cervecero**: autoriza ... y lo declaró de interés" va
            out.append(f"el ítem \"{tema}\" es una declaración de interés o un aniversario: sacalo")
    for line in draft.splitlines():  # en viñetas y en párrafos
        if RELLENO_PAT.search(line):
            out.append(f"\"{line.strip()[:80]}\" remite a otro ítem: contá qué dice o sacalo")
    for m in material:
        if m.get("tipo", "").startswith("sesión realizada") and m["texto"] not in draft:
            out.append(f"falta, tal cual, la oración \"{m['texto']}\" con el link al orden del día")
        elif m.get("tipo") == "sesión realizada, resultado publicado en parte" and f"**{m['texto']}**" not in draft:
            out.append(f"la oración \"{m['texto']}\" va en negrita")
    for ses in {m["sesion"] for m in material if m.get("tipo") == "aprobado" or m.get("tipo", "").startswith("sesión realizada")}:
        n = re.escape(ses.split(" (")[0])
        if re.search(rf"^#+[^\n]*tratar[^\n]*{n}\b", draft, re.M | re.I):
            out.append(f"la {ses.split(' (')[0]} ya se realizó: no va en \"Qué va a tratar\"")
    return out


def redactar(desde, hasta, held, material, previos):
    client = anthropic.Anthropic(api_key=ic.api_key(), timeout=180.0, max_retries=2)
    prev = "\n\n".join(f"=== {t} ===\n{b}" for t, b in previos) or "(no hay newsletters anteriores)"
    contexto = (f"Semana del {desde:%d/%m/%Y} al {hasta:%d/%m/%Y}. Sesiones del Concejo en la semana: "
                f"{', '.join(held) or 'ninguna'}.\n"
                + "".join(f"Ordenanzas del Boletín Oficial N° {b} en estos documentos: {n}.\n" for b, n in conteos(material).items()))
    prompt = (contexto + f"\nNewsletters anteriores (para no repetir y contar qué hay de nuevo):\n{prev}\n\n"
              f"Documentos oficiales de esta semana:\n{json.dumps(material, ensure_ascii=False, indent=1)}")
    messages = [{"role": "user", "content": prompt}]
    for _ in range(4):  # tres reintentos, diciéndole qué estuvo mal (a veces la primera respuesta viene vacía)
        # effort bajo, como el resto del repo: con el default el razonamiento se come max_tokens y el texto sale vacío
        resp = client.messages.create(model=ic.CRIOLLO_MODEL, max_tokens=16000, system=SYSTEM, messages=messages,
                                      output_config={"effort": "low"})
        draft = "".join(b.text for b in resp.content if b.type == "text").strip()
        bad = problems(draft, material, held, contexto) + ([f"respuesta cortada ({resp.stop_reason})"] if resp.stop_reason != "end_turn" else [])
        if not bad:
            return draft
        messages += [{"role": "assistant", "content": draft},
                     {"role": "user", "content": "Corregí esto y devolvé el newsletter completo:\n- " + "\n- ".join(bad)}]
    raise RuntimeError("El borrador no cumple las reglas después de los reintentos:\n- " + "\n- ".join(bad))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hasta", type=date.fromisoformat, default=datetime.now(ARG).date())
    ap.add_argument("--sin-issue", action="store_true", help="imprime el borrador en lugar de abrir el Issue")
    a = ap.parse_args()
    desde = a.hasta - timedelta(days=6)
    avisos = []
    held, material = collect(desde, a.hasta, avisos)
    title = f"Newsletter · semana del {desde:%d/%m} al {a.hasta:%d/%m/%Y}"
    if not held and not material:
        body = (f"No hubo sesión del Concejo entre el {desde:%d/%m/%Y} y el {a.hasta:%d/%m/%Y} ni documentos oficiales "
                "nuevos. Esta semana no hay borrador: no se inventa contenido.")
    else:
        body = redactar(desde, a.hasta, held, material, anteriores(desde, avisos))
        body += ("\n\n---\n\n<sub>Borrador generado con Claude Haiku a partir de " f"{len(material)} documentos oficiales. "
                 "Copiá desde el título hasta la firma; esta línea no va.</sub>")
    if avisos:  # debajo del "---": no se copia al newsletter
        body += "\n\n" + "\n".join(f"> ⚠️ {x}" for x in avisos)
    if a.sin_issue:
        print(f"{title}\n\n{body}")
        return 0
    ip.gh("POST", "/labels", ok=(201, 422), json={"name": LABEL[0], "color": LABEL[1]})  # 422: ya existe
    print(f"Issue: {ip.create_issue(title, body, [LABEL[0]])[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
