import os
import re
import time
import argparse
from datetime import datetime
from urllib.parse import unquote, urljoin, urlparse, urlunparse
 
import requests
from bs4 import BeautifulSoup
from actas_parser import process_pdf, sha256_bytes
from hf_dataset_store import (DATASET_REPO_ID, PROCESSOR_VERSION, DEBUG, log, dbg, make_doc_id, api,
                              load_processed_json, load_index, save_index, upload_pdf_and_json)
 
SOURCE_URL = os.getenv("SOURCE_URL", "").strip()
 
UA = os.getenv(
    "HTTP_USER_AGENT",
    "Mozilla/5.0 (compatible; ObservatorioActasBot/2.0; +https://github.com/FacundoCicilio/Actualizar-PDFs-Concejo-SMA)"
)
 
session = requests.Session()
session.headers.update({"User-Agent": UA})
 
def must_env(name):
    val = os.getenv(name, "").strip()
    if not val:
        raise SystemExit(f"Falta variable de entorno {name}.")
    return val
 
def canonicalize_url(url):
    # La página publica "N°" y requests devuelve "N%C2%B0": unquote para que sean la misma URL.
    parsed = urlparse((url or "").strip())
    parsed = parsed._replace(fragment="", path=unquote(parsed.path))
    return urlunparse(parsed)
 
def safe_filename_from_url(url, content_disposition=None):
    if content_disposition:
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', content_disposition, re.I)
        if m:
            name = m.group(1).strip().strip('"').strip("'")
            name = os.path.basename(name)
            name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
            if not name.lower().endswith(".pdf"): name += ".pdf"
            return name
    path = urlparse(url).path
    name = os.path.basename(path) or "archivo.pdf"
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    if not name.lower().endswith(".pdf"): name += ".pdf"
    return name
 
def is_probable_document_link(text, href):
    href_l = (href or "").lower()
    text_l = (text or "").lower()
    if ".pdf" in href_l: return True
    href_keywords = ["upload", "uploads", "download", "descarga", "archivo", "file", "media", "wp-content", "document", "docs"]
    text_keywords = ["orden", "día", "dia", "sesión", "sesion", "acta", "extraordinaria", "ordinaria", "documento", "descargar", "descarga", "pdf"]
    if any(k in href_l for k in href_keywords) and any(k in text_l for k in text_keywords): return True
    return False
 
def fetch_pdf_links(page_url):
    log(f"🌐 Leyendo página: {page_url}")
    r = session.get(page_url, timeout=30)
    log(f"📡 Status code página: {r.status_code}")
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    all_links = soup.find_all("a", href=True)
    log(f"🔎 Cantidad total de enlaces <a>: {len(all_links)}")
    candidates = []
    for i, a in enumerate(all_links, start=1):
        raw_href = (a.get("href") or "").strip()
        text = a.get_text(" ", strip=True)
        full = canonicalize_url(urljoin(page_url, raw_href))
        dbg(f"[{i}] Texto: {text} | Href: {raw_href}")
        if not raw_href: continue
        if raw_href.lower().startswith(("javascript:", "mailto:", "tel:")): continue
        if is_probable_document_link(text, raw_href):
            dbg(f"[{i}] ✅ Candidato a documento")
            candidates.append(full)
    seen = set()
    out = []
    for u in candidates:
        if u not in seen:
            seen.add(u)
            out.append(u)
    log(f"📄 Documentos candidatos únicos encontrados: {len(out)}")
    for u in out: log(f"   - {u}")
    return out
 
def download_pdf(url):
    r = session.get(url, timeout=60, allow_redirects=True)
    r.raise_for_status()
    content_type = (r.headers.get("Content-Type") or "").lower()
    content_disposition = r.headers.get("Content-Disposition")
    dbg(f"📨 Final URL: {r.url} | Content-Type: {content_type}")
    looks_like_pdf = ".pdf" in r.url.lower() or "application/pdf" in content_type or b"%PDF" in r.content[:1024]
    if not looks_like_pdf:
        raise ValueError(f"No parece PDF. URL final: {r.url} | Content-Type: {content_type}")
    # r.url tal cual (codificada): de ahí sale el nombre de archivo y por lo tanto el doc_id de lo ya guardado.
    return r.content, r.url, content_disposition
 
