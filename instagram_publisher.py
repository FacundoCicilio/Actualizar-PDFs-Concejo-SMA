# Publica en Instagram cada orden del día nueva y, desde las notas de Prensa (prensa.py), el resumen "Qué se aprobó"
# de cada sesión (7 días después de la sesión) y un "Destacado" por cada nota de Bolsillo, Servicios u Obras; desde el
# Boletín Oficial (boletin.py), un "Destacado" por cada ordenanza de esos temas que Prensa no cubrió.
# Corre en el cron, después de auto_fetch.py.
#   python instagram_publisher.py                          # corrida normal
#   python instagram_publisher.py --prueba 23 [--anio 2026] [--tipo que-se-aprobo]  # Issue de vista previa: no publica
#                                                                                   # ni toca el estado
#   python instagram_publisher.py --puesta [--excluir 1782,1601]  # vista previa de la "puesta al día" (Issue)
#   python instagram_publisher.py --puesta --publicar sí         # publica esa vista previa, tal cual se revisó
# Período de prueba, por tipo de post: cada post nuevo abre un Issue; si no tiene la etiqueta "no-publicar", la corrida
# diaria siguiente lo publica. Después de 3 publicaciones aprobadas así de ese tipo, ese tipo pasa a publicar directo.
# Estado y token viven en el dataset privado de HF. Nunca se imprime ni se reporta un token.
import argparse
import base64
import sys
import tempfile
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

import boletin
import instagram_carousel as ic
import prensa
from hf_dataset_store import DATASET_REPO_ID, load_index, load_json, load_processed_json, save_json

STATE_PATH = "instagram/state.json"
TOKEN_PATH = "instagram/token.json"  # aparte del estado: el estado puede terminar citado en un Issue, el token nunca
MEDIA_BRANCH = "instagram-media"     # imágenes públicas para Instagram; un push acá no dispara el deploy del Space
GRAPH = "https://graph.instagram.com/v25.0"
IG_USERNAME = "actas.sma"  # cuenta en la que se publica: la prueba verifica que el token sea de esta cuenta
REVIEW_APPROVALS = 3
REVIEW_WAIT = timedelta(hours=12)    # "la próxima corrida diaria": re-ejecutar el mismo día no publica
REFRESH_BEFORE = timedelta(days=15)  # el token dura 60 días; con el cron diario hay ~15 intentos de renovación
CRON_TIME_UTC = (10, 17)             # hora y minuto: igual que el schedule de auto-fetch.yml
APROBADO_WAIT = timedelta(days=7)    # "Qué se aprobó" espera a que lleguen las notas de la sesión
TIPOS = {"orden-del-dia": "od", "que-se-aprobo": "aprobado", "destacado": "destacado", "boletin": "boletin"}
KIND_NAMES = {"od": "las órdenes del día nuevas", "aprobado": "los resúmenes 'Qué se aprobó' nuevos",
              "destacado": "los destacados nuevos", "boletin": "los destacados del Boletín Oficial nuevos"}
LABELS = {"instagram-vista-previa": "1F4E79", "no-publicar": "B60205", "instagram-error": "D93F0B", "instagram-prueba": "C5DEF5",
          "instagram-puesta-al-dia": "4B2E6F"}
PUESTA_DEFAULT_START = date(2026, 6, 1)  # si no se puede saber la fecha del último post
ERROR_TITLES = {
    "token": "Instagram: problema con el token",
    "publicacion": "Instagram: falló la publicación",
    "anthropic": "Instagram: falló la API de Anthropic",
    "prensa": "Instagram: no se pudieron leer las notas de Prensa",
    "boletin": "Instagram: no se pudo leer el Boletín Oficial",
    "otro": "Instagram: error inesperado",
}


class PublishError(Exception):
    """Error con un mensaje apto para un Issue: nunca incluye tokens."""


class AnthropicFailure(PublishError):
    """Sin la API de Anthropic no hay texto para las placas de Prensa: no se publica y se reintenta mañana."""


class IGError(PublishError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat(timespec="seconds")


# ------------------------------------------------------------------ GitHub (Issues e imágenes)

def repo():
    return ic.env_value("GITHUB_REPOSITORY") or "FacundoCicilio/Actualizar-PDFs-Concejo-SMA"


def gh(method, path, ok=(200, 201), **kw):
    token = ic.env_value("GITHUB_TOKEN")
    if not token:
        raise PublishError("Falta GITHUB_TOKEN.")
    try:
        r = requests.request(method, f"https://api.github.com/repos/{repo()}{path}", timeout=30,
                             headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}, **kw)
    except requests.RequestException as e:
        raise PublishError(f"GitHub no respondió ({type(e).__name__}).") from None  # el texto de la excepción no se usa
    if r.status_code not in ok:
        raise PublishError(f"GitHub {method} {path}: HTTP {r.status_code} {r.text[:200]}")
    return r.json() if r.content else {}


def ensure_setup():
    """Etiquetas y rama de imágenes; se crean la primera vez."""
    for name, color in LABELS.items():
        gh("POST", "/labels", ok=(201, 422), json={"name": name, "color": color})  # 422: ya existe
    if "object" not in gh("GET", f"/git/ref/heads/{MEDIA_BRANCH}", ok=(200, 404)):
        sha = gh("GET", "/git/ref/heads/main")["object"]["sha"]
        gh("POST", "/git/refs", json={"ref": f"refs/heads/{MEDIA_BRANCH}", "sha": sha})


def upload_images(paths, folder):
    """Sube los JPEG a la rama de imágenes y espera a que las URLs públicas respondan."""
    urls = []
    for p in paths:
        gh("PUT", f"/contents/{folder}/{p.name}", json={
            "message": f"Instagram: {folder}/{p.name}", "branch": MEDIA_BRANCH,
            "content": base64.b64encode(p.read_bytes()).decode("ascii")})
        urls.append(f"https://raw.githubusercontent.com/{repo()}/{MEDIA_BRANCH}/{folder}/{p.name}")
    for u in urls:
        for _ in range(12):
            try:
                if requests.head(u, timeout=15).status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(10)
        else:
            raise PublishError(f"La imagen {u} no quedó accesible públicamente.")
    return urls


