# Anexos de ordenanzas desde el Digesto del Concejo (https://digesto.cdsma.gob.ar, el iframe de
# prensacd.cdsma.gob.ar/digesto). El Boletín Oficial a veces publica el articulado sin el Anexo (p. ej. la 15.816/26, el
# cronograma del 79% del agua): por cada ordenanza del Boletín cuyo articulado cita un Anexo, se baja el PDF del Digesto,
# se guarda el texto del Anexo y si el Boletín lo traía o no. Corre en el cron después de Instagram (que actualiza el
# Boletín). Solo ordenanzas con texto público: las de particulares no se buscan. "capturado": cuándo se bajó; el
# newsletter incluye el Anexo esa semana aunque el Boletín sea anterior. En la primera corrida (store vacío) es la fecha
# de publicación del Boletín, para que el newsletter no meta de golpe los Anexos de todo el histórico.
#   python digesto.py
import hashlib
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

import boletin
from hf_dataset_store import DATASET_REPO_ID, load_json, save_json

BASE = "https://digesto.cdsma.gob.ar"
ARG = timezone(timedelta(hours=-3))
STORE_PATH = "digesto/anexos.json"
CITA_PAT = re.compile(r"\bAnexo\s+[IVX\d]+\b", re.I)  # "de acuerdo al esquema que como Anexo I forma parte"
ANEXO_HEAD_PAT = re.compile(r"^\s*ANEXO\b", re.M)  # título en mayúsculas: "Anexo I de la Ordenanza…" es una cita


def anexo_en_boletin(text, numero):
    """El Boletín trae un ANEXO entre el encabezado de la ordenanza y el de la siguiente (o la próxima sesión)."""
    heads = sorted([*boletin.ORD_HEAD_PAT.finditer(text), *boletin.SESSION_HEAD_PAT.finditer(text)], key=lambda m: m.start())
    for i, m in enumerate(heads):
        if m.re is boletin.ORD_HEAD_PAT and int(m.group(1) + m.group(2)) == numero:
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            return bool(ANEXO_HEAD_PAT.search(text, m.end(), end))
    return False


def anexo_text(text):
    """Desde el primer título ANEXO hasta el final, o "" si el PDF no trae texto de Anexo (p. ej. escaneado)."""
    m = ANEXO_HEAD_PAT.search(text)
    return boletin.scrub(text[m.start():]) if m else ""


def capturado(primera, last_modified, now=None):
    """ISO de "capturado": en la primera corrida, cuándo el Boletín subió el PDF (Last-Modified); si no, ahora."""
    if primera and last_modified:
        try:
            return parsedate_to_datetime(last_modified).astimezone(ARG).isoformat(timespec="seconds")
        except (TypeError, ValueError):
            pass
    return (now or datetime.now(ARG)).isoformat(timespec="seconds")


def session():
    """Laravel: la búsqueda es un POST con el token CSRF de la portada y su cookie."""
    s = requests.Session()
    r = s.get(BASE + "/", timeout=30)
    r.raise_for_status()
    token = re.search(r'name="csrf-token" content="([^"]+)"', r.text)
    if not token:
        raise RuntimeError("El Digesto cambió: no encontré el token CSRF en la portada.")
    s.headers.update({"X-CSRF-TOKEN": token.group(1), "X-Requested-With": "XMLHttpRequest"})
    return s


def buscar(s, numero):
    """{id, year, number, date, summary} de la ordenanza en el Digesto, o None si todavía no la cargaron."""
    r = s.post(BASE + "/get-statutes", timeout=30, json={"order": "desc", "show": "10", "keyword": str(numero), "type": "",
                                                          "scope": "", "category": None, "tags": [], "page": 1})
    r.raise_for_status()
    return next((st for st in r.json()["statutes"] if int(st["number"]) == numero), None)


def run():
    ords = (load_json(DATASET_REPO_ID, boletin.STORE_PATH) or {}).get("ordenanzas", {})
    store = load_json(DATASET_REPO_ID, STORE_PATH) or {}
    todo = [o for k, o in ords.items() if "text" in o and CITA_PAT.search(o["text"]) and k not in store]
    if not todo:
        print("Digesto: no hay ordenanzas nuevas que citen un Anexo.")
        return 0
    s, boletines, changed, primera = session(), {}, [], not store
    for o in sorted(todo, key=lambda o: o["numero"]):
        st = buscar(s, o["numero"])
        if not st:
            print(f"Ordenanza {o['label']}: todavía no está en el Digesto; se reintenta mañana.")
            continue
        url = f"{BASE}/normas/{st['id']}.pdf"
        pdf = s.get(url, timeout=60)
        pdf.raise_for_status()
        if o["url"] not in boletines:
            b = requests.get(o["url"], timeout=60)
            b.raise_for_status()
            boletines[o["url"]] = (boletin.pdf_text(b.content), b.headers.get("Last-Modified"))
        texto_boletin, publicado = boletines[o["url"]]
        rec = {"label": o["label"], "pdf": url, "sha256": hashlib.sha256(pdf.content).hexdigest(),
               "anexo": anexo_text(boletin.pdf_text(pdf.content)), "boletin": o["boletin"], "boletin_url": o["url"],
               "anexo_en_boletin": anexo_en_boletin(texto_boletin, o["numero"]),
               "capturado": capturado(primera, publicado)}  # el newsletter lo cuenta esa semana
        store[str(o["numero"])] = rec
        changed.append(o["label"])
        falta = "" if rec["anexo_en_boletin"] else " (no está en el Boletín Oficial)"
        print(f"Ordenanza {o['label']}: Anexo {'capturado' if rec['anexo'] else 'sin texto en el PDF (¿escaneado?)'}{falta}: {url}")
    if changed:
        save_json(DATASET_REPO_ID, STORE_PATH, store, f"Digesto: Anexos de {', '.join(changed)}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
