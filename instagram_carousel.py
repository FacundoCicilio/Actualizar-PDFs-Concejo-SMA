# Carrusel de Instagram (1080x1350) + texto del post para una orden del día procesada. No publica nada.
#   python instagram_carousel.py tests/orden-del-dia-23-2026.pdf --out preview
#   python instagram_carousel.py processed/<doc_id>.json --out preview
#   Con ANTHROPIC_API_KEY (variable de entorno o archivo .env del repo), cada placa suma una línea "En criollo"
#   generada con Claude Haiku.
import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path

import anthropic
from PIL import Image, ImageDraw, ImageFont

from actas_parser import IMPACT_MESSAGES, MONTHS, process_pdf

W, H = 1080, 1350
M = 90  # margen lateral
BRAND = "Observatorio de Actas SMA"
MAX_ITEMS = 8

BG = "#F6F4EF"
INK = "#1B2430"
MUTED = "#5B6573"
LINE = "#D9D4C7"
ACCENT = "#1F4E79"
ACCENT_SOFT = "#C9D8E8"
APROBADO = ("#2E6B3F", "#CFE3D4")   # acento (fondo) y suave de "Qué se aprobó": se distingue del azul del orden del día
DESTACADO = ("#8A4B0F", "#F1DCC4")
PUESTA = ("#4B2E6F", "#DCD0EA")
FUENTE = "Fuente: Prensa del Concejo Deliberante (prensacd.cdsma.gob.ar)"

# Peso de cada impacto para ordenar los ítems: primero lo que toca el bolsillo y los servicios.
IMPACT_WEIGHT = {
    "💰 Bolsillo": 5, "🚰 Servicios": 4, "🚦 Tránsito/Seguridad": 3, "🏗 Infraestructura": 3,
    "🏘 Urbanismo": 3, "🌿 Ambiente": 3, "📚 Educación": 3, "🏪 Comercio": 2, "🎭 Cultura/Eventos": 1,
}
TAG_COLOR = {
    "💰 Bolsillo": "#9A3B2E", "🚰 Servicios": "#1F6F8B", "🚦 Tránsito/Seguridad": "#9C6614",
    "🏗 Infraestructura": "#4F5390", "🏘 Urbanismo": "#3E7C59", "🌿 Ambiente": "#2F6E46",
    "📚 Educación": "#5A6B1E", "🏪 Comercio": "#7E5227", "🎭 Cultura/Eventos": "#7A3E73",
}
HASHTAGS = "#SanMartinDeLosAndes #SMAndes #ConcejoDeliberante #Neuquen #Patagonia #ObservatorioDeActas #TransparenciaMunicipal"

# Inter (licencia OFL, fonts/OFL.txt) incluida en el repo: las placas salen idénticas en Windows y en Actions.
FONT_FILES = {False: Path(__file__).with_name("fonts") / "Inter-Regular.ttf",
              True: Path(__file__).with_name("fonts") / "Inter-Bold.ttf"}
_fonts = {}


def font(size, bold=False):
    key = (size, bold)
    if key not in _fonts:
        _fonts[key] = ImageFont.truetype(str(FONT_FILES[bold]), size)
    return _fonts[key]


def strip_emoji(s):
    return re.sub(r"[\U0001F000-\U0001FAFF☀-➿️]", "", s or "").strip()


def split_action(que_dice):
    """'Dictamen: Modificación…' -> ('Dictamen', 'Modificación…'). Sin etiqueta -> ('', texto)."""
    m = re.match(r"^([A-ZÁÉÍÓÚ][\wáéíóúñ ]{2,30}):\s+(.+)$", que_dice or "", re.S)
    return (m.group(1), m.group(2)) if m else ("", que_dice or "")


# ------------------------------------------------------------------ anonimización
# Personas particulares -> "un vecino" / "una vecina". Concejales, funcionarios e instituciones quedan.
_NAME = r"[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+(?:de\s+|del\s+)?[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+){0,4}"
_ART = r"(?P<art>\b(?:del|de la|el|la|al|a la)\s+)?"
_OFFICIAL = re.compile(r"(?:Intendente|Viceintendente|Concejal|Secretari|President|Director|Ministr|Gobernador|Juez|Defensor)", re.I)
_TITLE_PAT = re.compile(_ART + r"(?P<t>Sra|Srta|Sr|Dra|Dr|Ing|Arq|Lic|Prof|Cdora|Cdor)\.\s+(?P<n>" + _NAME + r")")
_ROLE_PAT = re.compile(_ART + r"(?P<role>vecin[oa]|joven(?:\s+sanmartinense)?)\s+(?P<n>" + _NAME + r")")
_OWNER_PAT = re.compile(r"\bpropiedad de (?P<n>" + _NAME + r")(?P<more>\s+y\s+" + _NAME + r")?")
_PAIR_PAT = re.compile(r"\b(de\s+)?una? vecin[oa] y (?:de\s+)?una? vecin[oa]\b")
_INSTITUTION = re.compile(r"^(?:Municipalidad|Provincia|Estado|Asociaci|Club|Cooperativa|Fundaci|Junta|Escuela|Iglesia)", re.I)


def _with_article(art, noun, female):
    un = "una" if female else "un"
    art = (art or "").strip().lower()
    prefix = {"del": "de ", "de la": "de ", "al": "a ", "a la": "a "}.get(art, "")
    return f"{prefix}{un} {noun}"