def create_issue(title, body, labels):
    issue = gh("POST", "/issues", json={"title": title, "body": body, "labels": labels})
    return issue["number"], issue["html_url"]


def comment(number, body):
    gh("POST", f"/issues/{number}/comments", json={"body": body})


def close_issue(number):
    gh("PATCH", f"/issues/{number}", json={"state": "closed"})


def report_error(kind, detail):
    """Abre un Issue con el problema, o comenta en el que ya está abierto por lo mismo (no uno nuevo por día)."""
    title = ERROR_TITLES[kind]
    body = f"{detail}\n\nCorrida: {iso(now())}. El cron lo vuelve a intentar en la próxima corrida."
    print(f"ERROR ({kind}): {detail}")
    try:
        open_issues = gh("GET", "/issues", params={"labels": "instagram-error", "state": "open", "per_page": 100})
        same = next((i for i in open_issues if i["title"] == title), None)
        if same:
            comment(same["number"], body)
        else:
            create_issue(title, body, ["instagram-error"])
    except PublishError as e:
        print(f"Tampoco se pudo abrir el Issue: {e}")


# ------------------------------------------------------------------ Instagram

def ig(method, path, token, **params):
    try:
        # token en el header: así nunca queda en una URL que pueda aparecer en un mensaje de error
        r = requests.request(method, f"{GRAPH}/{path}", headers={"Authorization": f"Bearer {token}"}, timeout=60,
                             **({"params": params} if method == "GET" else {"data": params}))
    except requests.RequestException as e:
        raise PublishError(f"Instagram no respondió ({type(e).__name__}).") from None
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code >= 400 or "error" in body:
        err = body.get("error") or {}
        raise IGError(f"Instagram: {err.get('message') or f'HTTP {r.status_code}'}", err.get("code"))
    return body


def wait_finished(container, token):
    """Espera a que un contenedor esté listo (la doc recomienda 1 consulta por minuto, máximo 5 minutos)."""
    for _ in range(6):
        status = ig("GET", container, token, fields="status_code").get("status_code")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise PublishError(f"Instagram rechazó el contenedor {container} (estado {status}).")
        time.sleep(60)
    raise PublishError(f"El contenedor {container} no terminó de procesarse en 5 minutos.")


def build_carousel(token, user_id, image_urls, caption):
    """Crea y verifica todos los contenedores. Si falla cualquiera, se lanza el error y no se publica nada."""
    if not 2 <= len(image_urls) <= 10:
        raise PublishError(f"Un carrusel necesita entre 2 y 10 imágenes; hay {len(image_urls)}.")
    children = []
    for u in image_urls:
        cid = ig("POST", f"{user_id}/media", token, image_url=u, is_carousel_item="true")["id"]
        wait_finished(cid, token)
        children.append(cid)
    container = ig("POST", f"{user_id}/media", token, media_type="CAROUSEL", children=",".join(children), caption=caption)["id"]
    wait_finished(container, token)
    return container


def find_recent_post(token, user_id, caption):
    """Post ya publicado con este texto (por si una corrida anterior publicó y no llegó a guardarlo)."""
    first_line = caption.splitlines()[0]
    media = ig("GET", f"{user_id}/media", token, fields="id,caption,permalink", limit=10).get("data", [])
    return next((m for m in media if (m.get("caption") or "").splitlines()[:1] == [first_line]), None)


# ------------------------------------------------------------------ token

_token = None


def token_works(token):
    try:
        ig("GET", "me", token, fields="user_id")
        return True
    except IGError as e:
        if e.code == 190:  # OAuthException: token vencido, revocado o inválido
            return False
        raise


def refresh(token):
    """Renueva por 60 días. El token tiene que tener más de 24 h y no estar vencido."""
    try:
        r = requests.get("https://graph.instagram.com/refresh_access_token", timeout=30,
                         params={"grant_type": "ig_refresh_token", "access_token": token})
    except requests.RequestException as e:
        raise PublishError(f"Instagram no respondió al renovar el token ({type(e).__name__}).") from None
    body = r.json() if r.content else {}
    if r.status_code >= 400 or "access_token" not in body:
        raise PublishError(f"No se pudo renovar el token: {(body.get('error') or {}).get('message') or f'HTTP {r.status_code}'}")
    return body["access_token"], now() + timedelta(seconds=int(body["expires_in"]))


def has_token():
    return bool(ic.env_value("IG_ACCESS_TOKEN") or (load_json(DATASET_REPO_ID, TOKEN_PATH) or {}).get("token"))


def get_token():
    """Token que funciona, renovado si vence en menos de 15 días. Primero el guardado en el dataset (puede ser más
    nuevo que el secret); si no anda, el secret IG_ACCESS_TOKEN (por si cargaste uno nuevo)."""
    global _token
    if _token:
        return _token
    stored = load_json(DATASET_REPO_ID, TOKEN_PATH) or {}
    for token in dict.fromkeys(t for t in (stored.get("token"), ic.env_value("IG_ACCESS_TOKEN")) if t):
        if not token_works(token):
            continue
        is_stored = token == stored.get("token")
        expires = datetime.fromisoformat(stored["expires_at"]) if is_stored and stored.get("expires_at") else None
        if expires is None or expires - now() < REFRESH_BEFORE:
            try:
                token, expires = refresh(token)
            except PublishError as e:
                if expires is not None:  # se acerca el vencimiento y no se renueva: avisar, pero el token todavía sirve
                    report_error("token", f"{e} Vence el {expires:%d/%m/%Y}.")
                else:                    # token recién cargado (menos de 24 h): se renueva más adelante
                    expires = now() + timedelta(days=55)
        if not is_stored or token != stored.get("token") or iso(expires) != stored.get("expires_at"):
            save_json(DATASET_REPO_ID, TOKEN_PATH, {"token": token, "expires_at": iso(expires), "saved_at": iso(now())},
                      "Instagram: token")
        _token = token
        return token
    raise PublishError("Ningún token de Instagram funciona (falta, venció o fue revocado). Generá uno nuevo de larga "
                       "duración y cargalo en el secret IG_ACCESS_TOKEN de GitHub; el cron lo toma en la próxima corrida.")


def user_id(token):
    return ic.env_value("IG_USER_ID") or ig("GET", "me", token, fields="user_id")["user_id"]


