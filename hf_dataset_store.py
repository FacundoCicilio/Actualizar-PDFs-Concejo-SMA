import io
import json
import os
import re
import time
import hashlib
from datetime import datetime, timezone

from huggingface_hub import HfApi, hf_hub_download
from huggingface_hub.utils import EntryNotFoundError, LocalEntryNotFoundError

DATASET_REPO_ID = os.getenv("HF_REPO_ID", "").strip() or "Facucici/actas-sma-data"
INDEX_PATH = "index/index.json"
PROCESSOR_VERSION = 28  # única versión para app y cron: subirla hace que el cron reprocese lo anterior
DEBUG = os.getenv("DEBUG", "1").strip().lower() in {"1", "true", "yes", "y"}

# Leídos al llamar, no al importar: en Streamlit los secrets pasan a env recién tras el primer acceso a st.secrets.
def _token(): return os.getenv("HF_TOKEN", "").strip()

def log(msg): print(msg, flush=True)
def dbg(msg):
    if DEBUG: print(msg, flush=True)

def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()

def safe_int(v):
    try:
        if v is None: return None
        return int(str(v).strip())
    except: return None

def normalize_session_number(s):
    if not s: return None
    m = re.search(r"\b(\d{1,3})\b", str(s))
    return int(m.group(1)) if m else None

def make_doc_id(pdf_bytes, filename):
    h = hashlib.sha256()
    h.update(pdf_bytes)
    h.update((filename or "").encode("utf-8", errors="ignore"))
    return h.hexdigest()[:16]

def api():
    if not _token(): raise RuntimeError("Falta HF_TOKEN.")
    return HfApi(token=_token())

def download_text_file(repo_id, path):
    """None solo si el archivo no existe. Cualquier otro error se propaga:
    tratarlo como 'vacío' hace que el siguiente save pise los datos."""
    try:
        local = hf_hub_download(repo_id=repo_id, repo_type="dataset", filename=path, token=_token() or None)
    except LocalEntryNotFoundError:
        raise  # sin red: es subclase de EntryNotFoundError, pero NO significa que el archivo no exista
    except EntryNotFoundError:
        return None
    with open(local, "r", encoding="utf-8") as f: return f.read()

def load_processed_json(repo_id, doc_id):
    raw = download_text_file(repo_id, f"processed/{doc_id}.json")
    if not raw: return None
    try: return json.loads(raw)
    except: return None

# ============================================================
# UPLOAD ROBUSTO CON RETRY Y VERIFICACION
# ============================================================

def _upload_with_retry(hf, path_or_fileobj, path_in_repo, repo_id, repo_type, commit_message, max_retries=3):
    """Sube un archivo con reintentos. Devuelve True si funcionó."""
    for attempt in range(max_retries):
        try:
            if hasattr(path_or_fileobj, 'seek'):
                path_or_fileobj.seek(0)
            hf.upload_file(
                path_or_fileobj=path_or_fileobj,
                path_in_repo=path_in_repo,
                repo_id=repo_id,
                repo_type=repo_type,
                commit_message=commit_message,
            )
            return True
        except Exception as e:
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                log(f"⚠️ Intento {attempt+1} falló al subir {path_in_repo}: {e}. Reintentando en {wait}s…")
                time.sleep(wait)
            else:
                log(f"❌ Falló al subir {path_in_repo} tras {max_retries} intentos: {e}")
    return False


def _file_exists_in_dataset(repo_id, path):
    """Verifica que un archivo realmente existe en el dataset."""
    try:
        hf = api()
        files = hf.list_repo_files(repo_id=repo_id, repo_type="dataset")
        return path in files
    except Exception as e:
        log(f"⚠️ No pude verificar existencia de {path}: {e}")
        return False