def anonymize(text):
    def title(m):
        before = text[max(0, m.start() - 40):m.start()]
        if _OFFICIAL.search(before) or _OFFICIAL.match(m.group("n")):  # "Intendente Municipal Dr. …", "Sr. Intendente …"
            return m.group(0)
        female = m.group("t") in ("Sra", "Srta", "Dra", "Cdora")
        noun = ("vecina" if female else "vecino") if m.group("t").startswith("Sr") else ("profesional")
        return _with_article(m.group("art"), noun, female)

    def role(m):
        r = m.group("role")
        return _with_article(m.group("art"), r, r.startswith("vecina"))

    def owner(m):
        if _INSTITUTION.match(m.group("n")):
            return m.group(0)
        return "propiedad de particulares" if m.group("more") else "propiedad de un particular"

    text = text or ""
    text = _TITLE_PAT.sub(title, text)
    text = _ROLE_PAT.sub(role, text)
    text = _OWNER_PAT.sub(owner, text)
    return _PAIR_PAT.sub(lambda m: (m.group(1) or "") + "vecinos", text)  # "de una vecina y de un vecino" -> "de vecinos"


# ------------------------------------------------------------------ relevancia
# Alcance general (tarifas, obras, servicios, normas) frente a trámites de un lote o una persona y reconocimientos.
INDIVIDUAL_PAT = re.compile(
    r"\b(?:lotes?\s+\d|manzana\s+\w|chacra\s+\d|quinta\s+\d|nomenclatura catastral|escritura|boleto (?:de )?compraventa"
    r"|regularizaci[oó]n dominial|responsable de pago|a favor de|persona en situaci[oó]n|subsidio"
    r"|solicitud de(?:l| la) Sra?\."  # pedido personal; "contratación directa de la Sra. … agua potable a barrios" sí es general
    r"|distinci[oó]n|embajador|declar\w* de inter[eé]s|donaci[oó]n)", re.I)
GENERAL_PAT = re.compile(
    r"\b(?:tarifa|tasas?|impuesto|tribut|arancel|contribuci[oó]n por mejoras|obras? p[uú]blica|pavimento|licitaci[oó]n"
    r"|servicios?|agua|cloaca|residuos|transporte|tr[aá]nsito|c[oó]digo|reglament|ordenanza|programa|convenio)", re.I)
GENERAL_TAGS = {"💰 Bolsillo", "🚰 Servicios", "🏗 Infraestructura", "🚦 Tránsito/Seguridad"}


def is_relevant(item):
    text = item.get("title") or item.get("que_dice") or ""
    if INDIVIDUAL_PAT.search(text):
        return False
    return bool(GENERAL_TAGS & set(item.get("impact_tags") or [])) or bool(GENERAL_PAT.search(text))


# ------------------------------------------------------------------ "En criollo" (Claude)
CRIOLLO_MODEL = "claude-haiku-5-5"
CRIOLLO_SYSTEM = (
    "Reescribís puntos del orden del día del Concejo Deliberante de San Martín de los Andes para vecinos sin "
    "formación legal. Devolvé una sola oración de hasta 20 palabras, en español rioplatense simple, que diga qué va a "
    "tratar el Concejo y a quién afectaría. Es el orden del día de una sesión que todavía no se votó: no des nada por "
    "aprobado ni decidido (nada de 'se aprueba' o 'se modifica'); usá formas como 'El Concejo trata…' o 'Se propone…', "
    "y para cambios de normas o tarifas 'El Concejo trata un cambio en…' (no 'trata modificar'). "
    "No inventes datos que no estén en el texto. No nombres a personas particulares (usá "
    "'un vecino' o 'una vecina'). Sin comillas, sin emojis y sin prefijos como 'En criollo:'."
)
DOTENV = Path(__file__).with_name(".env")  # está en .gitignore
# Es un orden del día: nada está votado. Si el modelo igual lo da por hecho, la línea se descarta.
DECIDED_PAT = re.compile(r"\bse\s+(?:aprueba|aprob[oó]|modifica|modific[oó])\b", re.I)
_client = None
_warned_dotenv = set()


def env_value(name):
    """Variable del entorno o, si no está, del .env del repo. Solo lee esa clave; vacía cuenta como ausente."""
    if os.getenv(name):
        return os.environ[name]
    try:
        lines = DOTENV.read_text(encoding="utf-8-sig").splitlines()
    except OSError:  # no hay .env
        return None
    for ln in lines:
        key, sep, value = ln.strip().partition("=")
        if sep and key.strip().removeprefix("export ").strip() == name:
            return value.strip().strip("\"'") or None
    if name not in _warned_dotenv:  # una vez por variable y ejecución; nunca se imprime nada del archivo
        _warned_dotenv.add(name)
        print(f"Aviso: el archivo .env existe pero no tiene la linea {name}=...")
    return None


def api_key():
    return env_value("ANTHROPIC_API_KEY")


