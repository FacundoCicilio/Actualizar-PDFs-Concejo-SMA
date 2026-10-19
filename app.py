# app.py — Observatorio de Actas (SMA)
import os
import re
import io
import json
import time
import hashlib
import urllib.request
from datetime import datetime

import streamlit as st
import pandas as pd
from huggingface_hub import hf_hub_download, HfApi

from actas_parser import (
    build_summary_citizen, ensure_items_schema, extract_entities, fmt_date_short, infer_impact,
    infer_num_year_from_filename, normalize_name, normalize_text, parse_date_any, process_pdf, sha256_bytes,
)
from hf_dataset_store import (
    DATASET_REPO_ID,
    PROCESSOR_VERSION,
    safe_int,
    normalize_session_number,
    make_doc_id,
    load_processed_json,
    upload_pdf_and_json,
    load_index,
    download_text_file,
    load_json,
)
from buscador import FUENTES, boletin_records, build_search_records_from_acta, prensa_records, search_records, suggest

APP_TITLE = "Observatorio de Actas"
APP_SUBTITLE = "Concejo Deliberante de San Martín de los Andes"
APP_TAGLINE = "Resúmenes claros y buscables de la actividad legislativa"

st.set_page_config(page_title=f"{APP_TITLE} — SMA", layout="wide")
st.title(f"🏛️ {APP_TITLE}")
st.caption(f"**{APP_SUBTITLE}** · {APP_TAGLINE}")

import streamlit.components.v1 as components
components.html("""
    <script defer src="https://cloud.umami.is/script.js" 
            data-website-id="4190781c-a5dc-47fc-8e7d-6b5f89c39610"></script>
""", height=0)

HF_TOKEN = (st.secrets.get("HF_TOKEN", None) or os.getenv("HF_TOKEN", "")).strip()
hf_api = HfApi(token=HF_TOKEN) if HF_TOKEN else HfApi()

SUBSCRIBERS_PATH = "subscribers/subscribers.jsonl"
VISITS_PATH = "analytics/visits.jsonl"
MANUAL_REFS_PATH = "refs/manual_refs.jsonl"


def _hf_download_text(repo_id, path_in_repo):
    # Para mostrar datos: si la lectura falla, se muestra vacío.
    try:
        return download_text_file(repo_id, path_in_repo)
    except Exception:
        return None


def _hf_upload_text(repo_id, path_in_repo, text, commit_message):
    bio = io.BytesIO(text.encode("utf-8"))
    hf_api.upload_file(repo_id=repo_id, repo_type="dataset", path_in_repo=path_in_repo,
                       path_or_fileobj=bio, commit_message=commit_message)


def _read_jsonl(repo_id, path_in_repo):
    txt = _hf_download_text(repo_id, path_in_repo)
    if not txt:
        return []
    out = []
    for ln in txt.splitlines():
        ln = (ln or "").strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


def _append_jsonl(repo_id, path_in_repo, row, commit_message):
    # Lectura estricta: si falla (red, permisos) se propaga el error en vez de pisar el archivo con una sola fila.
    # shortcut: leer-modificar-escribir, dos escrituras simultáneas pueden perder una; pasar a un archivo por fila si crece el tráfico.
    txt = download_text_file(repo_id, path_in_repo) or ""
    text = (txt.rstrip("\n") + "\n" if txt.strip() else "") + json.dumps(row, ensure_ascii=False) + "\n"
    _hf_upload_text(repo_id, path_in_repo, text, commit_message=commit_message)


def admin_gate():
    admin_pass = st.secrets.get("ADMIN_PASS", "") or os.getenv("ADMIN_PASS", "")
    if not admin_pass:
        return False  # sin contraseña configurada, nadie es admin (antes: todos)
    with st.sidebar:
        st.markdown("### 🔒 Admin")
        entered = st.text_input("Admin password", type="password")
    return entered == admin_pass


def download_raw_pdf(repo_id, doc_id):
    filename = f"raw/{doc_id}.pdf"
    local_path = hf_hub_download(repo_id=repo_id, repo_type="dataset", filename=filename, token=HF_TOKEN or None)
    with open(local_path, "rb") as f:
        return f.read()