# ------------------------------------------------------------------ carrusel y Issues

def session_key(rec):
    return f"{rec['session_year']}-{rec['session_number']}" if rec.get("session_year") and rec.get("session_number") else None


def prepare(doc_id, folder):
    """Genera el carrusel en JPEG y sube las imágenes. Devuelve (doc, urls, caption) o None sin ítems de alcance general."""
    doc = load_processed_json(DATASET_REPO_ID, doc_id)
    if not doc:
        raise PublishError(f"No encontré processed/{doc_id}.json en el dataset.")
    items = ic.select_items(doc)
    if not items:
        return None
    lines, error = ic.criollo_lines(items)
    if error:
        report_error("anthropic", f"{error} La orden del día {ic.session_title(doc)} sale sin 'En criollo' en ninguna placa.")
    with tempfile.TemporaryDirectory() as tmp:
        paths = ic.render_carousel(doc, tmp, prefix="placa", simple_lines=lines, ext="jpg")
        caption = (Path(tmp) / "caption.txt").read_text(encoding="utf-8")
        urls = upload_images(paths, folder)
    return doc, urls, caption


def prepare_aprobado(s, folder):
    """Carrusel "Qué se aprobó" de una sesión. Devuelve (título, urls, caption) o None si ninguna nota dio un resumen."""
    sdoc = prensa.session_doc(s)
    label = f"Qué se aprobó · {ic.session_title(sdoc)} ({ic.date_long(sdoc['date'])})"
    notes = prensa.rank(s["notes"])[:ic.MAX_ITEMS]
    lines, error = ic.note_lines([prensa.note_text(n) for n in notes])
    if error:
        raise AnthropicFailure(f"{error} No se generó '{label}'; se reintenta en la próxima corrida.")
    picked = [{**n, "line": line} for n, line in zip(notes, lines) if line]
    if not picked:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        paths = ic.render_aprobado(sdoc, picked, tmp, ext="jpg")
        caption = (Path(tmp) / "caption.txt").read_text(encoding="utf-8")
        urls = upload_images(paths, folder)
    return label, urls, caption


def prepare_destacado(s, note, folder):
    """Dos placas: qué cambia, a quién afecta y desde cuándo. Devuelve (título, urls, caption, vigente hasta) o None."""
    sdoc = prensa.session_doc(s)
    ordenanza = note.get("ordenanza")  # del Boletín Oficial: articulado en lugar de nota de Prensa
    fields, error = ic.destacado(prensa.note_text(note), ordenanza=bool(ordenanza))
    if error:
        raise AnthropicFailure(f"{error} No se generó el destacado de la nota {note['link']}; se reintenta en la próxima corrida.")
    if not fields:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        paths = ic.render_destacado(sdoc, fields, note["impact_tags"], tmp, ext="jpg", ordenanza=ordenanza)
        caption = (Path(tmp) / "caption.txt").read_text(encoding="utf-8")
        if ordenanza and boletin.leaks(caption):
            raise PublishError(f"El destacado de la Ordenanza {ordenanza['label']} nombraba a un particular: no se publica.")
        urls = upload_images(paths, folder)
    end = prensa.vigencia_hasta(note)
    return f"Destacado · {fields['titular']}", urls, caption, end and end.isoformat()


def destacables(s, today):
    """Notas de la sesión que van como destacado: no las que informan un plazo que ya terminó."""
    return [n for n in s["notes"] if prensa.is_destacado(n) and not prensa.vencida(n, today)]


def next_cron_run():
    t = now() + REVIEW_WAIT
    run = t.replace(hour=CRON_TIME_UTC[0], minute=CRON_TIME_UTC[1], second=0, microsecond=0)
    return run if run > t else run + timedelta(days=1)


def od_label(doc):
    return f"{ic.session_title(doc)} ({ic.date_long(doc.get('date'))})"


def preview_title(label, prueba=False):
    return f"{'PRUEBA ' if prueba else 'Vista previa '}Instagram: {label}"


def preview_body(urls, caption, deadline=None, approved=None, head=None):
    if head is None and deadline is None:
        head = ["**PRUEBA: esta vista previa no se publica.**"]
    elif head is None:
        when = f"{deadline:%d/%m} a las {deadline:%H:%M} UTC ({(deadline - timedelta(hours=3)):%H:%M} en Argentina)"
        head = [f"Se publica en Instagram en la próxima corrida del cron: **{when}** (puede demorar unos minutos).", "",
                "**Si no querés que se publique, poné la etiqueta `no-publicar`** (o cerrá este Issue) antes de esa hora."]
    body = head + ["", "### Placas", ""] + [f"![Placa {i}]({u})" for i, u in enumerate(urls, 1)]
    body += ["", "### Texto del post", "", "```text", caption.strip(), "```"]
    if approved is not None:
        body += ["", f"Período de prueba: {approved} de {REVIEW_APPROVALS} publicaciones aprobadas de este tipo antes de pasar a publicar directo."]
    return "\n".join(body)


# ------------------------------------------------------------------ corrida

def save_state(state, message):
    save_json(DATASET_REPO_ID, STATE_PATH, state, message)


def counters(state, kind):
    """Período de prueba de un tipo de post. El de las órdenes del día queda en la raíz del estado (como antes)."""
    if kind == "od":
        return state
    return state.setdefault("review", {}).setdefault(kind, {"approved": 0, "mode": "review"})


def enqueue(state, key, kind, label, urls, caption, until=None):
    """Vista previa por Issue mientras el tipo está en período de prueba; después, publica directo.
    until: fecha ISO en que vence lo que informa la nota; si pasa antes de publicar, no se publica."""
    c = counters(state, kind)
    if c["mode"] == "review":
        number, url = create_issue(preview_title(label), preview_body(urls, caption, next_cron_run(), c["approved"]),
                                   ["instagram-vista-previa"])
        state["pending"][key] = {"issue": number, "created_at": iso(now()), "images": urls, "caption": caption, "kind": kind}
        if until:
            state["pending"][key]["until"] = until
        print(f"{key}: vista previa en {url}")
    else:
        print(f"{key}: publicado en {publish_doc(state, key, urls, caption)}")


