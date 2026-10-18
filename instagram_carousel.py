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

# Peso de cada impacto para ordenar los ítems: primero lo que toca el bolsillo y los servicios.
IMPACT_WEIGHT = {
    "💰 Bolsillo": 5, "🚰 Servicios": 4, "🚦 Tránsito/Seguridad": 3, "🏗 Infraestructura": 3,
    "🏘 Urbanismo": 3, "🌿 Ambiente": 3, "🏪 Comercio": 2, "🎭 Cultura/Eventos": 1,
}
TAG_COLOR = {
    "💰 Bolsillo": "#9A3B2E", "🚰 Servicios": "#1F6F8B", "🚦 Tránsito/Seguridad": "#9C6614",
    "🏗 Infraestructura": "#4F5390", "🏘 Urbanismo": "#3E7C59", "🌿 Ambiente": "#2F6E46",
    "🏪 Comercio": "#7E5227", "🎭 Cultura/Eventos": "#7A3E73",
}
HASHTAGS = "#SanMartinDeLosAndes #SMAndes #ConcejoDeliberante #Neuquen #Patagonia #ObservatorioDeActas #TransparenciaMunicipal"

FONT_FILES = {
    False: ["DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "segoeui.ttf", "arial.ttf"],
    True: ["DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "segoeuib.ttf", "arialbd.ttf"],
}
_fonts = {}


def font(size, bold=False):
    key = (size, bold)
    if key not in _fonts:
        for name in FONT_FILES[bold]:
            try:
                _fonts[key] = ImageFont.truetype(name, size)
                break
            except OSError:
                continue
        else:
            _fonts[key] = ImageFont.load_default(size)  # sin negrita, pero con tildes
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


def _ask_claude(official_text):
    """Pide la línea a Claude. Lanza anthropic.APIError si la API falla; None si la respuesta no sirve."""
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=api_key(), timeout=30.0, max_retries=2)
    resp = _client.messages.create(
        model=CRIOLLO_MODEL,
        max_tokens=1024,
        output_config={"effort": "low"},
        system=CRIOLLO_SYSTEM,
        messages=[{"role": "user", "content": official_text}],  # ya anonimizado: los nombres no salen del sistema
    )
    if resp.stop_reason != "end_turn":  # refusal / max_tokens: mejor sin línea que con una cortada
        return None
    out = "".join(b.text for b in resp.content if b.type == "text").strip()
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


def session_title(doc):
    return f"Sesión N° {doc['session_number']}" if doc.get("session_number") else "Orden del Día"


def shorten(text, limit):
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0].rstrip(",;:.") + "…"