def is_stale_cached(cached):
    if not cached: return True
    return (safe_int(cached.get("processor_version")) or 0) < PROCESSOR_VERSION


def normalize_cached_result(cached):
    cached = dict(cached)
    cached["items"] = ensure_items_schema(cached.get("items"))
    sn_norm = normalize_session_number(cached.get("session_number"))
    if sn_norm is not None: cached["session_number"] = str(sn_norm)
    if not cached.get("session_year"):
        dt = parse_date_any(cached.get("date"))
        if dt != datetime.min: cached["session_year"] = dt.year
    if not cached.get("entities"):
        cached["entities"] = extract_entities(cached.get("text_preview", ""))
    if not cached.get("summary_citizen"):
        meta = {k: cached.get(k) for k in ["session_type", "session_number", "session_year", "date", "time"]}
        cached["summary_citizen"] = build_summary_citizen(meta, cached.get("topics") or [], cached.get("items") or [])
    return cached


def doc_sort_key(r):
    year = safe_int(r.get("session_year"))
    sn = normalize_session_number(r.get("session_number"))
    dt = parse_date_any(r.get("date"))
    ts = int(dt.timestamp()) if dt != datetime.min else 0
    return (year or 0, sn or 0, ts)


def doc_label(r):
    stype = (r.get("session_type") or "ORDEN DEL DIA").strip()
    year = safe_int(r.get("session_year"))
    sn = normalize_session_number(r.get("session_number"))
    dt = parse_date_any(r.get("date"))
    date_disp = fmt_date_short(dt, fallback=(r.get("date") or "s/f"))
    num_txt = f"{sn:02d}" if isinstance(sn, int) else "s/n"
    year_txt = str(year) if year else "s/año"
    base = f"{stype} {num_txt} — Año {year_txt} · {date_disp}"
    if num_txt == "s/n" and year_txt == "s/año":
        fname = (r.get("original_filename") or r.get("source_filename") or "").strip()
        if fname: base = f"{stype} · {fname}"
    return base


def enrich_index_record(r):
    rr = dict(r or {})
    doc_id = rr.get("doc_id")
    cached = {}
    if doc_id:
        try: cached = load_processed_json(DATASET_REPO_ID, doc_id) or {}
        except: cached = {}
    for k in ["session_type", "session_number", "session_year", "date", "time"]:
        if not rr.get(k) and cached.get(k): rr[k] = cached.get(k)
    if not rr.get("original_filename"):
        source = cached.get("source") or {}
        if source.get("filename"): rr["original_filename"] = source.get("filename")
    if not rr.get("session_number") or not rr.get("session_year"):
        fnum, fyr = infer_num_year_from_filename(rr.get("original_filename") or "")
        if not rr.get("session_number") and fnum is not None: rr["session_number"] = str(fnum)
        if not rr.get("session_year") and fyr is not None: rr["session_year"] = fyr
    if not rr.get("session_year"):
        dt = parse_date_any(rr.get("date"))
        if dt != datetime.min: rr["session_year"] = dt.year
    if not rr.get("session_type"): rr["session_type"] = "ORDEN DEL DIA"
    return rr


def build_history_options(idx):
    enriched = [enrich_index_record(r) for r in idx]
    enriched_sorted = sorted(enriched, key=doc_sort_key, reverse=True)
    raw_options = [(doc_label(r), r.get("doc_id") or "", r.get("original_filename") or "") for r in enriched_sorted]
    counts = {}
    unique_options = []
    for label, doc_id, original_filename in raw_options:
        counts[label] = counts.get(label, 0) + 1
        unique_label = label
        if counts[label] > 1:
            short_id = doc_id[:8] if doc_id else "sinid"
            unique_label = f"{label} · {short_id}"
        unique_options.append((unique_label, doc_id, original_filename))
    return unique_options


REF_PAT = re.compile(r"\b(\d{4,6}[-]?\d{1,4}\/\d{4}|\d{1,4}\/\d{4}|EE-\d{4}-\d{3,}|[A-Z]{1,4}-\d{4}-\d{3,})\b")