def publish_doc(state, doc_id, urls, caption):
    """Publica el carrusel y devuelve el link. Guarda 'publicando' antes de media_publish para no duplicar el post."""
    prev = state["published"].get(doc_id)
    if prev and prev.get("status") == "done":  # ya publicado (p. ej. falló cerrar el Issue después): no repetir
        return prev.get("permalink", "")
    token = get_token()
    uid = user_id(token)
    if prev and prev.get("status") == "publishing":
        found = find_recent_post(token, uid, caption)
        if found:
            prev.update(status="done", media_id=found["id"], permalink=found.get("permalink", ""))
            save_state(state, f"Instagram: publicado {doc_id}")
            return prev["permalink"]
    container = build_carousel(token, uid, urls, caption)
    state["published"][doc_id] = {"status": "publishing", "container": container, "images": urls, "caption": caption, "at": iso(now())}
    save_state(state, f"Instagram: publicando {doc_id}")
    media_id = ig("POST", f"{uid}/media_publish", token, creation_id=container)["id"]
    permalink = ig("GET", media_id, token, fields="permalink").get("permalink", "")
    state["published"][doc_id].update(status="done", media_id=media_id, permalink=permalink)
    save_state(state, f"Instagram: publicado {doc_id}")
    return permalink


def run():
    index = load_index(DATASET_REPO_ID)
    state = load_json(DATASET_REPO_ID, STATE_PATH)
    if state is None:
        state = {"seen": [r["doc_id"] for r in index if r.get("doc_id")], "sessions": [], "pending": {}, "published": {},
                 "approved": 0, "mode": "review"}
        save_state(state, "Instagram: estado inicial (órdenes existentes marcadas como vistas)")
        print(f"Primera corrida: {len(state['seen'])} órdenes marcadas como vistas; no se publica nada.")
        return 0
    errors = 0
    ensure_setup()
    if has_token():  # mantenimiento diario: renueva antes de que venza aunque no haya nada para publicar
        try:
            get_token()
        except PublishError as e:
            report_error("token", str(e))
            errors += 1

    # publicaciones que quedaron a medias en modo directo (las de revisión se retoman desde su Issue)
    for doc_id, pub in list(state["published"].items()):
        if pub.get("status") == "publishing" and doc_id not in state["pending"]:
            try:
                publish_doc(state, doc_id, pub["images"], pub["caption"])
            except PublishError as e:
                report_error("publicacion", f"Documento {doc_id}: {e}")
                errors += 1

    # vistas previas: publicar las aprobadas en la corrida siguiente
    for doc_id, p in list(state["pending"].items()):
        try:
            issue = gh("GET", f"/issues/{p['issue']}")
            if "no-publicar" in {lb["name"] for lb in issue["labels"]} or issue["state"] == "closed":
                if issue["state"] == "open":
                    comment(p["issue"], "No se publica: tiene la etiqueta `no-publicar`.")
                    close_issue(p["issue"])
                del state["pending"][doc_id]
                save_state(state, f"Instagram: descartado {doc_id}")
                continue
            if now() - datetime.fromisoformat(p["created_at"]) < REVIEW_WAIT:
                continue
            if p.get("until") and now().date() > date.fromisoformat(p["until"]):
                comment(p["issue"], f"No se publica: el plazo que informa la nota terminó el "
                                    f"{date.fromisoformat(p['until']):%d/%m/%Y}. Queda en el resumen 'Qué se aprobó'.")
                close_issue(p["issue"])
                del state["pending"][doc_id]
                save_state(state, f"Instagram: vencido {doc_id}")
                continue
            permalink = publish_doc(state, doc_id, p["images"], p["caption"])
            comment(p["issue"], f"Publicado en Instagram: {permalink}")
            close_issue(p["issue"])
            del state["pending"][doc_id]
            kind = p.get("kind", "od")
            c = counters(state, kind)
            c["approved"] += 1
            if c["mode"] == "review" and c["approved"] >= REVIEW_APPROVALS:
                c["mode"] = "direct"
                comment(p["issue"], f"Con {REVIEW_APPROVALS} publicaciones aprobadas, termina el período de prueba: "
                                    f"desde ahora {KIND_NAMES[kind]} se publican directo.")
            save_state(state, f"Instagram: publicado {doc_id}")
        except PublishError as e:
            report_error("publicacion", f"Vista previa #{p['issue']} (documento {doc_id}): {e}")
            errors += 1

    # órdenes del día nuevas
    for rec in index:
        doc_id = rec.get("doc_id")
        if not doc_id or doc_id in state["seen"]:
            continue
        key = session_key(rec)
        try:
            if key and key in state["sessions"]:
                print(f"{doc_id}: la sesión {key} ya se publicó o está en revisión (PDF corregido); no se repite.")
            else:
                prep = prepare(doc_id, f"{doc_id}-{now():%Y%m%d%H%M%S}")
                if prep is None:
                    print(f"{doc_id}: sin ítems de alcance general; no se publica.")
                else:
                    doc, urls, caption = prep
                    enqueue(state, doc_id, "od", od_label(doc), urls, caption)
                    if key:
                        state["sessions"].append(key)
            state["seen"].append(doc_id)  # solo si se resolvió; si falló, se reintenta mañana
            save_state(state, f"Instagram: procesado {doc_id}")
        except PublishError as e:
            report_error("publicacion", f"Documento {doc_id}: {e}")
            errors += 1
    try:
        notes = prensa.load_notes()
    except (requests.RequestException, ValueError) as e:
        report_error("prensa", f"El sitio de Prensa no respondió bien ({type(e).__name__}).")
        return 1
    errors += run_prensa(state, notes)
    errors += run_boletin(state, notes)
    return 1 if errors else 0