def build_caption(doc, items):
    head = " · ".join(x for x in ["🏛️ Orden del Día", session_title(doc) if doc.get("session_number") else "", date_long(doc.get("date"))] if x)
    n = len(doc.get("items") or [])
    lines = [head, "", f"El Concejo Deliberante de San Martín de los Andes trata {n} puntos en esta sesión."]
    if items:
        lines[-1] += " Los de alcance general:"
        lines += [""] + [f"• {shorten(official_text(it), 110)}" for it in items[:3]]
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
    return fnt, lines[: max_height // lh], lh  # último recurso: se corta (los textos ya vienen limitados a ~260 caracteres)


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


def cover(doc, n_selected):
    img = Image.new("RGB", (W, H), ACCENT)
    d = ImageDraw.Draw(img)
    brand_bar(d, "white")
    d.line((M, 130, W - M, 130), fill=ACCENT_SOFT, width=2)
    y = 330
    d.text((M, y), "ORDEN DEL DÍA", font=font(40, True), fill=ACCENT_SOFT)
    y += 90
    if doc.get("session_number"):
        d.text((M, y), "Sesión", font=font(84, True), fill="white")
        d.text((M, y + 100), f"N° {doc['session_number']}", font=font(170, True), fill="white")
        y += 330
    else:
        d.text((M, y), "Orden del Día", font=font(96, True), fill="white")
        y += 140
    d.text((M, y), date_long(doc.get("date")), font=font(52), fill="white")
    y += 120
    d.line((M, y, M + 140, y), fill=ACCENT_SOFT, width=6)
    y += 50
    sub_font, sub_lines, lh = fit_text(d, "Concejo Deliberante de San Martín de los Andes", W - 2 * M, 200, sizes=(40,))
    draw_lines(d, (M, y), sub_lines, sub_font, lh, ACCENT_SOFT)
    teaser = (f"Lo más importante en {n_selected} punto{'s' if n_selected != 1 else ''}  →" if n_selected
              else "Sin temas de alcance general en esta sesión")
    d.text((M, H - 150), teaser, font=font(40, True), fill="white")
    return img


def item_slide(doc, item, simple, pos, n_selected, page, total):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    brand_bar(d, ACCENT, session_title(doc), MUTED)
    d.line((M, 130, W - M, 130), fill=LINE, width=2)

    action = split_action(item.get("que_dice"))[0]
    official = official_text(item)
    kicker = f"{action.upper()} · PUNTO {pos} DE {n_selected}" if action else f"PUNTO {pos} DE {n_selected}"
    tags = (item.get("impact_tags") or [])[:2]
    impact_msg = IMPACT_MESSAGES.get(tags[0], "") if tags else "Informativo: sin impacto directo claro (puede ser interno o administrativo)."

    # Bloques de arriba a abajo; se miden primero y se centran en el alto libre (sin hueco fijo abajo).
    width = W - 2 * M
    blocks = [("text", [kicker], font(32, True), 44, ACCENT, 36)]  # (tipo, líneas, fuente, alto de línea, color, espacio después)
    if simple:
        blocks.append(("text", ["En criollo:"], font(36, True), 48, ACCENT, 8))
        # textos cortos -> letra más grande: llena la placa en vez de dejar aire
        f, ls, lh = fit_text(d, simple, width, 440, sizes=(72, 66, 60, 56, 52, 48, 44), bold=True)
        blocks.append(("text", ls, f, lh, INK, 44))
        f, ls, lh = fit_text(d, official, width, 300, sizes=(40, 36, 34, 32, 30))
        blocks.append(("text", ls, f, lh, MUTED, 44))
    else:
        f, ls, lh = fit_text(d, official, width, 660, sizes=(84, 76, 68, 60, 56, 52, 48, 44, 40, 36))
        blocks.append(("text", ls, f, lh, INK, 48))
    blocks.append(("rule", None, None, 2, LINE, 40))
    if tags:
        blocks.append(("chips", tags, None, 56, None, 28))
    f, ls, lh = fit_text(d, impact_msg, width, 140, sizes=(38, 34, 30))
    blocks.append(("text", ls, f, lh, MUTED, 0))

    def height(b):
        kind, lines, _, lh, _, gap = b
        return (len(lines) * lh if kind == "text" else lh) + gap

    top, bottom = 170, H - 140
    y = top + max(0, (bottom - top - sum(height(b) for b in blocks)) // 2)
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
    if item.get("expediente") or item.get("key"):
        d.text((M, H - 100), f"Ref. {item.get('expediente') or item.get('key')}", font=footer_font, fill=MUTED)
    page_txt = f"{page}/{total}"
    d.text((W - M - d.textlength(page_txt, font=footer_font), H - 100), page_txt, font=footer_font, fill=MUTED)
    return img


def closing():
    img = Image.new("RGB", (W, H), ACCENT)
    d = ImageDraw.Draw(img)
    brand_bar(d, "white")
    d.line((M, 130, W - M, 130), fill=ACCENT_SOFT, width=2)
    d.text((M, 430), "¿Querés saber más?", font=font(56), fill=ACCENT_SOFT)
    title_font = font(100, True)
    y = draw_lines(d, (M, 530), ["Más info:", "link en la bio"], title_font, 132, "white")
    d.line((M, y + 50, M + 140, y + 50), fill=ACCENT_SOFT, width=6)
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
    slides = [cover(doc, len(items))]
    total = len(items) + 2
    slides += [item_slide(doc, it, simple, i, len(items), i + 1, total)
               for i, (it, simple) in enumerate(zip(items, simple_lines), start=1)]
    slides.append(closing())
    paths = []
    for i, img in enumerate(slides, start=1):
        p = out / f"{prefix}-{i:02d}.{ext}"
        img.save(p, quality=90, optimize=True)  # quality solo aplica a JPEG (Instagram solo acepta JPEG)
        paths.append(p)
    (out / "caption.txt").write_text(build_caption(doc, items), encoding="utf-8")
    return paths


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
