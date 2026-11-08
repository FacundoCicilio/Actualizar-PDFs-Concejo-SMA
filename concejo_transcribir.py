# Transcribe un video del Concejo con faster-whisper, lo sube al dataset y comenta los hallazgos en su Issue.
#   python concejo_transcribir.py VIDEO_ID [VIDEO_ID ...]                 # lo que corre el workflow
#   python concejo_transcribir.py VIDEO_ID --audio x.mp3 --sin-subir      # local, con el audio ya bajado
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests
import yaml

from concejo_videos import LABEL

PROMPT = ("Concejo Deliberante San Martín de los Andes. Concejales: Brondo, Petagna, Vita, Durando. Temas: ordenanza, "
          "dictamen, tarifa, agua, saneamiento, votación, cuarto intermedio.")
HF_DIR = "concejo-transcripciones"
CHUNK = 3600       # audios de más de 5 h se transcriben en tramos de 1 h
LARGO = 5 * 3600
AVISO = "Transcripción automática. Puede haber errores de reconocimiento, especialmente en nombres propios."
VOTACION = ["aprobado", "aprobada", "rechazado", "rechazada", "giro a comisión", "cuarto intermedio",
            "por la afirmativa", "por la negativa", "abstención", "unanimidad"]
TEMAS = ["agua", "tarifa", "cooperativa", "licitación", "comodato", "aporte", "pedido de informes", "deuda", "millones"]
POR_TERMINO = 25   # links por término en el comentario: GitHub corta a 65536 caracteres


def hms(s):
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def link(video_id, s):
    return f"https://youtube.com/watch?v={video_id}&t={int(s)}s"


def bajar_audio(video_id, carpeta):
    url = f"https://www.youtube.com/watch?v={video_id}"
    base = ["yt-dlp", "--js-runtimes", "node", "-x", "--audio-format", "mp3", "--audio-quality", "5", "-o", f"{carpeta}/{video_id}.%(ext)s"]
    for extra in ([], ["--extractor-args", "youtube:player_client=android"]):
        if subprocess.run(base + extra + [url]).returncode == 0:
            return Path(carpeta) / f"{video_id}.mp3"
    raise SystemExit(f"yt-dlp no pudo bajar {video_id}, ni con el cliente android.")


def duracion(audio):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(audio)],
                         capture_output=True, text=True, check=True).stdout
    return float(out)


def tramos(audio, carpeta):
    """[(archivo, offset)]: el audio entero, o tramos de 1 h si dura más de 5 h."""
    if duracion(audio) <= LARGO:
        return [(audio, 0)]
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(audio), "-f", "segment", "-segment_time", str(CHUNK),
                    "-c", "copy", f"{carpeta}/tramo%03d.mp3"], check=True)
    return [(p, i * CHUNK) for i, p in enumerate(sorted(Path(carpeta).glob("tramo*.mp3")))]


def transcribir(audio, modelo="medium", limite=None):
    """Segmentos [{start, end, text}]. Sin memoria para medium, usa small. Pasado `limite` (time.time()), corta."""
    from faster_whisper import WhisperModel
    try:
        m = WhisperModel(modelo, device="cpu", compute_type="int8")
    except (MemoryError, RuntimeError) as e:
        if modelo == "small":
            raise
        print(f"No cargó {modelo} ({e}): uso small.")
        m, modelo = WhisperModel("small", device="cpu", compute_type="int8"), "small"
    segs, completo = [], True
    with tempfile.TemporaryDirectory() as tmp:
        for archivo, offset in tramos(audio, tmp):
            out, _ = m.transcribe(str(archivo), language="es", initial_prompt=PROMPT, vad_filter=True,
                                  word_timestamps=True)
            for s in out:
                segs.append({"start": round(offset + s.start, 2), "end": round(offset + s.end, 2),
                             "text": s.text.strip()})
                if limite and time.time() > limite:
                    completo = False
                    break
            if not completo:
                break
            print(f"Tramo desde {hms(offset)} listo: {len(segs)} segmentos.", flush=True)
    return {"modelo": modelo, "completo": completo, "segments": segs}


def guardar(video_id, res, carpeta):
    carpeta.mkdir(parents=True, exist_ok=True)
    j, t = carpeta / f"{video_id}.json", carpeta / f"{video_id}.txt"
    j.write_text(json.dumps({"video_id": video_id, **res}, ensure_ascii=False, indent=1), encoding="utf-8")
    t.write_text("".join(f"[{hms(s['start'])}] {s['text']}\n" for s in res["segments"]), encoding="utf-8")
    return j, t