def run_prensa(state, notes):
    """"Qué se aprobó" y destacados desde las notas de Prensa. Devuelve la cantidad de errores."""
    if "prensa_since" not in state:  # primera vez: las sesiones anteriores a hoy no se publican
        state["prensa_since"] = now().date().isoformat()
        save_state(state, "Instagram: Prensa desde hoy (sesiones anteriores marcadas como vistas)")
        print(f"Prensa: primera corrida; se publican las sesiones desde el {state['prensa_since']}.")
        return 0
    errors = 0
    since = date.fromisoformat(state["prensa_since"])
    for s in prensa.sessions(notes).values():
        if s["date"] < since:
            continue
        jobs = [(f"destacado:{n['id']}", "destacado", lambda folder, n=n, s=s: prepare_destacado(s, n, folder))
                for n in destacables(s, now().date())]
        if now().date() >= s["date"] + APROBADO_WAIT:
            jobs.append((f"aprobado:{s['key']}", "aprobado", lambda folder, s=s: prepare_aprobado(s, folder)))
        errors += run_jobs(state, jobs)
    return errors


def run_jobs(state, jobs):
    """[(clave, tipo, prep)]: genera y encola cada post no visto. Devuelve la cantidad de errores."""
    errors = 0
    for key, kind, prep in jobs:
        if key in state["seen"]:
            continue
        try:
            result = prep(f"{key.replace(':', '-')}-{now():%Y%m%d%H%M%S}")
            if result is None:
                print(f"{key}: sin texto usable; no se publica.")
            else:
                enqueue(state, key, kind, *result)
            state["seen"].append(key)  # solo si se resolvió; si falló, se reintenta mañana
            save_state(state, f"Instagram: procesado {key}")
        except AnthropicFailure as e:
            report_error("anthropic", str(e))
            errors += 1
        except PublishError as e:
            report_error("publicacion", f"{key}: {e}")
            errors += 1
    return errors


def boletin_candidates(store, notes, today, excluir=frozenset()):
    """Ordenanzas que irían como destacado, con su cruce con Prensa: [(nota, sesión, "nueva"|"duplicado"|"dudoso",
    nota de Prensa parecida o None)]. Sin las de particulares, las sin sesión ni las de plazo vencido. Del Boletín solo
    van temas de fondo o Bolsillo: las obras y servicios puntuales (una contratación, un tendido) no."""
    sessions = prensa.sessions(notes)
    out = []
    for o in store["ordenanzas"].values():
        if "text" not in o or not o.get("session") or f"ord-{o['numero']}" in excluir:
            continue
        n, s = boletin.as_note(o), boletin.session_of(o)
        fondo = prensa.themes(n) or "💰 Bolsillo" in prensa.destacado_tags(n)
        if fondo and prensa.is_destacado(n) and not prensa.vencida(n, today):
            out.append((n, s, *boletin.match(n, sessions.get(s["key"], {}).get("notes", []))))
    return out


def run_boletin(state, notes):
    """Destacados de las ordenanzas del Boletín Oficial que Prensa no cubrió (las dudosas no salen; las de
    particulares, solo agregadas en la puesta al día). Devuelve la cantidad de errores."""
    try:
        store = boletin.load(now().date())
    except (requests.RequestException, ValueError, RuntimeError) as e:
        report_error("boletin", f"El sitio del Boletín Oficial no respondió bien ({type(e).__name__}).")
        return 1
    last = max((int(k) for k in store["boletines"]), default=0)
    if "boletin_desde" not in state:  # primera vez: los boletines ya publicados no salen
        state["boletin_desde"] = last
        save_state(state, "Instagram: Boletín Oficial desde el próximo número")
        print(f"Boletín Oficial: primera corrida; se publican las ordenanzas de los boletines posteriores al N° {last}.")
        return 0
    jobs = []
    for n, s, status, best in boletin_candidates(store, notes, now().date()):
        if n["ordenanza"]["boletin"] <= state["boletin_desde"]:
            continue
        if status == "nueva":
            jobs.append((f"destacado:{n['id']}", "boletin", lambda folder, n=n, s=s: prepare_destacado(s, n, folder)))
        elif f"destacado:{n['id']}" not in state["seen"]:
            print(f"Ordenanza {n['ordenanza']['label']}: {status} con la nota {best['id']} de Prensa; no se publica.")
            state["seen"].append(f"destacado:{n['id']}")
            save_state(state, f"Instagram: Ordenanza {n['ordenanza']['label']} ya cubierta por Prensa")
    return run_jobs(state, jobs)


def check_token():
    """(ok, mensaje) sobre el secret IG_ACCESS_TOKEN: solo consulta /me, no publica. El mensaje nunca incluye el token."""
    token = ic.env_value("IG_ACCESS_TOKEN")
    if not token:
        return False, "Falta el secret `IG_ACCESS_TOKEN`."
    try:
        me = ig("GET", "me", token, fields="user_id,username")
    except PublishError as e:
        return False, f"El token no funciona: {e}"
    if me.get("username") != IG_USERNAME:
        return False, f"El token es de la cuenta @{me.get('username')}, no de @{IG_USERNAME}."
    expected_id = ic.env_value("IG_USER_ID")
    if expected_id and str(me.get("user_id")) != expected_id:
        return False, f"El token es de @{IG_USERNAME}, pero el secret `IG_USER_ID` no coincide con el ID de esa cuenta."
    return True, f"Token válido: cuenta @{IG_USERNAME}."


def od_preview(session_number, year, stamp):
    matches = [r for r in load_index(DATASET_REPO_ID)
               if str(r.get("session_number")) == str(session_number) and str(r.get("session_year")) == str(year)]
    if not matches:
        raise PublishError(f"No hay una orden del día N° {session_number} de {year} en el índice.")
    rec = max(matches, key=lambda r: r.get("created_at") or "")
    prep = prepare(rec["doc_id"], f"prueba/{rec['doc_id']}-{stamp}")
    if prep is None:
        doc = load_processed_json(DATASET_REPO_ID, rec["doc_id"]) or {}
        return [(od_label(doc), "**PRUEBA:** esta orden del día no tiene ítems de alcance general, así que no se publicaría.")]
    doc, urls, caption = prep
    return [(od_label(doc), preview_body(urls, caption))]