def auto_heal_index(repo_id):
    """
    Verifica que cada registro del índice tenga su PDF y JSON.
    Si falta el PDF de algun registro, lo logea para revisión manual.
    Se ejecuta al inicio de cada run.
    """
    log("🔍 Verificando integridad del índice...")
    index = load_index(repo_id)
    try:
        hf = api()
        files = set(hf.list_repo_files(repo_id=repo_id, repo_type="dataset"))
    except Exception as e:
        log(f"⚠️ No pude listar files del dataset: {e}")
        return
 
    huérfanos = []
    for record in index:
        doc_id = record.get("doc_id")
        if not doc_id: continue
        pdf_path = record.get("pdf_path") or f"raw/{doc_id}.pdf"
        if pdf_path not in files:
            huérfanos.append(doc_id)
            log(f"⚠️ Registro huérfano detectado: {doc_id} (PDF {pdf_path} no existe)")
 
    if huérfanos:
        log(f"⚠️ Hay {len(huérfanos)} registros huérfanos. Limpiando del índice…")
        index_clean = [r for r in index if r.get("doc_id") not in huérfanos]
        ok = save_index(repo_id, index_clean, message=f"Auto-heal: remove {len(huérfanos)} orphan records")
        if ok:
            log(f"✅ Índice limpio. Los PDFs huérfanos se van a re-descargar en el próximo ciclo.")
    else:
        log("✅ Índice íntegro.")
 
def main():
    must_env("HF_TOKEN")
 
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=None)
    parser.add_argument("--source-url", type=str, default=None)
    parser.add_argument("--sleep", type=float, default=0.0)
    args = parser.parse_args()
 
    year = args.year or datetime.now().year
    page_url = (args.source_url or SOURCE_URL or f"https://prensacd.cdsma.gob.ar/ordenes-del-dia-{year}/").strip()
 
    log("🚀 Auto fetch iniciado")
    log(f"📦 Repo: {DATASET_REPO_ID}")
    log(f"📰 Fuente: {page_url}")
    log(f"🗓️ Año: {year}")
    log(f"🐞 DEBUG: {DEBUG}")
 
    # AUTO-HEAL: limpiar registros huérfanos antes de empezar
    auto_heal_index(DATASET_REPO_ID)
 
    index = load_index(DATASET_REPO_ID)
    known_doc_ids = {r.get("doc_id") for r in index if r.get("doc_id")}

    log(f"📚 Documentos ya indexados: {len(index)}")
    for r in index[:20]:
        dbg(f"   - {r.get('doc_id')} | {r.get('original_filename')} | {r.get('source_url')}")

    pdf_links = fetch_pdf_links(page_url)

    uploaded_count = 0
    reprocessed_count = 0
    skipped_count = 0
    errors = []

    for url in pdf_links:
        try:
            log(f"⬇️ Descargando: {url}")
            data, raw_url, content_disposition = download_pdf(url)
            source_url = canonicalize_url(raw_url)
            digest = sha256_bytes(data)
            fname = safe_filename_from_url(raw_url, content_disposition)
            doc_id = make_doc_id(data, fname)
            log(f"✅ Descargado OK. Bytes: {len(data)} | doc_id: {doc_id}")

            # Se decide por contenido (doc_id) y versión, no por URL: la misma URL puede traer un PDF corregido
            # y la misma URL puede venir escrita distinto ("N°" / "N%C2%B0").
            if doc_id in known_doc_ids:
                existing_json = load_processed_json(DATASET_REPO_ID, doc_id)
                old_version = (existing_json or {}).get("processor_version") or 0
                if existing_json and old_version >= PROCESSOR_VERSION:
                    log(f"⚠️ Saltado: versión actual, doc_id {doc_id}")
                    skipped_count += 1
                    continue
                log(f"♻️ Reprocesando versión {old_version or 'sin JSON'} → {PROCESSOR_VERSION}")
            else:
                log("🧠 Procesando PDF nuevo...")

            processed = process_pdf(data, fname)
            upload_pdf_and_json(repo_id=DATASET_REPO_ID, doc_id=doc_id, pdf_bytes=data,
                                processed_json=processed, original_filename=fname,
                                source_url=source_url, sha256_digest=digest)
            if doc_id in known_doc_ids:
                reprocessed_count += 1
                log("✅ Reprocesado y actualizado.")
            else:
                known_doc_ids.add(doc_id)
                uploaded_count += 1
                log("✅ Documento subido y indexado correctamente.")

            if args.sleep > 0: time.sleep(args.sleep)

        except Exception as e:
            err = f"❌ Error con {url}: {e}"
            log(err)
            errors.append(err)

    log(f"🏁 Fin. Nuevos: {uploaded_count} | Reprocesados: {reprocessed_count} | Saltados: {skipped_count} | Errores: {len(errors)}")
    if errors:
        log("⚠️ Errores:")
        for e in errors: log(f"   - {e}")
 
if __name__ == "__main__":
    main()
 