def _complete(system, text, output_config=None):
    """Texto de la respuesta de Claude Haiku. Lanza anthropic.APIError si la API falla; None si la respuesta no sirve."""
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=api_key(), timeout=30.0, max_retries=2)
    resp = _client.messages.create(
        model=CRIOLLO_MODEL,
        max_tokens=1024,
        output_config={"effort": "low", **(output_config or {})},
        system=system,
        messages=[{"role": "user", "content": text}],  # ya anonimizado: los nombres no salen del sistema
    )
    if resp.stop_reason != "end_turn":  # refusal / max_tokens: mejor sin línea que con una cortada
        return None
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def _ask_claude(official_text):
    out = _complete(CRIOLLO_SYSTEM, official_text)
    if out is None:
        return None
    out = re.sub(r"^En criollo:\s*", "", out, flags=re.I).strip("\"“”' ")
    if DECIDED_PAT.search(out):
        print("Aviso: linea 'En criollo' descartada (daba el tema por aprobado)")
        return None
    out = anonymize(out)
    return shorten(out, 180) if out else None


def criollo(official_text):
    """Línea en lenguaje simple, o None si no hay API key o la API falla: la placa sale igual, sin esa línea."""
    if not api_key():
        return None
    try:
        return _ask_claude(official_text)
    except anthropic.APIError as e:
        print(f"Aviso: linea 'En criollo' no disponible ({type(e).__name__})")  # ASCII: un emoji rompe la consola de Windows
        return None


def criollo_lines(items):
    """Líneas "En criollo" de todos los ítems o de ninguno (no se mezclan placas con y sin línea si la API falla).
    Devuelve (líneas, error): error es un texto para avisar, sin datos sensibles, o None."""
    if not items:
        return [], None
    if not api_key():
        return [None] * len(items), "Falta ANTHROPIC_API_KEY: las placas salen sin 'En criollo'."
    try:
        return [_ask_claude(official_text(it)) for it in items], None
    except anthropic.APIError as e:
        return [None] * len(items), f"La API de Anthropic falló ({type(e).__name__}): {getattr(e, 'message', '')[:300]}"


def impact_score(item):
    # Manda el impacto más fuerte; la cantidad de etiquetas solo desempata (sumar premia etiquetas falsas por palabras sueltas).
    weights = [IMPACT_WEIGHT.get(t, 0) for t in item.get("impact_tags") or []]
    return (max(weights, default=0), len(weights))


PROJECT_SPLIT = re.compile(r"\s*[–—-]?\s*(?:Tema:\s*)?Proyecto\s+\d+\s*[\):.\-]\s*[–—-]?\s*")


def slide_text(body):
    """Primer proyecto de un ítem con varios ("… – Proyecto 2: - Dictamen s/ …"), más un aviso de cuántos quedan."""
    parts = [p.strip(" -–—") for p in PROJECT_SPLIT.split(body) if p.strip(" -–—")]
    if not parts:
        return body
    extra = len(parts) - 1
    return parts[0] + (f" (+{extra} proyecto{'s' if extra > 1 else ''} más)" if extra else "")


def select_items(doc, max_items=MAX_ITEMS):
    """Solo ítems de alcance general, el de mayor impacto primero. Si hay menos de max_items, el carrusel es más corto."""
    items = [it for it in doc.get("items") or [] if (it.get("que_dice") or "").strip() and is_relevant(it)]
    return sorted(items, key=impact_score, reverse=True)[:max_items]  # sorted es estable: empata por orden del documento


def official_text(item):
    """Texto oficial que se muestra (y se manda a Claude): sin etiqueta, primer proyecto y anonimizado."""
    return anonymize(slide_text(split_action(item.get("que_dice"))[1]))


def date_long(date_str):
    try:
        d = datetime.strptime(date_str or "", "%d/%m/%Y")
    except ValueError:
        return date_str or ""
    month = next(name for name, num in MONTHS.items() if num == d.month and name != "setiembre")
    return f"{d.day} de {month} de {d.year}"


def session_word(doc):
    return "Sesión Especial" if doc.get("session_kind") == "Especial" else "Sesión"


def session_title(doc):
    return f"{session_word(doc)} N° {doc['session_number']}" if doc.get("session_number") else "Orden del Día"


def more_count(doc, items):
    """Puntos del orden del día que no entran en el carrusel (sin alcance general o más allá de MAX_ITEMS)."""
    return max(0, sum(1 for it in doc.get("items") or [] if (it.get("que_dice") or "").strip()) - len(items))


def y_mas(n):
    return f"y {n} tema{'s' if n != 1 else ''} más" if n else ""


def shorten(text, limit):
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0].rstrip(",;:.") + "…"


def build_caption(doc, items):
    head = " · ".join(x for x in ["🏛️ Orden del Día", session_title(doc) if doc.get("session_number") else "", date_long(doc.get("date"))] if x)
    n = len(doc.get("items") or [])
    lines = [head, "", f"El Concejo Deliberante de San Martín de los Andes trata {n} puntos en esta sesión."]
    if items:
        lines[-1] += " Los de alcance general:"
        lines += [""] + [f"• {shorten(official_text(it), 110)}" for it in items]
        if more_count(doc, items):
            lines += [f"…{y_mas(more_count(doc, items))}."]
        lines += ["", "Deslizá para ver el detalle 👉"]
    lines += ["Más info: link en la bio.", "", HASHTAGS]
    return "\n".join(lines)


# ------------------------------------------------------------------ dibujo