def prensa_preview(session, year, kind, stamp):
    """session: "23" (ordinaria) o "E1" (especial). Destacado: un Issue por nota destacable (hasta 3)."""
    especial = str(session)[:1] in "Ee"
    key = prensa.session_key(str(session).lstrip("EeOo"), year, "Especial" if especial else "Ordinaria")
    s = prensa.sessions(prensa.load_notes(save=False)).get(key)
    if not s:
        raise PublishError(f"No hay notas de Prensa con temas aprobados en la sesión {session} de {year}.")
    if kind == "aprobado":
        r = prepare_aprobado(s, f"prueba/aprobado-{key}-{stamp}")
        return [(r[0], preview_body(r[1], r[2])) if r else
                (f"Qué se aprobó · sesión {session}", "**PRUEBA:** ninguna nota de esta sesión dio un resumen usable.")]
    notes = prensa.rank(destacables(s, now().date()))[:3]
    if not notes:
        return [(f"Destacado · sesión {session}", "**PRUEBA:** esta sesión no tiene notas aprobadas de Bolsillo, "
                                                 "Servicios u Obras con un plazo vigente, así que no habría destacado.")]
    out = []
    for n in notes:
        r = prepare_destacado(s, n, f"prueba/destacado-{n['id']}-{stamp}")
        out.append((r[0], preview_body(r[1], r[2])) if r else (f"Destacado · {n['title']}", "**PRUEBA:** sin texto usable."))
    return out


def boletin_preview(session, year, stamp):
    """Destacados del Boletín Oficial de una sesión (hasta 3), y por qué no sale cada ordenanza cubierta por Prensa."""
    especial = str(session)[:1] in "Ee"
    key = prensa.session_key(str(session).lstrip("EeOo"), year, "Especial" if especial else "Ordinaria")
    today = now().date()
    cands = [c for c in boletin_candidates(boletin.load(today, save=False), prensa.load_notes(save=False), today)
             if c[1]["key"] == key]
    if not cands:
        return [(f"Boletín · sesión {session}", "**PRUEBA:** el Boletín Oficial todavía no tiene ordenanzas de esta sesión "
                                               "con tema de fondo o de Bolsillo, así que no habría destacado.")]
    out = [(f"Boletín · Ordenanza {n['ordenanza']['label']}",
            f"**PRUEBA:** no saldría: es {status} con la nota [{best['id']}]({best['link']}) de Prensa.")
           for n, _, status, best in cands if status != "nueva"]
    for n in prensa.rank([n for n, _, status, _ in cands if status == "nueva"])[:3]:
        s = next(s for m, s, _, _ in cands if m is n)
        r = prepare_destacado(s, n, f"prueba/{n['id']}-{stamp}")
        out.append((r[0], preview_body(r[1], r[2])) if r else (f"Boletín · Ordenanza {n['ordenanza']['label']}",
                                                               "**PRUEBA:** sin texto usable."))
    return out


def run_preview(session_number, year, tipo="orden-del-dia"):
    """Workflow "Probar Instagram": Issue con la vista previa de una sesión y la verificación del token, para cada
    tipo de post. No publica ni toca el estado."""
    token_ok, token_msg = check_token()
    check = ["### Verificación del token de Instagram", "", ("✅ " if token_ok else "❌ ") + token_msg, ""]
    ensure_setup()
    stamp = f"{now():%Y%m%d%H%M%S}"
    kind = TIPOS[tipo]
    if kind == "od":
        previews = od_preview(session_number, year, stamp)
    elif kind == "boletin":
        previews = boletin_preview(session_number, year, stamp)
    else:
        previews = prensa_preview(session_number, year, kind, stamp)
    for label, body in previews:
        number, url = create_issue(preview_title(label, prueba=True), "\n".join(check) + "\n" + body, ["instagram-prueba"])
        print(f"Vista previa de prueba: {url}")
    print(("OK: " if token_ok else "ERROR: ") + token_msg)
    return 0 if token_ok else 1  # en rojo en Actions si el token no sirve (el detalle está en el Issue)


# ------------------------------------------------------------------ puesta al día (workflow manual)

def period_start():
    """(fecha, explicación) del inicio de la puesta al día: el día del último post de la cuenta, o el 1/6/2026."""
    try:
        token = get_token()
        media = ig("GET", f"{user_id(token)}/media", token, fields="timestamp", limit=1).get("data", [])
    except PublishError as e:
        return PUESTA_DEFAULT_START, f"no se pudo consultar Instagram ({e}); se usa el 1/6/2026"
    if not media or not media[0].get("timestamp"):
        return PUESTA_DEFAULT_START, "la cuenta no tiene posts; se usa el 1/6/2026"
    last = datetime.strptime(media[0]["timestamp"], "%Y-%m-%dT%H:%M:%S%z")
    return last.date(), f"fecha del último post de @{IG_USERNAME}"


def puesta_items(notes, start, today, excluir, skip_keys, slots=ic.MAX_ITEMS, extra=()):
    """Notas que serían destacado, de sesiones entre start y today, sin plazo vencido, sin las excluidas ni las ya
    publicadas como destacado; las de mayor peso primero. Si sobran (más que slots), se agrupa solo lo necesario: la
    nota suelta de menor peso que comparte tema con otra placa se suma a esa placa, hasta que entren; si no hay temas
    repetidos, se recortan las de menor peso. extra: [(ordenanza como nota, sesión)] del Boletín Oficial que Prensa no
    cubrió. Devuelve (ítems para las placas, notas descartadas por texto no usable)."""
    sessions = prensa.sessions(notes)
    candidates = [(n, s) for s in sessions.values() for n in destacables(s, today)] + list(extra)
    candidates = [(n, s) for n, s in candidates if start <= s["date"] <= today
                  and str(n["id"]) not in excluir and f"destacado:{n['id']}" not in skip_keys]
    ranked = prensa.rank([n for n, _ in candidates])
    session_of = {n["id"]: s for n, s in candidates}
    units = [(prensa.group_key(n), [n]) for n in ranked]
    while len(units) > slots:
        merged = False
        for i in range(len(units) - 1, 0, -1):
            # a la placa más cercana del mismo tema: las de mayor peso quedan sueltas, al principio
            j = next((j for j in range(i - 1, -1, -1) if units[j][0] == units[i][0]), None)
            if j is not None:
                units[j][1].extend(units.pop(i)[1])
                merged = True
                break
        if not merged:
            break
    items, dropped = [], []
    for key, unit in units:
        if len(items) == slots:
            break
        made = []
        for n in unit:  # Haiku solo para las notas que van a salir
            text = prensa.note_text(n)
            fields, error = ic.destacado(text, ordenanza=bool(n.get("ordenanza")))
            if error:
                raise AnthropicFailure(f"{error} No se generó la puesta al día; corré el workflow de nuevo más tarde.")
            line = fields and (fields["que_cambia"] or ic.note_line(text))
            if line:
                made.append((n, fields, line))
            else:
                dropped.append(n)
        chips = lambda n: prensa.themes(n)[:1] + n["impact_tags"]  # noqa: E731
        if len(made) == 1:
            n, fields, line = made[0]
            items.append({"note": n, "sdoc": prensa.session_doc(session_of[n["id"]]), "titular": fields["titular"],
                          "line": line, "impact_tags": chips(n), "ordenanza": n.get("ordenanza")})
        elif made:
            label = lambda n: f", Ordenanza {n['ordenanza']['label']}" if n.get("ordenanza") else ""  # noqa: E731
            items.append({"notes": [n for n, _, _ in made], "titular": f"{key}: {len(made)} decisiones",
                          "lines": [f"{f['titular']} ({ic.session_title(prensa.session_doc(session_of[n['id']]))}{label(n)})"
                                    for n, f, _ in made],
                          "impact_tags": chips(made[0][0]), "ref": group_ref([n for n, _, _ in made])})
    return items, dropped


