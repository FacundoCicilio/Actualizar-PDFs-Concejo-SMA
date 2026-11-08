# Registra en el dataset los videos nuevos del canal del Concejo y abre un Issue por cada uno, para revisarlos a mano.
# Solo registro: no toca Instagram, destacados/ ni instagram/state.json. Ver YOUTUBE_API_SETUP.md.
import json
import os

import requests

from hf_dataset_store import DATASET_REPO_ID, load_json, save_json

CHANNEL_ID = "UCaJbax3QkHRsO4BHKPcxfEw"
UPLOADS = "UU" + CHANNEL_ID[2:]  # lista "subidas" del canal
API = "https://www.googleapis.com/youtube/v3"
INDEX_PATH = "concejo-videos/_index.json"
LABEL = "nuevo-video-concejo"
RECIENTES = 15


def youtube(recurso, key, **params):
    # La clave va en un header: en la URL aparecería en los logs si raise_for_status falla.
    r = requests.get(f"{API}/{recurso}", params=params, headers={"X-Goog-Api-Key": key}, timeout=30)
    r.raise_for_status()
    return r.json()["items"]


def latest_videos(key):
    """Los últimos videos subidos, del más nuevo al más viejo. Cuesta 2 unidades de cuota."""
    items = youtube("playlistItems", key, part="contentDetails", playlistId=UPLOADS, maxResults=RECIENTES)
    ids = ",".join(i["contentDetails"]["videoId"] for i in items)
    return [{"video_id": v["id"], "title": v["snippet"]["title"], "published_at": v["snippet"]["publishedAt"],
             "duration": v["contentDetails"]["duration"], "description": v["snippet"]["description"]}
            for v in youtube("videos", key, part="snippet,contentDetails", id=ids)] if ids else []


def issue_body(v):
    # Bloque de código: la descripción viene de afuera y una @mención notificaría a alguien.
    return "\n".join([f"https://www.youtube.com/watch?v={v['video_id']}", "",
                      f"- Publicado: {v['published_at']}", f"- Duración: {v['duration']}", "",
                      "### Descripción", "", "```text", v["description"][:2000].replace("```", "'''"), "```", "",
                      "Solo registro: este video no genera ninguna publicación."])


def open_issue(v, repo, token):
    url = f"https://api.github.com/repos/{repo}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    r = requests.post(f"{url}/labels", headers=headers, timeout=30, json={"name": LABEL, "color": "1d76db"})
    if r.status_code not in (201, 422):  # 422: ya existe
        r.raise_for_status()
    requests.post(f"{url}/issues", headers=headers, timeout=30, json={
        "title": f"Nuevo video del Concejo: {v['title']}", "body": issue_body(v), "labels": [LABEL]}).raise_for_status()


def main():
    key = os.getenv("YOUTUBE_API_KEY", "").strip()
    if not key:
        print("Sin YOUTUBE_API_KEY: no se revisa el canal.")
        return 0
    index = load_json(DATASET_REPO_ID, INDEX_PATH)
    primera = index is None
    index = index or {}
    nuevos = [v for v in latest_videos(key) if v["video_id"] not in index]
    if nuevos or primera:
        index.update({v["video_id"]: v for v in nuevos})
        save_json(DATASET_REPO_ID, INDEX_PATH, index, f"Videos del Concejo: {len(nuevos)} nuevos")
    print(f"{len(nuevos)} videos nuevos; {len(index)} en el índice.")
    if primera:  # lo que ya estaba en el canal no es novedad: sin esto serían 15 Issues de una vez
        print("Primera corrida: índice inicial, sin Issues.")
        return 0
    # Después de guardar: si un Issue falla se pierde ese aviso, pero una corrida nueva no duplica los ya abiertos.
    repo, token = os.getenv("GITHUB_REPOSITORY", ""), os.getenv("GITHUB_TOKEN", "")
    for v in reversed(nuevos):
        if repo and token:
            open_issue(v, repo, token)
    if os.getenv("GITHUB_OUTPUT"):  # el job "transcribir" del workflow corre una vez por video
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"nuevos={json.dumps([v['video_id'] for v in nuevos])}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
