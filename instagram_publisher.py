# Publica en Instagram cada orden del día nueva. Corre en el cron, después de auto_fetch.py.
#   python instagram_publisher.py                          # corrida normal
#   python instagram_publisher.py --prueba 23 [--anio 2026]  # Issue de vista previa: no publica ni toca el estado
# Período de prueba: cada orden nueva abre un Issue; si no tiene la etiqueta "no-publicar", la corrida diaria
# siguiente la publica. Después de 3 publicaciones aprobadas así, pasa a publicar directo.
# Estado y token viven en el dataset privado de HF. Nunca se imprime ni se reporta un token.
import argparse
import base64
import sys
import tempfile
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import instagram_carousel as ic
from hf_dataset_store import DATASET_REPO_ID, load_index, load_json, load_processed_json, save_json

STATE_PATH = "instagram/state.json"
TOKEN_PATH = "instagram/token.json"  # aparte del estado: el estado puede terminar citado en un Issue, el token nunca
MEDIA_BRANCH = "instagram-media"     # imágenes públicas para Instagram; un push acá no dispara el deploy del Space
GRAPH = "https://graph.instagram.com/v25.0"
IG_USERNAME = "concejo.sma"  # cuenta en la que se publica: la prueba verifica que el token sea de esta cuenta
REVIEW_APPROVALS = 3
REVIEW_WAIT = timedelta(hours=12)    # "la próxima corrida diaria": re-ejecutar el mismo día no publica
REFRESH_BEFORE = timedelta(days=15)  # el token dura 60 días; con el cron diario hay ~15 intentos de renovación
CRON_HOUR_UTC = 10                   # igual que el schedule de auto-fetch.yml
LABELS = {"instagram-vista-previa": "1F4E79", "no-publicar": "B60205", "instagram-error": "D93F0B", "instagram-prueba": "C5DEF5"}
ERROR_TITLES = {
    "token": "Instagram: problema con el token",
    "publicacion": "Instagram: falló la publicación",
    "anthropic": "Instagram: falló la API de Anthropic",
    "otro": "Instagram: error inesperado",
}


class PublishError(Exception):
    """Error con un mensaje apto para un Issue: nunca incluye tokens."""


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


def next_cron_run():
    t = now() + REVIEW_WAIT
    run = t.replace(hour=CRON_HOUR_UTC, minute=0, second=0, microsecond=0)
    return run if run > t else run + timedelta(days=1)


def preview_title(doc, prueba=False):
    return f"{'PRUEBA ' if prueba else 'Vista previa '}Instagram: {ic.session_title(doc)} ({ic.date_long(doc.get('date'))})"


def preview_body(urls, caption, deadline=None, approved=None):
    if deadline is None:
        head = ["**PRUEBA: esta vista previa no se publica.**"]
    else:
        when = f"{deadline:%d/%m} a las {deadline:%H}:00 UTC ({(deadline - timedelta(hours=3)):%H}:00 en Argentina)"
        head = [f"Se publica en Instagram en la próxima corrida del cron: **{when}** (puede demorar unos minutos).", "",
                "**Si no querés que se publique, poné la etiqueta `no-publicar`** (o cerrá este Issue) antes de esa hora."]
    body = head + ["", "### Placas", ""] + [f"![Placa {i}]({u})" for i, u in enumerate(urls, 1)]
    body += ["", "### Texto del post", "", "```text", caption.strip(), "```"]
    if approved is not None:
        body += ["", f"Período de prueba: {approved} de {REVIEW_APPROVALS} publicaciones aprobadas antes de pasar a publicar directo."]
    return "\n".join(body)


# ------------------------------------------------------------------ corrida

def save_state(state, message):
    save_json(DATASET_REPO_ID, STATE_PATH, state, message)


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
            permalink = publish_doc(state, doc_id, p["images"], p["caption"])
            comment(p["issue"], f"Publicado en Instagram: {permalink}")
            close_issue(p["issue"])
            del state["pending"][doc_id]
            state["approved"] += 1
            if state["mode"] == "review" and state["approved"] >= REVIEW_APPROVALS:
                state["mode"] = "direct"
                comment(p["issue"], f"Con {REVIEW_APPROVALS} publicaciones aprobadas, termina el período de prueba: "
                                    "desde ahora las órdenes del día nuevas se publican directo.")
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
                    if state["mode"] == "review":
                        number, url = create_issue(preview_title(doc), preview_body(urls, caption, next_cron_run(), state["approved"]),
                                                   ["instagram-vista-previa"])
                        state["pending"][doc_id] = {"issue": number, "created_at": iso(now()), "images": urls, "caption": caption}
                        print(f"{doc_id}: vista previa en {url}")
                    else:
                        print(f"{doc_id}: publicado en {publish_doc(state, doc_id, urls, caption)}")
                    if key:
                        state["sessions"].append(key)
            state["seen"].append(doc_id)  # solo si se resolvió; si falló, se reintenta mañana
            save_state(state, f"Instagram: procesado {doc_id}")
        except PublishError as e:
            report_error("publicacion", f"Documento {doc_id}: {e}")
            errors += 1
    return 1 if errors else 0


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


def run_preview(session_number, year):
    """Workflow "Probar Instagram": Issue con la vista previa de una orden ya procesada y la verificación del token.
    No publica ni toca el estado."""
    matches = [r for r in load_index(DATASET_REPO_ID)
               if str(r.get("session_number")) == str(session_number) and str(r.get("session_year")) == str(year)]
    if not matches:
        raise PublishError(f"No hay una orden del día N° {session_number} de {year} en el índice.")
    rec = max(matches, key=lambda r: r.get("created_at") or "")
    token_ok, token_msg = check_token()
    check = ["### Verificación del token de Instagram", "", ("✅ " if token_ok else "❌ ") + token_msg, ""]
    ensure_setup()
    prep = prepare(rec["doc_id"], f"prueba/{rec['doc_id']}-{now():%Y%m%d%H%M%S}")
    if prep is None:
        doc = load_processed_json(DATASET_REPO_ID, rec["doc_id"]) or {}
        body = "**PRUEBA:** esta orden del día no tiene ítems de alcance general, así que no se publicaría."
    else:
        doc, urls, caption = prep
        body = preview_body(urls, caption)
    number, url = create_issue(preview_title(doc, prueba=True), "\n".join(check) + "\n" + body, ["instagram-prueba"])
    print(f"Vista previa de prueba: {url}")
    print(("OK: " if token_ok else "ERROR: ") + token_msg)
    return 0 if token_ok else 1  # en rojo en Actions si el token no sirve (el detalle está en el Issue)


def main():
    ap = argparse.ArgumentParser(description="Publica en Instagram las órdenes del día nuevas.")
    ap.add_argument("--prueba", metavar="N", help="número de orden del día: abre un Issue de vista previa, sin publicar")
    ap.add_argument("--anio", type=int, default=now().year)
    args = ap.parse_args()
    try:
        return run_preview(args.prueba, args.anio) if args.prueba else run()
    except PublishError as e:
        report_error("otro", str(e))
        return 1
    except Exception as e:  # cualquier otra falla también abre un Issue (las de red ya llegan sin URLs ni tokens)
        traceback.print_exc()
        report_error("otro", f"{type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