def group_ref(notes):
    """Pie de una placa agrupada: de dónde salen sus decisiones (None: solo Prensa)."""
    bo = sorted({n["ordenanza"]["boletin"] for n in notes if n.get("ordenanza")})
    if not bo:
        return None
    nums = "Boletín Oficial N° " + ", ".join(map(str, bo))
    return f"Fuente: {nums}" if all(n.get("ordenanza") for n in notes) else f"Fuente: Prensa del Concejo y {nums}"


def puesta_adjudicaciones(store, start, today, excluir):
    """Una placa con las adjudicaciones de tierras a particulares del período, solo agregadas por chacra: ni nombres,
    ni lote, ni manzana, ni número de ordenanza (en un pueblo chico identifican a la persona). None si no hay."""
    ords = sorted((o for o in store["ordenanzas"].values() if o.get("adjudicacion") and o.get("session")
                   and start <= date.fromisoformat(o["session"]["date"]) <= today and f"ord-{o['numero']}" not in excluir),
                  key=lambda o: o["numero"])
    groups = boletin.adjudicaciones(ords)
    if not groups:
        return None
    line = lambda g: f"Se adjudicaron {g['lotes']} lote{'s' if g['lotes'] != 1 else ''} en Chacra {g['chacra']} a familias"  # noqa: E731
    if len(groups) == 1:
        titular, lines = line(groups[0]), []
    else:
        titular = f"Se adjudicaron {sum(g['lotes'] for g in groups)} lotes a familias"
        lines = [line(g) for g in groups]
    return {"notes": [{"id": f"ord-{o['numero']}", "link": o["url"]} for o in ords], "titular": titular, "lines": lines,
            "impact_tags": ["Tierras"], "kicker": "TIERRAS · BOLETÍN OFICIAL",
            "ref": "Fuente: Boletín Oficial N° " + ", ".join(map(str, sorted({o["boletin"] for o in ords})))}


def puesta_audiencias(notes, start, today, excluir, ord_notes=()):
    """Audiencias públicas sobre tierras y concesiones ya realizadas en el período y todavía sin ordenanza en el Boletín
    Oficial. Devuelve ([{numero, fecha, line, ids}], [(audiencia, ordenanza que la resolvió)])."""
    auds = [a for a in prensa.audiencias(notes, start, today) if not {str(n["id"]) for n in a["notes"]} & excluir]
    decididas = [(a, o) for a in auds for o in [boletin.decided(a["notes"], ord_notes)] if o]
    auds = [a for a in auds if not any(a is d for d, _ in decididas)]
    lines, error = ic.audiencia_lines([prensa.note_text(a["notes"][0]) for a in auds],
                                      [prensa.audiencia_subject(a["notes"][0]) for a in auds])
    if error and auds:
        raise AnthropicFailure(f"{error} No se generó la puesta al día; corré el workflow de nuevo más tarde.")
    return [{"numero": a["numero"], "fecha": a["fecha"], "line": line, "ids": [n["id"] for n in a["notes"]]}
            for a, line in zip(auds, lines)], decididas


def item_ids(it):
    return [n["id"] for n in it.get("notes") or [it["note"]]]