def wrap(draw, text, fnt, width):
    lines, cur = [], ""
    for word in text.split():
        test = f"{cur} {word}".strip()
        if draw.textlength(test, font=fnt) <= width or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def fit_text(draw, text, width, max_height, sizes=(56, 52, 48, 44, 40, 36), bold=False):
    """Mayor tamaño de letra con el que el texto entra en la caja; devuelve (font, líneas, alto de línea)."""
    for size in sizes:
        fnt = font(size, bold)
        lines = wrap(draw, text, fnt, width)
        lh = int(size * 1.32)
        if len(lines) * lh <= max_height:
            return fnt, lines, lh
    return fnt, clip(draw, lines, max(1, max_height // lh), fnt, width), lh  # último recurso: se corta con "…"


def clip(draw, lines, n, fnt, width):
    """Las primeras n líneas; si sobran, la última termina en "…" sin pasarse del ancho."""
    if len(lines) <= n:
        return lines
    words = lines[n - 1].split()
    while len(words) > 1 and draw.textlength(" ".join(words) + "…", font=fnt) > width:
        words.pop()
    return lines[:n - 1] + [" ".join(words).rstrip(",;:.") + "…"]


def draw_lines(draw, xy, lines, fnt, lh, fill):
    x, y = xy
    for ln in lines:
        draw.text((x, y), ln, font=fnt, fill=fill)
        y += lh
    return y


def chip(draw, x, y, label, color):
    fnt = font(30, True)
    tw = draw.textlength(label, font=fnt)
    draw.rounded_rectangle((x, y, x + tw + 44, y + 56), radius=28, fill=color)
    draw.text((x + 22, y + 11), label, font=fnt, fill="white")
    return x + tw + 44 + 16


def brand_bar(draw, color, right_text="", right_color=None):
    draw.text((M, 70), BRAND.upper(), font=font(28, True), fill=color)
    if right_text:
        fnt = font(28)
        draw.text((W - M - draw.textlength(right_text, font=fnt), 70), right_text, font=fnt, fill=right_color or color)


def cover(doc, n_selected, n_more=0, kicker="ORDEN DEL DÍA", colors=(ACCENT, ACCENT_SOFT), teaser=None):
    accent, soft = colors
    img = Image.new("RGB", (W, H), accent)
    d = ImageDraw.Draw(img)
    brand_bar(d, "white")
    d.line((M, 130, W - M, 130), fill=soft, width=2)
    y = 330
    d.text((M, y), kicker, font=font(40, True), fill=soft)
    y += 90
    if doc.get("session_number"):
        d.text((M, y), session_word(doc), font=font(84, True), fill="white")
        d.text((M, y + 100), f"N° {doc['session_number']}", font=font(170, True), fill="white")
        y += 330
    else:
        d.text((M, y), "Orden del Día", font=font(96, True), fill="white")
        y += 140
    d.text((M, y), date_long(doc.get("date")), font=font(52), fill="white")
    y += 120
    d.line((M, y, M + 140, y), fill=soft, width=6)
    y += 50
    sub_font, sub_lines, lh = fit_text(d, "Concejo Deliberante de San Martín de los Andes", W - 2 * M, 200, sizes=(40,))
    draw_lines(d, (M, y), sub_lines, sub_font, lh, soft)
    if teaser is None:
        teaser = (f"Lo más importante en {n_selected} punto{'s' if n_selected != 1 else ''}  →" if n_selected
                  else "Sin temas de alcance general en esta sesión")
    d.text((M, H - (210 if n_more else 150)), teaser, font=font(40, True), fill="white")
    if n_more:
        d.text((M, H - 150), y_mas(n_more), font=font(36), fill=soft)
    return img


def item_slide(doc, item, simple, pos, n_selected, page, total):
    action = split_action(item.get("que_dice"))[0]
    official = official_text(item)
    kicker = f"{action.upper()} · PUNTO {pos} DE {n_selected}" if action else f"PUNTO {pos} DE {n_selected}"
    tags = (item.get("impact_tags") or [])[:2]
    impact_msg = IMPACT_MESSAGES.get(tags[0], "") if tags else "Informativo: sin impacto directo claro (puede ser interno o administrativo)."

    blocks = [text_block(kicker, (32,), ACCENT, 36, bold=True)]
    if simple:
        blocks.append(text_block("En criollo:", (36,), ACCENT, 8, bold=True))
        # textos cortos -> letra más grande: llena la placa en vez de dejar aire
        blocks.append(text_block(simple, (72, 66, 60, 56, 52, 48, 44), INK, 44, 440, bold=True))
        blocks.append(text_block(official, (40, 36, 34, 32, 30), MUTED, 44, 300))
    else:
        blocks.append(text_block(official, (84, 76, 68, 60, 56, 52, 48, 44, 40, 36), INK, 48, 660))
    blocks += impact_blocks(tags, impact_msg)
    ref = f"Ref. {item.get('expediente') or item.get('key')}" if item.get("expediente") or item.get("key") else ""
    return content_slide(session_title(doc), blocks, ref, f"{page}/{total}")


MEASURE = ImageDraw.Draw(Image.new("RGB", (1, 1)))  # solo para medir texto antes de dibujar


def text_block(text, sizes, color, gap, max_h=None, bold=False):
    """Bloque de texto para content_slide: el tamaño de letra se elige al armar la placa (ver layout)."""
    return ("text", text, sizes, bold, color, gap, max_h or int(max(sizes) * 1.32))


def impact_blocks(tags, impact_msg):
    return ([("rule", None, None, None, LINE, 40, 2)] + ([("chips", tags, None, None, None, 28, 56)] if tags else [])
            + [text_block(impact_msg, (38, 34, 30), MUTED, 0, 140)])


CONTENT_TOP, CONTENT_BOTTOM = 170, H - 140  # el pie ("Ref. …" y la página) está en H - 100
SCALES = (1, 0.9, 0.8, 0.7, 0.6)


def layout(blocks, avail):
    """Bloques listos para dibujar: (tipo, líneas, fuente, alto de línea, color, espacio después). Si no entran en
    avail, achica todas las letras; si ni así entran, recorta con "…". Nunca se pasan de avail."""
    for scale in SCALES:
        out = []
        for kind, payload, sizes, bold, color, gap, max_h in blocks:
            gap = int(gap * scale)
            if kind == "text":
                f, ls, lh = fit_text(MEASURE, payload, W - 2 * M, int(max_h * scale), [max(20, int(x * scale)) for x in sizes], bold)
                out.append((kind, ls, f, lh, color, gap))
            else:
                out.append((kind, payload, None, max_h, color, gap))
        if sum(block_height(b) for b in out) <= avail:
            return out
    clipped, room = [], avail  # último recurso
    for kind, lines, f, lh, color, gap in out:
        if kind == "text":
            n = min(len(lines), room // lh)
            if n < 1:
                break
            lines = clip(MEASURE, lines, n, f, W - 2 * M) if n < len(lines) else lines
        elif lh > room:
            break
        clipped.append((kind, lines, f, lh, color, gap))
        room -= block_height(clipped[-1])
        if room <= 0:
            break
    return clipped


def block_height(b):
    kind, lines, _, lh, _, gap = b
    return (len(lines) * lh if kind == "text" else lh) + gap


def content_slide(header, blocks, footer_left, page_txt, accent=ACCENT):
    """Placa clara. blocks (text_block, impact_blocks) de arriba a abajo; se miden primero y se centran en el alto
    libre, sin invadir nunca el pie."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    brand_bar(d, accent, header, MUTED)
    d.line((M, 130, W - M, 130), fill=LINE, width=2)

    avail = CONTENT_BOTTOM - CONTENT_TOP
    blocks = layout(blocks, avail)
    y = CONTENT_TOP + max(0, (avail - sum(block_height(b) for b in blocks)) // 2)
    for kind, lines, fnt, lh, color, gap in blocks:
        if kind == "text":
            y = draw_lines(d, (M, y), lines, fnt, lh, color)
        elif kind == "rule":
            d.line((M, y, W - M, y), fill=color, width=lh)
            y += lh
        else:
            x = M
            for t in lines:
                x = chip(d, x, y, strip_emoji(t), TAG_COLOR.get(t, MUTED))
            y += lh
        y += gap

    footer_font = font(28)
    if footer_left:
        d.text((M, H - 100), footer_left, font=footer_font, fill=MUTED)
    d.text((W - M - d.textlength(page_txt, font=footer_font), H - 100), page_txt, font=footer_font, fill=MUTED)
    return img


def closing(colors=(ACCENT, ACCENT_SOFT)):
    accent, soft = colors
    img = Image.new("RGB", (W, H), accent)
    d = ImageDraw.Draw(img)
    brand_bar(d, "white")
    d.line((M, 130, W - M, 130), fill=soft, width=2)
    d.text((M, 430), "¿Querés saber más?", font=font(56), fill=soft)
    title_font = font(100, True)
    y = draw_lines(d, (M, 530), ["Más info:", "link en la bio"], title_font, 132, "white")
    d.line((M, y + 50, M + 140, y + 50), fill=soft, width=6)
    sub_font, sub_lines, sub_lh = fit_text(d, "Resúmenes claros y buscables de cada sesión del Concejo Deliberante de San Martín de los Andes.",
                                          W - 2 * M, 200, sizes=(40, 36))
    draw_lines(d, (M, y + 110), sub_lines, sub_font, sub_lh, "white")
    return img


def render_carousel(doc, out_dir, prefix="slide", simple_lines=None, ext="png"):
    """Escribe las placas (png o jpg) y caption.txt en out_dir. Devuelve la lista de rutas de las imágenes.
    simple_lines: líneas "En criollo" ya generadas (una por ítem de select_items); si es None, se piden a Claude."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    items = select_items(doc)
    if simple_lines is None:
        simple_lines, error = criollo_lines(items)
        if error:
            print(f"Aviso: {error}")
    slides = [cover(doc, len(items), more_count(doc, items))]
    total = len(items) + 2
    slides += [item_slide(doc, it, simple, i, len(items), i + 1, total)
               for i, (it, simple) in enumerate(zip(items, simple_lines), start=1)]
    slides.append(closing())
    return save_slides(slides, out, prefix, ext, build_caption(doc, items))


def save_slides(slides, out, prefix, ext, caption):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, img in enumerate(slides, start=1):
        p = out / f"{prefix}-{i:02d}.{ext}"
        img.save(p, quality=90, optimize=True)  # quality solo aplica a JPEG (Instagram solo acepta JPEG)
        paths.append(p)
    (out / "caption.txt").write_text(caption, encoding="utf-8")
    return paths


# ------------------------------------------------------------------ notas de Prensa: "Qué se aprobó" y destacados
NOTE_SYSTEM = (
    "Resumís notas de prensa del Concejo Deliberante de San Martín de los Andes para vecinos sin formación legal. "
    "Devolvé una sola oración de hasta 25 palabras, en español rioplatense simple, que diga qué aprobó el Concejo y a "
    "quién afecta. Podés decir 'se aprobó' o 'el Concejo aprobó': la nota lo confirma. Escribí con tus palabras, sin "
    "copiar frases de la nota. Usá solo datos que estén en la nota: no inventes montos, fechas ni plazos. Si la nota "
    "cuenta varios temas, resumí el principal. No nombres a personas particulares (usá 'un vecino' o 'una vecina'). "
    "Sin comillas, sin emojis y sin prefijos."
)
DESTACADO_SYSTEM = (
    "Explicás a vecinos de San Martín de los Andes una norma que aprobó el Concejo Deliberante, a partir de una nota "
    "de prensa. Completá: titular (hasta 10 palabras), que_cambia, a_quien_afecta y desde_cuando (una oración corta "
    "cada uno), en español rioplatense simple y con tus palabras, sin copiar frases de la nota. Usá SOLO datos que "
    "estén en la nota (montos, porcentajes, plazos, alcance). Si la nota no informa alguno de los tres, dejalo vacío "
    "(\"\"): nunca lo deduzcas ni lo inventes. No nombres a personas particulares. Sin emojis. "
    "El titular es descriptivo y neutral: dice qué se modificó, sin calificar la medida (nada de 'alivio', "
    "'beneficio', 'golpe', 'ayuda' ni 'mejora'). Si la nota tiene medidas que bajan y otras que suben lo que se paga, "
    "el titular no la presenta como solo una baja ni como solo una suba: nombra lo que se modificó. Ejemplo: "
    "'El Concejo modificó la Tasa COM y la tasa ENSATUR para comercios'."
)
# Controles del titular: el prompt lo pide, pero si Haiku igual califica o simplifica, el titular se descarta.
VALORATIVE_PAT = re.compile(r"\b(?:alivi\w*|benefici(?!ari)\w*|golpe\w*|ayud\w*|mejor[ae]s?|perjudic\w*|perjuicio\w*|castig\w*"
                            r"|tarifazo\w*|premi\w*|logr\w*|hist[oó]ric\w*|important\w*|impuls\w*|favorec\w*)\b", re.I)
DOWN_PAT = re.compile(r"\b(?:baja|bajan|bajar\w*|baj[oó]|reduc\w*|rebaj\w*|exim\w*|exenci\w*|descuent\w*)\b", re.I)
UP_PAT = re.compile(r"\b(?:aument\w*|duplic\w*|triplic\w*|increment\w*|sube|suben|subir\w*|subi[oó]|suba)\b", re.I)
DESTACADO_FIELDS = ("titular", "que_cambia", "a_quien_afecta", "desde_cuando")
DESTACADO_FORMAT = {"format": {"type": "json_schema", "schema": {
    "type": "object", "properties": {k: {"type": "string"} for k in DESTACADO_FIELDS},
    "required": list(DESTACADO_FIELDS), "additionalProperties": False}}}
QUESTIONS = (("que_cambia", "¿Qué cambia?"), ("a_quien_afecta", "¿A quién afecta?"), ("desde_cuando", "¿Desde cuándo?"))
NO_INFO = "La nota no lo informa."


def numbers(text):
    return {re.sub(r"[.,]", "", n) for n in re.findall(r"\d[\d.,]*\d|\d", text or "")}


def only_note_numbers(out, note):
    """El texto generado no puede traer cifras (montos, porcentajes, fechas) que no estén en la nota."""
    # shortcut: compara cifras sueltas, no su sentido ("50%" pasa si la nota cita el "Artículo 50"); la revisión
    # por Issue del período de prueba es el control de fondo. Mejorar si aparece un caso real.
    return numbers(out) <= numbers(note)


def note_line(note):
    """Línea "En criollo" de una nota, o None si la respuesta no sirve. Lanza anthropic.APIError si la API falla."""
    out = _complete(NOTE_SYSTEM, note)
    out = anonymize(re.sub(r"^En criollo:\s*", "", out or "", flags=re.I).strip("\"“”' "))
    if not out or not only_note_numbers(out, note):
        return None
    return shorten(out, 260)


def note_lines(notes):
    """Como criollo_lines, para textos de notas: (líneas, error). Sin línea, la nota no entra en el carrusel."""
    if not api_key():
        return [None] * len(notes), "Falta ANTHROPIC_API_KEY."
    try:
        return [note_line(n) for n in notes], None
    except anthropic.APIError as e:
        return [None] * len(notes), f"La API de Anthropic falló ({type(e).__name__}): {getattr(e, 'message', '')[:300]}"


def titular_ok(titular, note):
    """Neutral (sin palabras valorativas) y, si la nota baja unas cosas y sube otras, sin mostrar solo una dirección."""
    if VALORATIVE_PAT.search(titular):
        return False
    if DOWN_PAT.search(note) and UP_PAT.search(note):
        return bool(DOWN_PAT.search(titular)) == bool(UP_PAT.search(titular))
    return True


def destacado_fields(note):
    """{titular, que_cambia, a_quien_afecta, desde_cuando} o None si no hay titular usable. Un campo vacío o con cifras
    que no están en la nota queda vacío ("la nota no lo informa"). Lanza anthropic.APIError si la API falla."""
    for _ in range(2):  # la respuesta varía: un segundo intento suele traer un titular usable
        try:
            data = json.loads(_complete(DESTACADO_SYSTEM, note, DESTACADO_FORMAT) or "")
        except ValueError:
            continue
        fields = {k: anonymize(str(data.get(k) or "").strip()) for k in DESTACADO_FIELDS}
        fields = {k: v if v and only_note_numbers(v, note) else "" for k, v in fields.items()}
        if fields["titular"] and titular_ok(fields["titular"], note):
            return fields
        print("Aviso: titular del destacado descartado (valorativo o muestra solo una baja o una suba)")
    return None


def destacado(note):
    """(campos, error) como note_lines: error es un texto para avisar, sin datos sensibles, o None."""
    if not api_key():
        return None, "Falta ANTHROPIC_API_KEY."
    try:
        return destacado_fields(note), None
    except anthropic.APIError as e:
        return None, f"La API de Anthropic falló ({type(e).__name__}): {getattr(e, 'message', '')[:300]}"


def note_slide(sdoc, note, pos, n, page, total):
    tags = (note.get("impact_tags") or [])[:2]
    impact_msg = IMPACT_MESSAGES.get(tags[0], "") if tags else "Aprobado por el Concejo Deliberante."
    blocks = [text_block(f"APROBADO · {pos} DE {n}", (32,), APROBADO[0], 36, bold=True),
              text_block("En criollo:", (36,), APROBADO[0], 8, bold=True),
              text_block(note["line"], (72, 66, 60, 56, 52, 48, 44), INK, 44, 560, bold=True)] + impact_blocks(tags, impact_msg)
    return content_slide(session_title(sdoc), blocks, "Fuente: Prensa del Concejo Deliberante", f"{page}/{total}", APROBADO[0])


def aprobado_caption(sdoc, notes):
    head = " · ".join(["✅ Qué se aprobó", session_title(sdoc), date_long(sdoc.get("date"))])
    lines = [head, "", "Lo que aprobó el Concejo Deliberante de San Martín de los Andes en esta sesión, según las notas de Prensa:", ""]
    lines += [f"• {n['line']}" for n in notes]
    lines += ["", "Deslizá para ver el detalle 👉", FUENTE, "Más info: link en la bio.", "", HASHTAGS]
    return "\n".join(lines)


def render_aprobado(sdoc, notes, out_dir, prefix="aprobado", ext="png"):
    """Carrusel "Qué se aprobó". notes: dicts con line (ya generada) e impact_tags, ya ordenados (máximo MAX_ITEMS)."""
    notes = notes[:MAX_ITEMS]
    total = len(notes) + 2
    teaser = f"Lo aprobado en {len(notes)} tema{'s' if len(notes) != 1 else ''}  →"
    slides = [cover(sdoc, len(notes), kicker="QUÉ SE APROBÓ", colors=APROBADO, teaser=teaser)]
    slides += [note_slide(sdoc, n, i, len(notes), i + 1, total) for i, n in enumerate(notes, start=1)]
    slides.append(closing(APROBADO))
    return save_slides(slides, out_dir, prefix, ext, aprobado_caption(sdoc, notes))


def destacado_cover(sdoc, fields, tags):
    accent, soft = DESTACADO
    img = Image.new("RGB", (W, H), accent)
    d = ImageDraw.Draw(img)
    brand_bar(d, "white")
    d.line((M, 130, W - M, 130), fill=soft, width=2)
    d.text((M, 300), "DESTACADO", font=font(40, True), fill=soft)
    # de arriba a abajo, cada caja con alto máximo: lo último (las etiquetas) termina antes de H - 230
    f, ls, lh = fit_text(d, fields["titular"], W - 2 * M, 430, sizes=(96, 88, 80, 72, 64, 56), bold=True)
    y = draw_lines(d, (M, 380), ls, f, lh, "white") + 30
    d.line((M, y, M + 140, y), fill=soft, width=6)
    sub = f"Aprobado en la {session_title(sdoc)} del {date_long(sdoc.get('date'))}"
    f, ls, lh = fit_text(d, sub, W - 2 * M, 106, sizes=(40, 36))
    y = draw_lines(d, (M, y + 36), ls, f, lh, soft) + 24
    x = M
    for t in tags[:2]:
        x = chip(d, x, y, strip_emoji(t), "#4A2606")  # los colores de etiqueta se pierden sobre el naranja
    d.text((M, H - 150), "Qué cambia, a quién afecta y desde cuándo  →", font=font(36, True), fill="white")
    return img


def destacado_slide(sdoc, fields):
    blocks = []
    for key, question in QUESTIONS:
        answer = fields[key] or NO_INFO
        blocks += [text_block(question, (36,), DESTACADO[0], 10, bold=True),
                   text_block(answer, (52, 48, 44, 40, 36, 32), INK if fields[key] else MUTED, 56, 260, bold=bool(fields[key]))]
    return content_slide(session_title(sdoc), blocks, "Fuente: Prensa del Concejo Deliberante", "2/2", DESTACADO[0])


def destacado_caption(sdoc, fields):
    lines = [f"📌 Destacado: {fields['titular']}", ""]
    lines += [f"{q} {fields[k] or NO_INFO}" for k, q in QUESTIONS]
    lines += ["", f"Aprobado por el Concejo Deliberante en la {session_title(sdoc)} ({date_long(sdoc.get('date'))}).",
              FUENTE, "Más info: link en la bio.", "", HASHTAGS]
    return "\n".join(lines)


def render_destacado(sdoc, fields, tags, out_dir, prefix="destacado", ext="png"):
    """Dos placas (la API de Instagram publica un carrusel con 2 o más imágenes; así se reusa el mismo circuito)."""
    slides = [destacado_cover(sdoc, fields, tags), destacado_slide(sdoc, fields)]
    return save_slides(slides, out_dir, prefix, ext, destacado_caption(sdoc, fields))


# ------------------------------------------------------------------ puesta al día (un solo post para retomar la cuenta)
def month_since(start, today):
    """"junio" si es el mismo año que hoy; si no, "diciembre de 2025"."""
    month = next(name for name, num in MONTHS.items() if num == start.month and name != "setiembre")
    return month if start.year == today.year else f"{month} de {start.year}"


def puesta_cover(start, today, n):
    accent, soft = PUESTA
    img = Image.new("RGB", (W, H), accent)
    d = ImageDraw.Draw(img)
    brand_bar(d, "white")
    d.line((M, 130, W - M, 130), fill=soft, width=2)
    d.text((M, 330), "PUESTA AL DÍA", font=font(40, True), fill=soft)
    f, ls, lh = fit_text(d, "Lo más importante que aprobó el Concejo", W - 2 * M, 360, sizes=(96, 88, 80, 72), bold=True)
    y = draw_lines(d, (M, 420), ls, f, lh, "white") + 20
    d.text((M, y), f"desde {month_since(start, today)}", font=font(56), fill="white")
    y += 120
    d.line((M, y, M + 140, y), fill=soft, width=6)
    f, ls, lh = fit_text(d, "Concejo Deliberante de San Martín de los Andes", W - 2 * M, 110, sizes=(40,))
    draw_lines(d, (M, y + 50), ls, f, lh, soft)
    d.text((M, H - 150), f"{n} tema{'s' if n != 1 else ''} destacado{'s' if n != 1 else ''}  →", font=font(40, True), fill="white")
    return img


def puesta_slide(item, pos, n, page, total):
    """item: {sdoc, titular, line, impact_tags}."""
    sdoc = item["sdoc"]
    tags = (item.get("impact_tags") or [])[:2]
    blocks = [text_block(f"{session_title(sdoc)} · {date_long(sdoc['date'])}".upper(), (32, 28), PUESTA[0], 28, bold=True),
              text_block(item["titular"], (60, 56, 52, 48, 44), INK, 32, 300, bold=True),
              text_block("En criollo:", (36,), PUESTA[0], 8, bold=True),
              text_block(item["line"], (44, 40, 36, 34, 32), INK, 44, 320)]
    blocks += impact_blocks(tags, IMPACT_MESSAGES.get(tags[0], "") if tags else "")
    return content_slide(f"Puesta al día · {pos} de {n}", blocks, "Fuente: Prensa del Concejo Deliberante", f"{page}/{total}", PUESTA[0])


def puesta_caption(start, today, items):
    lines = [f"🗓️ Puesta al día: lo más importante que aprobó el Concejo desde {month_since(start, today)}", ""]
    lines += [f"• {it['titular']} ({session_title(it['sdoc'])}, {date_long(it['sdoc']['date'])})" for it in items]
    lines += ["", "Deslizá para ver el detalle 👉", FUENTE, "Más info: link en la bio.", "", HASHTAGS]
    return "\n".join(lines)


def render_puesta(start, today, items, out_dir, prefix="puesta", ext="png"):
    """Portada + hasta MAX_ITEMS placas (ya ordenadas por impacto) + cierre."""
    items = items[:MAX_ITEMS]
    total = len(items) + 2
    slides = [puesta_cover(start, today, len(items))]
    slides += [puesta_slide(it, i, len(items), i + 1, total) for i, it in enumerate(items, start=1)]
    slides.append(closing(PUESTA))
    return save_slides(slides, out_dir, prefix, ext, puesta_caption(start, today, items))


def load_doc(path):
    p = Path(path)
    if p.suffix.lower() == ".pdf":
        return process_pdf(p.read_bytes(), p.name)
    return json.loads(p.read_text(encoding="utf-8"))


def main():
    ap = argparse.ArgumentParser(description="Genera el carrusel de Instagram de una orden del día (no publica).")
    ap.add_argument("doc", help="PDF de la orden del día o JSON procesado (processed/<doc_id>.json)")
    ap.add_argument("--out", default="preview")
    args = ap.parse_args()
    doc = load_doc(args.doc)
    prefix = f"od-{doc.get('session_number') or 'sn'}-{doc.get('session_year') or ''}".rstrip("-")
    paths = render_carousel(doc, args.out, prefix=prefix)
    print(f"{len(paths)} placas + caption.txt en {args.out}/")


if __name__ == "__main__":
    main()
