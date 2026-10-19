# Buscador de la app (app.py): órdenes del día, notas de Prensa (todas, también las políticas: la exclusión es solo
# para Instagram) y ordenanzas del Boletín Oficial. Sin Streamlit, para poder testearlo.
import difflib
import re
from datetime import datetime

from actas_parser import ensure_items_schema, normalize_name, normalize_text, parse_date_any

FUENTES = ("Órdenes del día", "Notas de Prensa", "Boletín Oficial")


def squash(s):
    """Letras dobles como una sola, en la búsqueda y en el texto: "Salonitti" encuentra "Saloniti" (y al revés).
    Solo letras: "100" no tiene que encontrar "10"."""
    return re.sub(r"([a-zñ])\1+", r"\1", s or "")


def _fecha(iso):
    """"2026-09-04T12:00:00" -> "04/09/2026", como las órdenes del día."""
    try:
        return datetime.fromisoformat(str(iso)[:19]).strftime("%d/%m/%Y")
    except ValueError:
        return iso or ""


def make_snippet(text, query_norm, width=220):
    if not text: return ""
    t_norm = normalize_text(text)
    m = re.search("".join(re.escape(c) + ("+" if c.isalpha() else "") for c in squash(query_norm)), t_norm)
    if not m: return (text[:width] + "…") if len(text) > width else text
    i = m.start()
    start = max(0, i - width // 2)
    end = min(len(text), start + width)
    snippet = text[start:end]
    if start > 0: snippet = "…" + snippet
    if end < len(text): snippet = snippet + "…"
    return snippet


def build_search_records_from_acta(acta, doc_id):
    year = acta.get("session_year")
    acta_num = acta.get("session_number")
    fecha = acta.get("date")
    summary = acta.get("summary_citizen") or ""
    text_preview = acta.get("text_preview") or ""
    ents = acta.get("entities") or {}
    people = ents.get("persons") or []
    orgs = ents.get("orgs") or []
    acta_blob = normalize_text(" ".join([summary, text_preview,
                                          " ".join((acta.get("decretos") or [])[:50]),
                                          " ".join((acta.get("expedientes") or [])[:50])]))
    common = {"year": year, "doc_id": doc_id, "acta_num": acta_num, "session_kind": acta.get("session_kind"),
              "fecha": fecha, "fuente": FUENTES[0], "people": people, "orgs": orgs}
    records = [{**common, "level": "acta", "item_key": None, "item_title": None, "search_blob": acta_blob,
                "raw_for_snippet": {"summary_citizen": summary, "text_preview": text_preview}}]
    items = ensure_items_schema(acta.get("items") or [])
    for it in items:
        title = it.get("title") or ""
        que_dice = it.get("que_dice") or ""
        impacto = it.get("impact_text") or ""
        decreto = it.get("decreto") or ""
        expediente = it.get("expediente") or ""
        blob = normalize_text(" ".join([title, que_dice, impacto, decreto, expediente]))
        records.append({**common, "level": "item", "item_key": str(it.get("key") or ""),
                        "item_title": (it.get("que_dice") or it.get("title") or "")[:220],
                        "search_blob": blob,
                        "raw_for_snippet": {"title": title, "que_dice": que_dice, "impact_text": impacto,
                                            "decreto": decreto, "expediente": expediente}})
    return records


def prensa_records(notes):
    """Una por nota de Prensa ({id: nota} de prensa/notas.json), con su enlace."""
    return [{"level": "prensa", "fuente": FUENTES[1], "id": n["id"], "year": int(n["date"][:4]), "fecha": _fecha(n["date"]),
             "item_title": n["title"], "link": n["link"], "people": [], "orgs": [],
             "search_blob": normalize_text(f"{n['title']} {n['text']}"),
             "raw_for_snippet": {"title": n["title"], "text": n["text"]}}
            for n in (notes or {}).values()]


def boletin_records(store):
    """Una por ordenanza (boletin/boletines.json). Las que nombran a particulares no tienen texto guardado: se
    encuentran solo por su número y se muestran solo con número y fecha."""
    out = []
    for o in (store or {}).get("ordenanzas", {}).values():
        name = f"Ordenanza {o['label']}"
        s = o.get("session")
        sesion = f"Sesión {'Especial ' if s and s['kind'] == 'Especial' else ''}N° {s['number']}" if s else ""
        text = o.get("text") or ""
        m = re.search(r"ART[ÍI]CULO\s*1\s*[º°]?\s*\.?\s*-?\s*(.+?)(?=ART[ÍI]CULO\s*2\b|$)", text, re.I | re.S)
        out.append({"level": "ordenanza", "fuente": FUENTES[2], "year": int((o.get("promulgada") or "0")[:4] or 0),
                    "fecha": _fecha(o.get("promulgada")), "ordenanza": o["label"], "boletin": o["boletin"],
                    "sesion": sesion, "link": o["url"], "particular": bool(o.get("particular")),
                    "item_title": (m.group(1) if m else text)[:220] if text else "", "people": [], "orgs": [],
                    # el número también como se escribe al buscar: "15.751", "15751", "ordenanza 15751"
                    "search_blob": " ".join([normalize_text(f"{name} {text}"), normalize_name(name), f"ordenanza {o['numero']}"]),
                    "raw_for_snippet": {"text": text}})
    return out


def score_record(rec, qn):
    score = 0
    qs = squash(qn)
    people_norm = [squash(normalize_name(p)) for p in (rec.get("people") or [])]
    strict_person = (len(qs.split()) == 1)
    if strict_person:
        if qs in people_norm: score += 220
    else:
        if qs in people_norm: score += 220
        elif any(qs and qs in pn for pn in people_norm): score += 140
    raw = rec.get("raw_for_snippet") or {}
    title = (raw.get("que_dice") or raw.get("title") or rec.get("item_title") or "")
    if title and qs in squash(normalize_text(title)): score += 90
    if qs and qs in squash(rec.get("search_blob") or ""): score += 30
    return score


def _date_key(x):
    dt = parse_date_any(x.get("fecha") or "")
    return dt if dt else datetime.min


def search_records(records, query, year_filter=None, fuentes=FUENTES):
    q = (query or "").strip()
    if not q: return []
    qn = normalize_name(q)
    qs = squash(qn)
    hits = []
    for rec in records:
        if rec.get("fuente", FUENTES[0]) not in fuentes: continue
        if year_filter and rec.get("year"):
            try:
                if int(rec["year"]) != int(year_filter): continue
            except: continue
        people_norm = [squash(normalize_name(p)) for p in (rec.get("people") or [])]
        strict_person = (len(qs.split()) == 1)
        text_match = (qs in squash(rec.get("search_blob") or ""))
        if strict_person: person_match = any(qs == pn for pn in people_norm)
        else: person_match = any(qs and (qs == pn or qs in pn) for pn in people_norm)
        if not (text_match or person_match): continue
        out = dict(rec)
        out["_person_match"] = person_match
        out["_text_match"] = text_match
        out["_score"] = score_record(rec, qn)
        raw = rec.get("raw_for_snippet") or {}
        base_text = (raw.get("que_dice") or raw.get("summary_citizen") or raw.get("text") or raw.get("text_preview")
                     or raw.get("title") or "")
        out["_snippet"] = "" if rec.get("particular") else make_snippet(base_text, normalize_text(qn))
        hits.append(out)
    hits.sort(key=lambda r: (r.get("_score", 0), _date_key(r)), reverse=True)
    return hits


def suggest(records, query):
    """Sin resultados: la palabra indexada más parecida a cada una de la búsqueda ("¿Quisiste decir…?"), o None."""
    words = normalize_name(query).split()
    if not words: return None
    vocab = set()
    for rec in records:
        vocab.update(w for w in normalize_name(rec.get("search_blob") or "").split() if len(w) > 3)
    out = []
    for w in words:
        close = w if w in vocab else next(iter(difflib.get_close_matches(w, vocab, n=1, cutoff=0.8)), None)
        if not close: return None
        out.append(close)
    return " ".join(out) if out != words else None