def puesta_preview(excluir):
    state = load_json(DATASET_REPO_ID, STATE_PATH)
    if state is None:
        raise PublishError("Todavía no hay estado de Instagram: tiene que correr primero el cron diario.")
    ensure_setup()
    start, why = period_start()
    today = now().date()
    notes = prensa.load_notes()
    head = [f"Período: sesiones desde el **{start:%d/%m/%Y}** ({why}) hasta hoy.", ""]
    try:
        store = boletin.load(today)
    except (requests.RequestException, ValueError, RuntimeError) as e:
        store = {"boletines": {}, "ordenanzas": {}}
        head += [f"⚠️ No se pudo leer el Boletín Oficial ({type(e).__name__}): sale solo con Prensa.", ""]
    cands = [c for c in boletin_candidates(store, notes, today, excluir) if start <= c[1]["date"] <= today]
    ord_notes = [boletin.as_note(o) for o in store["ordenanzas"].values() if "text" in o]
    auds, decididas = puesta_audiencias(notes, start, today, excluir, ord_notes)
    adj = puesta_adjudicaciones(store, start, today, excluir)
    items, dropped = puesta_items(notes, start, today, excluir,
                                  set(state["seen"]) | set(state["pending"]) | set(state["published"]),
                                  slots=ic.MAX_ITEMS - (1 if auds else 0) - (1 if adj else 0),
                                  extra=[(n, s) for n, s, status, _ in cands if status == "nueva"])
    items += [adj] if adj else []
    if excluir:
        head += [f"Notas excluidas a pedido: {', '.join(sorted(excluir))}.", ""]
    if dropped:
        head += [f"Sin texto usable (quedan afuera): {', '.join(str(n['id']) for n in dropped)}.", ""]
    if not items and not auds:
        number, url = create_issue("Vista previa Instagram: Puesta al día", "\n".join(head + [
            "No hay notas de Bolsillo, Servicios u Obras con un plazo vigente en ese período: no hay nada para publicar."]),
            ["instagram-puesta-al-dia"])
        print(f"Puesta al día sin notas: {url}")
        return 0
    head += ["### Notas incluidas (id para el campo `excluir`)", ""]
    for it in items:
        if "lines" in it:
            if it is adj:  # adjudicaciones: ni números de ordenanza ni ids, tampoco en el Issue
                head += [f"- {it['titular']}"] + [f"  - {line}" for line in it["lines"]]
                continue
            head += [f"- {it['titular']}:"] + [f"  - `{n['id']}` {line} — [nota]({n['link']})" for n, line in zip(it["notes"], it["lines"])]
        else:
            head += [f"- `{it['note']['id']}` {it['titular']} — [nota]({it['note']['link']})"]
    if auds:
        head += ["", f"### {ic.AUDIENCIA_TITLE} (ids para `excluir`)", ""]
        head += [f"- {', '.join(f'`{i}`' for i in a['ids'])} N° {a['numero']}: {a['line']} (audiencia del {a['fecha']:%d/%m})"
                 for a in auds]
    if decididas:
        head += ["", "### Audiencias que ya tienen ordenanza (salen de esa placa)", ""]
        head += [f"- N° {a['numero']}: Ordenanza {o['ordenanza']['label']} (`{o['id']}`)" for a, o in decididas]
    dudosas = [(n, best) for n, _, status, best in cands if status == "dudoso"]
    if dudosas:
        head += ["", "### Ordenanzas parecidas a una nota de Prensa (no se publican; revisalas)", ""]
        head += [f"- `{n['id']}` Ordenanza {n['ordenanza']['label']}: {n['title'][:120]} (¿es la nota `{best['id']}`?)"
                 for n, best in dudosas]
    revisar = [o for o in store["ordenanzas"].values() if o.get("a_revisar") and (o.get("promulgada") or "") >= start.isoformat()]
    if revisar:
        head += ["", "### Ordenanzas sin sesión identificada (no se publican)", ""]
        head += [f"- Ordenanza {o['label']} (Boletín Oficial N° {o['boletin']})" for o in revisar]
    head += ["", "**No se publica nada todavía.** Si está bien, corré el workflow **Puesta al día** con `publicar = sí`: "
                 "publica exactamente estas placas y este texto. Para sacar notas, corrélo de nuevo con `excluir`."]
    with tempfile.TemporaryDirectory() as tmp:
        paths = ic.render_puesta(start, today, items, tmp, ext="jpg", audiencias=auds)
        caption = (Path(tmp) / "caption.txt").read_text(encoding="utf-8")
        shown = "\n".join([caption] + [it.get("line", "") for it in items] + [x for it in items for x in it.get("lines", [])])
        if boletin.leaks(shown):
            raise PublishError("La puesta al día nombraba a un particular: no se generó. Revisá las ordenanzas del período.")
        urls = upload_images(paths, f"puesta-{now():%Y%m%d%H%M%S}")
    old = state.get("puesta")
    number, url = create_issue(f"Vista previa Instagram: Puesta al día desde {ic.month_since(start, today)}",
                               preview_body(urls, caption, head=head), ["instagram-puesta-al-dia"])
    if old and old.get("status") == "preview":
        comment(old["issue"], f"Reemplazada por la vista previa #{number}.")
        close_issue(old["issue"])
    state["puesta"] = {"issue": number, "images": urls, "caption": caption, "created_at": iso(now()), "status": "preview",
                       "since": start.isoformat(), "notes": [i for it in items for i in item_ids(it)]}
    save_state(state, "Instagram: vista previa de la puesta al día")
    print(f"Puesta al día: vista previa en {url}")
    return 0


def puesta_publish():
    """Publica la última vista previa, sin regenerarla. No cuenta para el período de prueba de ningún tipo."""
    state = load_json(DATASET_REPO_ID, STATE_PATH) or {}
    p = state.get("puesta")
    if not p:
        raise PublishError("No hay vista previa de la puesta al día: corré el workflow con publicar = no.")
    if p["status"] == "done":
        raise PublishError(f"Esta puesta al día ya se publicó: {p.get('permalink', '')}")
    issue = gh("GET", f"/issues/{p['issue']}")
    if issue["state"] == "closed" or "no-publicar" in {lb["name"] for lb in issue["labels"]}:
        raise PublishError(f"La vista previa #{p['issue']} está cerrada o tiene `no-publicar`: no se publica.")
    permalink = publish_doc(state, f"puesta:{p['created_at']}", p["images"], p["caption"])
    p.update(status="done", permalink=permalink)
    # sus notas no vuelven a salir como destacado
    state["seen"] += [k for k in (f"destacado:{i}" for i in p["notes"]) if k not in state["seen"]]
    save_state(state, "Instagram: puesta al día publicada")
    comment(p["issue"], f"Publicado en Instagram: {permalink}")
    close_issue(p["issue"])
    print(f"Puesta al día publicada: {permalink}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Publica en Instagram las órdenes del día nuevas y los posts de Prensa.")
    ap.add_argument("--prueba", metavar="N", help="número de sesión (E1 = Sesión Especial 1): abre un Issue de vista previa, sin publicar")
    ap.add_argument("--anio", type=int, default=now().year)
    ap.add_argument("--tipo", choices=list(TIPOS), default="orden-del-dia")
    ap.add_argument("--puesta", action="store_true", help="puesta al día: vista previa por Issue (o publicarla con --publicar sí)")
    ap.add_argument("--publicar", choices=["no", "sí", "si"], default="no")
    ap.add_argument("--excluir", default="", help="ids de notas de Prensa separados por coma")
    args = ap.parse_args()
    try:
        if args.puesta:
            if args.publicar != "no":
                return puesta_publish()
            return puesta_preview({x.strip() for x in args.excluir.split(",") if x.strip()})
        return run_preview(args.prueba, args.anio, args.tipo) if args.prueba else run()
    except PublishError as e:
        report_error("otro", str(e))
        return 1
    except Exception as e:  # cualquier otra falla también abre un Issue (las de red ya llegan sin URLs ni tokens)
        traceback.print_exc()
        report_error("otro", f"{type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