@st.cache_data(show_spinner=False)
def load_manual_refs():
    try: return _read_jsonl(DATASET_REPO_ID, MANUAL_REFS_PATH)
    except: return []


@st.cache_data(show_spinner=False)
def manual_refs_map():
    rows = load_manual_refs()
    m = {}
    for r in rows:
        key = normalize_text(r.get("ref_number") or "")
        if not key: continue
        if key not in m or (r.get("created_at") or "") > (m[key].get("created_at") or ""):
            m[key] = r
    return m


def find_refs_in_text(s):
    if not s: return []
    hits = []
    for m in REF_PAT.finditer(s):
        val = (m.group(1) or "").strip()
        if val and val not in hits: hits.append(val)
    return hits[:6]


def render_refs_enrichment(text):
    refs = find_refs_in_text(text)
    if not refs: return
    m = manual_refs_map()
    for ref in refs:
        k = normalize_text(ref)
        if k in m:
            r = m[k]
            title = (r.get("title") or "").strip()
            summary = (r.get("summary") or "").strip()
            label = f"📌 Referencia {ref}"
            if title: label += f" — {title}"
            st.info(label)
            if summary: st.caption(summary[:450] + ("…" if len(summary) > 450 else ""))


def render_item_detail(df_all, selected_key, file_tag):
    if df_all.empty:
        st.info("No hay ítems para mostrar.")
        return
    df_all = df_all.copy()
    df_all["key"] = df_all["key"].astype(str)
    row_df = df_all[df_all["key"] == str(selected_key)]
    if row_df.empty:
        st.warning("No encontré el ítem seleccionado.")
        return
    row = row_df.iloc[0].to_dict()
    st.markdown(f"#### Ítem {row.get('key')}")
    st.write(f"**Qué dice:** {row.get('que_dice','')}")
    if row.get("impact_text"): st.write(f"**¿En qué te afecta?:** {row.get('impact_text')}")
    meta_line = []
    if row.get("expediente"): meta_line.append(f"**Expediente:** {row.get('expediente')}")
    if row.get("decreto"): meta_line.append(f"**Decreto:** {row.get('decreto')}")
    if row.get("topics"): meta_line.append(f"**Temas:** {', '.join(row.get('topics') or [])}")
    if meta_line: st.caption(" · ".join(meta_line))
    render_refs_enrichment(str(row.get("title", "")))
    st.caption("Texto original (extraído del PDF)")
    st.text_area(f"Original ({file_tag})", value=str(row.get("title", ""))[:6000], height=220, key=f"orig_{file_tag}")
    c1, c2 = st.columns(2)
    with c1:
        st.caption("Copiable (resumen)")
        st.code(row.get("que_dice", ""), language="markdown")
    with c2:
        st.caption("Para WhatsApp (sin markdown)")
        wa = f"Ítem {row.get('key')}\n{row.get('que_dice','')}\n\nImpacto: {row.get('impact_text','')}".strip()
        st.text_area(f"WhatsApp ({file_tag})", value=wa, height=140, key=f"wa_{file_tag}")
    st.download_button("📄 Descargar este ítem (TXT)", data=wa.encode("utf-8"),
                       file_name=f"item_{file_tag}_{row.get('key')}.txt", mime="text/plain", key=f"dl_{file_tag}")


@st.cache_data(show_spinner=False)
def load_index_safe():
    if load_index is None: return []
    try:
        idx = load_index(DATASET_REPO_ID) or []
        return [dict(x or {}) for x in idx]
    except: return []


@st.cache_data(show_spinner=False)
def build_global_search_records():
    idx = load_index_safe()
    records = []
    for r in idx:
        doc_id = r.get("doc_id")
        if not doc_id: continue
        cached = load_processed_json(DATASET_REPO_ID, doc_id) or {}
        if not cached: continue
        cached = normalize_cached_result(cached)
        records.extend(build_search_records_from_acta(cached, doc_id=doc_id))
    # las mismas fuentes que lee el cron de Instagram (prensa.py, boletin.py); si todavía no están, se sigue sin ellas
    for path, build in (("prensa/notas.json", prensa_records), ("boletin/boletines.json", boletin_records)):
        try:
            records.extend(build(load_json(DATASET_REPO_ID, path)))
        except Exception as e:
            print(f"Buscador: no se pudo cargar {path} ({type(e).__name__})")
    return records