def rebuild_index_from_processed(repo_id):
    log("🛠 Reconstruyendo index desde processed/*.json ...")
    out = []
    files = api().list_repo_files(repo_id=repo_id, repo_type="dataset")
    processed_files = [p for p in files if p.startswith("processed/") and p.endswith(".json")]
    dbg(f"Processed encontrados: {len(processed_files)}")
    for p in processed_files:
        m = re.match(r"processed/([a-f0-9]{16})\.json$", p)
        if not m: continue
        doc_id = m.group(1)
        payload = load_processed_json(repo_id, doc_id) or {}
        original_filename = payload.get("source", {}).get("filename") or f"{doc_id}.pdf"
        record = {
            "doc_id": doc_id, "original_filename": original_filename,
            "pdf_path": f"raw/{doc_id}.pdf", "json_path": p,
            "created_at": payload.get("source", {}).get("processed_at") or utc_now_iso(),
            "session_type": payload.get("session_type"), "session_number": payload.get("session_number"),
            "session_year": payload.get("session_year"), "date": payload.get("date"),
            "topics": payload.get("topics", []), "source_url": payload.get("source_url"),
            "sha256": payload.get("sha256"),
        }
        out.append(record)
    out.sort(key=lambda r: (safe_int(r.get("session_year")) or 0, normalize_session_number(r.get("session_number")) or 0, r.get("created_at") or ""), reverse=True)
    log(f"✅ Index reconstruido con {len(out)} documentos.")
    return out

def load_index(repo_id):
    raw = download_text_file(repo_id, INDEX_PATH)
    if not raw: return []
    try: data = json.loads(raw)
    except: return rebuild_index_from_processed(repo_id)
    if isinstance(data, list): return data
    if isinstance(data, dict): return rebuild_index_from_processed(repo_id)
    return []

def save_index(repo_id, index, message="Update index"):
    content = json.dumps(index, ensure_ascii=False, indent=2).encode("utf-8")
    hf = api()
    return _upload_with_retry(
        hf=hf,
        path_or_fileobj=io.BytesIO(content),
        path_in_repo=INDEX_PATH,
        repo_id=repo_id,
        repo_type="dataset",
        commit_message=message,
    )

def upload_pdf_and_json(repo_id, doc_id, pdf_bytes, processed_json, original_filename, source_url, sha256_digest):
    """
    Sube PDF + JSON + actualiza índice con verificación.
    Si el PDF falla, NO actualiza el índice (evita registros huérfanos).
    """
    hf = api()
    pdf_path = f"raw/{doc_id}.pdf"
    json_path = f"processed/{doc_id}.json"

    # 1) Subir PDF con retry
    pdf_ok = _upload_with_retry(
        hf=hf,
        path_or_fileobj=io.BytesIO(pdf_bytes),
        path_in_repo=pdf_path,
        repo_id=repo_id,
        repo_type="dataset",
        commit_message=f"Add PDF {doc_id}",
    )
    if not pdf_ok:
        raise RuntimeError(f"No se pudo subir el PDF {pdf_path} tras varios intentos.")

    # 2) Verificar que el PDF realmente está en el dataset
    if not _file_exists_in_dataset(repo_id, pdf_path):
        raise RuntimeError(f"El PDF {pdf_path} no aparece en el dataset tras el upload. Abortando.")
    log(f"✅ PDF verificado en dataset: {pdf_path}")

    # 3) Subir JSON con retry
    processed_json = dict(processed_json or {})
    processed_json["source_url"] = source_url
    processed_json["sha256"] = sha256_digest
    json_bytes = json.dumps(processed_json, ensure_ascii=False, indent=2).encode("utf-8")

    json_ok = _upload_with_retry(
        hf=hf,
        path_or_fileobj=io.BytesIO(json_bytes),
        path_in_repo=json_path,
        repo_id=repo_id,
        repo_type="dataset",
        commit_message=f"Add processed {doc_id}",
    )
    if not json_ok:
        raise RuntimeError(f"PDF subido OK pero NO se pudo subir el JSON {json_path}. NO actualizo el índice.")
    log(f"✅ JSON subido: {json_path}")

    # 4) Actualizar índice (solo si todo lo anterior funcionó)
    index = load_index(repo_id)
    record = {
        "doc_id": doc_id, "original_filename": original_filename,
        "pdf_path": pdf_path, "json_path": json_path,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "session_type": processed_json.get("session_type"), "session_number": processed_json.get("session_number"),
        "session_year": processed_json.get("session_year"), "date": processed_json.get("date"),
        "topics": processed_json.get("topics", []), "source_url": source_url, "sha256": sha256_digest,
    }
    index = [r for r in index if r.get("doc_id") != doc_id]
    index.insert(0, record)
    index_ok = save_index(repo_id, index, message=f"Index {doc_id}")
    if not index_ok:
        log(f"⚠️ PDF y JSON subidos OK pero falló el update del índice para {doc_id}. Va a quedar inconsistente.")
    else:
        log(f"✅ Índice actualizado con {doc_id}")

    return {"pdf_path": pdf_path, "json_path": json_path}