def patrones(concejales):
    """[(categoría, regex)]. Las palabras van entre límites de palabra: "agua" no matchea "aguantar"."""
    palabra = lambda w: re.compile(rf"\b{re.escape(w)}\b", re.I)  # noqa: E731
    return ([("Dictámenes", re.compile(r"\b\d{3,4}\s*/\s*20\d{2}\b")),
             ("Ordenanzas", re.compile(r"\bordenanza\s+(?:n[°º.]?\s*)?\d{3,5}\b", re.I))]
            + [("Votación", palabra(w)) for w in VOTACION]
            + [("Concejales", palabra(c)) for c in concejales]
            + [("Temas", palabra(w)) for w in TEMAS])


def analizar(segs, concejales):
    """{categoría: {término: [segundos]}}. El término es el texto encontrado, normalizado."""
    out = {}
    for seg in segs:
        for cat, rx in patrones(concejales):
            for m in rx.finditer(seg["text"]):
                termino = re.sub(r"\s+", "", m[0]) if cat == "Dictámenes" else re.sub(r"\s+", " ", m[0].lower())
                out.setdefault(cat, {}).setdefault(termino, []).append(seg["start"])
    return out


def comentario(video_id, hallazgos, res):
    lineas = [f"## Transcripción ({res['modelo']})", ""]
    if not res["completo"]:
        fin = hms(res["segments"][-1]["end"]) if res["segments"] else "0:00:00"
        lineas += [f"**Parcial:** se cortó en {fin} por el límite de tiempo del workflow.", ""]
    for cat, terminos in hallazgos.items():
        lineas += [f"### {cat}", ""]
        for termino, ts in sorted(terminos.items(), key=lambda kv: kv[1][0]):
            links = ", ".join(f"[{hms(t)}]({link(video_id, t)})" for t in ts[:POR_TERMINO])
            mas = f" y {len(ts) - POR_TERMINO} más" if len(ts) > POR_TERMINO else ""
            lineas.append(f"- **{termino}** ({len(ts)}): {links}{mas}")
        lineas.append("")
    if not hallazgos:
        lineas += ["Sin menciones de los patrones buscados.", ""]
    lineas += [f"Texto completo: `{HF_DIR}/{video_id}.txt` en el dataset.", "", f"_{AVISO}_"]
    return "\n".join(lineas)


def comentar(video_id, cuerpo, repo, token):
    """Comenta en el Issue que abrió concejo_videos.py para ese video. False si no lo encuentra."""
    url = f"https://api.github.com/repos/{repo}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    r = requests.get(f"{url}/issues", headers=headers, timeout=30,
                     params={"labels": LABEL, "state": "all", "per_page": 50})
    r.raise_for_status()
    issue = next((i for i in r.json() if f"watch?v={video_id}" in (i.get("body") or "")), None)
    if not issue:
        return False
    requests.post(f"{url}/issues/{issue['number']}/comments", headers=headers, timeout=30,
                  json={"body": cuerpo}).raise_for_status()
    return True


def procesar(video_id, args, concejales):
    with tempfile.TemporaryDirectory() as tmp:
        audio = args.audio or bajar_audio(video_id, tmp)
        limite = time.time() + args.minutos * 60 if args.minutos else None
        res = transcribir(audio, args.modelo, limite)
    j, t = guardar(video_id, res, args.salida)
    print(f"{video_id}: {len(res['segments'])} segmentos ({res['modelo']}, completo={res['completo']}) en {j.parent}")
    cuerpo = comentario(video_id, analizar(res["segments"], concejales), res)
    if args.sin_subir:
        print(cuerpo)
        return
    from hf_dataset_store import DATASET_REPO_ID, _upload_with_retry, api
    for f in (j, t):
        if not _upload_with_retry(api(), str(f), f"{HF_DIR}/{f.name}", DATASET_REPO_ID, "dataset",
                                  f"Transcripción {video_id}"):
            raise SystemExit(f"No se pudo subir {f.name} al dataset.")
    repo, token = os.getenv("GITHUB_REPOSITORY", ""), os.getenv("GITHUB_TOKEN", "")
    if repo and token and not comentar(video_id, cuerpo, repo, token):
        print(f"No encontré el Issue de {video_id}: el resumen queda solo en el log.\n{cuerpo}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("videos", nargs="+")
    ap.add_argument("--audio", type=Path, help="audio ya bajado (un solo video)")
    ap.add_argument("--modelo", default="medium")
    ap.add_argument("--minutos", type=float, help="corta la transcripción pasados estos minutos y la marca parcial")
    ap.add_argument("--salida", type=Path, default=Path("data/concejo-transcripciones"))
    ap.add_argument("--sin-subir", action="store_true", help="no sube al dataset ni comenta: imprime el resumen")
    args = ap.parse_args()
    concejales = yaml.safe_load((Path(__file__).parent / "concejales.yml").read_text(encoding="utf-8"))["concejales"]
    for v in args.videos:
        procesar(v, args, concejales)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