def render_global_search_tab():
    st.subheader("🔎 Buscar en todo")
    if load_index is None:
        st.info("Buscador global no disponible.")
        return
    records = build_global_search_records()
    years = sorted({r.get("year") for r in records if r.get("year")}, reverse=True)
    year_opt = st.selectbox("Año", ["Todos"] + [str(y) for y in years], index=0)
    fuentes = st.multiselect("Fuentes", list(FUENTES), default=list(FUENTES))
    with st.form("global_search_form", clear_on_submit=False):
        query = st.text_input("Buscar (texto o nombre de persona)",
                              placeholder="Ej: Pablo / Gomez / tarifa / parque…", key="global_search_q")
        submitted = st.form_submit_button("🔎 Buscar")
    if not submitted:
        st.caption("Tip: en celular, usá el botón **Buscar** (no hace falta tocar Enter).")
        return
    if not query.strip():
        st.warning("Escribí algo para buscar.")
        return
    year_filter = None if year_opt == "Todos" else int(year_opt)
    hits = search_records(records, query=query, year_filter=year_filter, fuentes=fuentes)
    st.caption(f"Resultados: {len(hits)} (mostrando hasta 80)")
    if not hits:
        alt = suggest([r for r in records if r.get("fuente") in fuentes], query)
        if alt: st.info(f"¿Quisiste decir **{alt}**?")
    for i, h in enumerate(hits[:80], start=1):
        doc_id = h.get("doc_id")
        acta_num = h.get("acta_num") or "s/n"
        year = h.get("year") or "s/año"
        fecha = h.get("fecha") or "s/f"
        level = h.get("level")
        item_key = h.get("item_key")
        if level in ("prensa", "ordenanza"):
            if level == "prensa":
                title = f"📰 Nota de Prensa — {fecha} — {h['item_title'][:100]}"
            else:
                title = f"📜 Ordenanza {h['ordenanza']} — Boletín Oficial N° {h['boletin']} — {fecha}"
            with st.expander(title, expanded=False):
                if h.get("particular"):
                    st.caption("Nombra a particulares: se muestra solo el número y la fecha.")
                else:
                    if level == "ordenanza" and h.get("sesion"): st.caption(f"Aprobada en la {h['sesion']}")
                    st.write(h.get("_snippet") or "")
                st.markdown(f"[Ver original]({h['link']}) · Fuente: {h['fuente']}")
            continue
        kind = h.get("session_kind")
        title = f"Acta {'Especial ' if kind == 'Especial' else ''}{acta_num} ({year}) — {fecha}"
        if level == "item" and item_key: title += f" — Ítem {item_key}"
        with st.expander(title, expanded=False):
            if h.get("_person_match"): st.caption("✅ Coincidencia por **persona** detectada")
            elif h.get("_text_match"): st.caption("✅ Coincidencia por **texto**")
            snip = h.get("_snippet") or ""
            if snip: st.write(snip)
            render_refs_enrichment(snip)
            c1, c2 = st.columns([1, 2])
            with c1:
                if st.button("📄 Abrir acta", key=f"open_{doc_id}_{item_key}_{level}_{i}"):
                    st.session_state["open_doc_id"] = doc_id
                    st.session_state["open_item_key"] = item_key
                    st.success("Listo: abrí la acta desde la pestaña Histórico.")
            with c2:
                st.caption(f"doc_id: {doc_id}")


EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def render_subscribe_tab(admin_ok):
    st.subheader("📩 Suscripción por Email")
    st.write("Dejá tu email para recibir aviso cuando aparezca una nueva acta.")
    consent = st.checkbox("Acepto recibir emails del Observatorio (máximo 1 por acta nueva).", value=True)
    email = st.text_input("Tu email", placeholder="tuemail@dominio.com", key="sub_email").strip()
    if st.button("✅ Suscribirme"):
        if not consent: st.warning("Necesitás aceptar el consentimiento.")
        elif not email or not EMAIL_RE.match(email): st.warning("Email inválido.")
        elif not HF_TOKEN: st.error("Falta HF_TOKEN.")
        else:
            email_norm = email.lower().strip()
            email_hash = hashlib.sha256(email_norm.encode("utf-8")).hexdigest()[:16]
            existing = _read_jsonl(DATASET_REPO_ID, SUBSCRIBERS_PATH)
            if any((r.get("email_hash") == email_hash) for r in existing): st.info("Ese email ya está suscripto ✅")
            else:
                row = {"email": email_norm, "email_hash": email_hash,
                       "created_at": datetime.utcnow().isoformat() + "Z", "source": "streamlit"}
                try:
                    _append_jsonl(DATASET_REPO_ID, SUBSCRIBERS_PATH, row, commit_message="Add subscriber")
                    st.success("Suscripción guardada ✅")
                except Exception:
                    st.error("No pude guardar la suscripción. Probá de nuevo en un rato.")
    if admin_ok:
        with st.expander("🔒 Admin — Ver suscriptores"):
            rows = _read_jsonl(DATASET_REPO_ID, SUBSCRIBERS_PATH) if HF_TOKEN else []
            st.caption(f"Total: {len(rows)}")
            if rows:
                df = pd.DataFrame(rows).sort_values("created_at", ascending=False)
                st.dataframe(df, use_container_width=True, hide_index=True)
            else: st.info("No hay suscriptores.")


def _get_or_make_session_id():
    if "anon_session_id" not in st.session_state:
        raw = f"{time.time()}-{os.urandom(16).hex()}"
        st.session_state["anon_session_id"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return str(st.session_state["anon_session_id"])


def log_visit_once_per_session():
    if st.session_state.get("visit_logged"): return
    st.session_state["visit_logged"] = True
    if not HF_TOKEN: return
    row = {"ts": datetime.utcnow().isoformat() + "Z", "date": datetime.utcnow().strftime("%Y-%m-%d"),
           "session": _get_or_make_session_id()}
    try: _append_jsonl(DATASET_REPO_ID, VISITS_PATH, row, commit_message="Log visit")
    except: pass


def render_admin_metrics(admin_ok):
    if not admin_ok: return
    with st.sidebar:
        with st.expander("📈 Métricas (admin)", expanded=False):
            if not HF_TOKEN:
                st.info("Sin HF_TOKEN.")
                return
            rows = _read_jsonl(DATASET_REPO_ID, VISITS_PATH)
            if not rows:
                st.info("Sin visitas registradas todavía.")
                return
            today = datetime.utcnow().strftime("%Y-%m-%d")
            last_7 = set()
            last_30 = set()
            dt_today = datetime.utcnow()
            for i in range(7): last_7.add((dt_today - pd.Timedelta(days=i)).strftime("%Y-%m-%d"))
            for i in range(30): last_30.add((dt_today - pd.Timedelta(days=i)).strftime("%Y-%m-%d"))
            st.metric("Hoy", sum(1 for r in rows if r.get("date") == today))
            st.metric("Últimos 7 días", sum(1 for r in rows if r.get("date") in last_7))
            st.metric("Últimos 30 días", sum(1 for r in rows if r.get("date") in last_30))
            st.metric("Total", len(rows))


def _download_pdf_from_url(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _summarize_pdf_for_manual_ref(pdf_bytes, filename):
    try: r = process_pdf(pdf_bytes, filename=filename)
    except: r = {"summary_citizen": "", "text_preview": "", "topics": [], "items": []}
    text_preview = (r.get("text_preview") or "").strip()
    items = ensure_items_schema(r.get("items") or [])
    if len(text_preview) < 200 and not items:
        return {"auto_summary": "", "auto_topics": [], "auto_items": [], "has_text": False}
    parts = []
    if r.get("topics"): parts.append("Temas: " + ", ".join((r.get("topics") or [])[:6]))
    if items:
        parts.append("Puntos:")
        for it in items[:6]:
            qd = (it.get("que_dice") or "").strip()
            if qd: parts.append(f"- {qd}")
    else:
        lines = [ln.strip() for ln in text_preview.splitlines() if ln.strip()]
        if lines:
            parts.append("Extracto:")
            for ln in lines[:8]: parts.append(f"- {ln[:180]}")
    return {"auto_summary": "\n".join(parts).strip(), "auto_topics": r.get("topics") or [],
            "auto_items": items[:50], "has_text": True}


def render_manual_refs_tab(admin_ok):
    st.subheader("📜 Ordenanzas / Expedientes (manual)")
    st.write("Cargá una referencia para que cuando en un acta aparezca solo un número se entienda de qué trata.")
    st.markdown("👉 **Podés subir un PDF o pegar un link a PDF.**")
    if not admin_ok:
        st.info("Modo público: la carga de referencias está deshabilitada. (Necesitas admin)")
        render_manual_refs_list()
        return
    with st.form("manual_ref_form", clear_on_submit=False):
        ref_number = st.text_input("Número (ordenanza / expediente / referencia)", placeholder="Ej: 05000-590/2012", key="ref_number").strip()
        title = st.text_input("Título corto (opcional)", placeholder="Ej: Tarifas / Concesión / Habilitación…", key="ref_title").strip()
        fuente = st.radio("Fuente", ["Subir PDF", "Link a PDF"], horizontal=True, key="ref_source")
        pdf_bytes = None
        source_url = ""
        if fuente == "Subir PDF":
            up = st.file_uploader("Subí el PDF", type=["pdf"], key="ref_pdf")
            if up is not None: pdf_bytes = up.read()
        else:
            source_url = st.text_input("Pegá el link directo al PDF", placeholder="https://.../archivo.pdf", key="ref_url").strip()
        manual_summary = st.text_area("Resumen manual (si el PDF es escaneado) — opcional",
                                       placeholder="- De qué trata\n- Qué autoriza/prohíbe\n- A quién afecta",
                                       height=150, key="ref_manual_summary").strip()
        consent = st.checkbox("Tengo permiso para compartir esto (o es de fuente pública).", value=True, key="ref_consent")
        submitted = st.form_submit_button("✅ Guardar")
    if submitted:
        if not consent: st.warning("Necesitás confirmar permiso."); return
        if not ref_number: st.warning("Falta el número de referencia."); return
        ref_key = normalize_text(ref_number)
        if fuente == "Link a PDF":
            if not source_url: st.warning("Pegá un link a PDF."); return
            try:
                with st.spinner("Bajando PDF desde el link…"): pdf_bytes = _download_pdf_from_url(source_url)
            except Exception as e: st.error(f"No pude bajar el PDF: {e}"); return
        auto_summary = ""
        has_text = False
        doc_id = None
        if pdf_bytes:
            doc_id = make_doc_id(pdf_bytes, filename=f"{ref_number}.pdf")
            try:
                with st.spinner("Extrayendo texto…"):
                    out = _summarize_pdf_for_manual_ref(pdf_bytes, filename=f"{ref_number}.pdf")
                auto_summary = (out.get("auto_summary") or "").strip()
                has_text = bool(out.get("has_text"))
            except: auto_summary = ""
        final_summary = auto_summary if auto_summary and len(auto_summary) > 80 else manual_summary
        if not final_summary: st.warning("No se pudo generar resumen. Agregá un resumen manual."); return
        row = {"ref_number": ref_number, "ref_key": ref_key, "title": title, "summary": final_summary,
               "source_url": source_url, "doc_id": doc_id, "has_text_extracted": has_text,
               "created_at": datetime.utcnow().isoformat() + "Z"}
        if not HF_TOKEN: st.error("Falta HF_TOKEN."); return
        try:
            _append_jsonl(DATASET_REPO_ID, MANUAL_REFS_PATH, row, commit_message=f"Add manual ref {ref_number}")
            st.success("Guardado ✅")
            load_manual_refs.clear()
            manual_refs_map.clear()
        except Exception as e: st.error(f"No pude guardar: {e}")
    render_manual_refs_list()


def render_manual_refs_list():
    with st.expander("📚 Ver registros cargados (últimos 50)"):
        rows = load_manual_refs()
        rows = sorted(rows, key=lambda r: (r.get("created_at") or ""), reverse=True)[:50]
        if not rows: st.info("Todavía no hay registros.")
        else:
            df = pd.DataFrame(rows)
            cols = [c for c in ["created_at", "ref_number", "title", "doc_id", "source_url", "has_text_extracted", "summary"] if c in df.columns]
            st.dataframe(df[cols], use_container_width=True, hide_index=True)


# ============================================================
# UI principal
# ============================================================
admin_ok = admin_gate()
log_visit_once_per_session()
render_admin_metrics(admin_ok)

tab_hist, tab_search, tab_refs, tab_sub, tab_upload = st.tabs(
    ["🗂️ Histórico", "🔎 Buscar en todo", "📜 Ordenanzas/Expedientes", "📩 Suscripción", "📄 Subir (admin)"]
)

with tab_hist:
    st.subheader("🗂️ Histórico guardado")
    preferred_doc_id = st.session_state.get("open_doc_id")
    if load_index is None:
        st.info("Histórico no disponible.")
    else:
        try: idx = load_index(DATASET_REPO_ID) or []
        except Exception as e: st.error(f"No pude leer el índice: {e}"); idx = []
        if not idx:
            st.info("Todavía no hay documentos guardados.")
        else:
            options = build_history_options(idx)
            label_list = [lab for lab, _, _ in options if lab]
            chosen_index = 0
            if preferred_doc_id:
                for i, (_, did, _) in enumerate(options):
                    if did == preferred_doc_id: chosen_index = i; break
            choice = st.selectbox("Elegí un documento", label_list, index=chosen_index, key="hist_doc_choice")
            chosen = next((x for x in options if x[0] == choice), None)
            if chosen:
                _, chosen_id, chosen_name = chosen
                cached = load_processed_json(DATASET_REPO_ID, chosen_id) or {}
                cached = normalize_cached_result(cached)
                stale = is_stale_cached(cached) or not (cached.get("items") or [])
                do_reprocess = False
                if admin_ok:
                    if stale: st.warning("Documento en versión vieja o sin ítems.")
                    do_reprocess = st.button("♻️ Reprocesar y actualizar histórico")
                if do_reprocess and admin_ok:
                    with st.spinner("Bajando PDF raw del dataset…"):
                        pdf_bytes = download_raw_pdf(DATASET_REPO_ID, chosen_id)
                    with st.spinner("Reprocesando…"):
                        new_result = process_pdf(pdf_bytes, chosen_name or "documento.pdf")
                    with st.spinner("Guardando update…"):
                        upload_pdf_and_json(repo_id=DATASET_REPO_ID, doc_id=chosen_id, pdf_bytes=pdf_bytes,
                                            processed_json=new_result, original_filename=chosen_name or "documento.pdf",
                                            source_url=(cached.get("source_url") or ""), sha256_digest=sha256_bytes(pdf_bytes))
                    st.success("Histórico actualizado ✅")
                    cached = normalize_cached_result(new_result)
                st.subheader("🧍 Resumen")
                st.write(cached.get("summary_citizen", ""))
                st.subheader("📊 Ítems")
                items = cached.get("items") or []
                if items:
                    df = pd.DataFrame(items)
                    df["key"] = df["key"].astype(str)
                    df["qué_dice"] = df["que_dice"]
                    if "impact_text" not in df.columns:
                        df["impact_text"] = df.get("title", "").apply(lambda x: infer_impact(str(x))["impact_text"])
                    df_show = df.copy()
                    cols = [c for c in ["key", "qué_dice", "impact_text", "decreto", "expediente", "topics"] if c in df_show.columns]
                    st.markdown("#### 🔎 Buscar en ítems")
                    with st.form("hist_items_search_form", clear_on_submit=False):
                        q = st.text_input("Buscar por palabra, nombre, expediente, decreto…", "", key="hist_search")
                        topic_values = []
                        if "topics" in df_show.columns:
                            for xs in df_show["topics"].tolist():
                                if isinstance(xs, list): topic_values.extend(xs)
                        topic_values = sorted(set(topic_values))
                        topic_filter = st.multiselect("Filtrar por temas", topic_values, key="hist_topics")
                        run_filter = st.form_submit_button("Filtrar")
                    if run_filter:
                        if q:
                            qlow = q.lower()
                            mask = df_show[cols].astype(str).apply(lambda col: col.str.lower().str.contains(qlow, na=False)).any(axis=1)
                            df_show = df_show[mask]
                        if topic_filter and "topics" in df_show.columns:
                            df_show = df_show[df_show["topics"].apply(lambda xs: any(t in (xs or []) for t in topic_filter))]
                    st.dataframe(df_show[cols], use_container_width=True, hide_index=True)
                    st.download_button("⬇️ Descargar ítems (CSV)", data=df_show[cols].to_csv(index=False).encode("utf-8"),
                                       file_name=f"actas_sma_items_{chosen_id}.csv", mime="text/csv")
                    st.markdown("### 🔍 Ver detalle de un ítem")
                    keys = [str(k) for k in df_show["key"].tolist()]
                    preferred_item_key = st.session_state.get("open_item_key")
                    default_idx = 0
                    if preferred_item_key and preferred_item_key in keys: default_idx = keys.index(preferred_item_key)
                    if keys:
                        selected_key = st.selectbox("Elegí un ítem por número", keys, index=default_idx, key="detail_key_hist")
                        render_item_detail(df, selected_key, file_tag=f"{chosen_id}_hist")
                    else: st.info("No hay ítems en el filtro actual.")
                else:
                    st.info("Este documento no tiene ítems detectados.")

with tab_search:
    render_global_search_tab()

with tab_refs:
    render_manual_refs_tab(admin_ok)

with tab_sub:
    render_subscribe_tab(admin_ok)

with tab_upload:
    if not admin_ok:
        st.info("Modo público: la carga de PDFs está deshabilitada. (Necesitas admin)")
    else:
        colA, colB = st.columns([2, 1])
        with colB:
            st.markdown("### Persistencia")
            persist = st.toggle("Guardar en Dataset", value=True)
            force_reprocess = st.toggle("Forzar reprocesado (ignorar cache)", value=False)
            st.caption(f"Dataset: {DATASET_REPO_ID}")
        with colA:
            uploaded = st.file_uploader("Subí un PDF (acta/orden del día)", type=["pdf"], key="upload_admin")
            if uploaded is None:
                st.info("Subí un PDF para procesar y guardar.")
            else:
                pdf_bytes = uploaded.read()
                doc_id = make_doc_id(pdf_bytes, uploaded.name)
                cached = load_processed_json(DATASET_REPO_ID, doc_id)
                stale = is_stale_cached(cached)
                if cached and (not force_reprocess) and (not stale):
                    result = normalize_cached_result(cached)
                    st.success("Cargando desde Dataset (cache OK). ✅")
                else:
                    with st.spinner("Procesando PDF…"):
                        result = process_pdf(pdf_bytes, uploaded.name)
                    if persist:
                        with st.spinner("Guardando en Dataset…"):
                            upload_pdf_and_json(repo_id=DATASET_REPO_ID, doc_id=doc_id, pdf_bytes=pdf_bytes,
                                                processed_json=result, original_filename=uploaded.name,
                                                source_url="", sha256_digest=sha256_bytes(pdf_bytes))
                        st.success("Dataset actualizado ✅")
                result = normalize_cached_result(result)
                st.subheader("🧍 Resumen")
                st.write(result.get("summary_citizen", ""))
                st.subheader("📊 Proyectos / Ítems")
                items = result.get("items") or []
                if items:
                    df = pd.DataFrame(items)
                    df["key"] = df["key"].astype(str)
                    df["qué_dice"] = df["que_dice"]
                    show_cols = [c for c in ["key", "qué_dice", "impact_text", "decreto", "expediente", "topics"] if c in df.columns]
                    st.dataframe(df[show_cols], use_container_width=True, hide_index=True)
                with st.expander("🧩 Texto extraído (preview)"):
                    st.text(result.get("text_preview", ""))
