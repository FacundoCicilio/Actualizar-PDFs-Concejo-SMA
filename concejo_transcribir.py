# Transcribe videos del Concejo con faster-whisper, los sube al dataset y comenta los hallazgos en su Issue.
# En Actions (2 vCPU) medium va a ~0,5x tiempo real: el video se parte en tramos que corren en jobs paralelos.
#   python concejo_transcribir.py --plan ID [ID ...]                 # tramos -> GITHUB_OUTPUT "partes"
#   python concejo_transcribir.py --parte ID DESDE HASTA TOTAL       # transcribe un tramo -> partes/ID_DESDE.json
#   python concejo_transcribir.py --unir partes/                     # junta, sube al dataset y comenta el Issue
#   python concejo_transcribir.py ID --audio x.mp3 --sin-subir       # local: video entero, sin subir nada
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
TRAMO = 15 * 60    # por job de Actions; ver el tiempo medido en el workflow
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


def segundos_iso(d):
    """PT2H38M36S -> 9516."""
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", d)
    dd, h, mi, s = (int(x or 0) for x in m.groups())
    return ((dd * 24 + h) * 60 + mi) * 60 + s


def partes(video_id, total, tramo=TRAMO):
    return [{"video": video_id, "desde": d, "hasta": min(d + tramo, total), "total": total}
            for d in range(0, total, tramo)]


def bajar_audio(video_id, carpeta):
    """Solo el audio, sin convertir: pcm() lo decodifica. --download-sections era mucho más lento que bajarlo entero."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    base = ["yt-dlp", "--js-runtimes", "node", "--remote-components", "ejs:github", "-f", "ba",
            "-o", f"{carpeta}/{video_id}.%(ext)s"]
    for extra in ([], ["--extractor-args", "youtube:player_client=android"]):
        if subprocess.run(base + extra + [url]).returncode == 0:
            return next(Path(carpeta).glob(f"{video_id}.*"))
    raise SystemExit(f"yt-dlp no pudo bajar {video_id}, ni con el cliente android.")


def pcm(archivo, desde=0, hasta=None):
    """Audio mono 16 kHz float32 vía ffmpeg: el decodificador de faster-whisper (PyAV) rompe con av>=15."""
    import numpy as np
    corte = ["-ss", str(desde)] + (["-to", str(hasta)] if hasta else [])
    raw = subprocess.run(["ffmpeg", "-v", "error", *corte, "-i", str(archivo), "-f", "s16le", "-ac", "1", "-ar", "16000",
                          "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768


def transcribir(audio, modelo="medium", limite=None, offset=0, hasta=None):
    """{modelo, faltan, segments}. Sin memoria para medium usa small; pasado `limite` corta y lo anota en faltan."""
    from faster_whisper import WhisperModel
    try:
        m = WhisperModel(modelo, device="cpu", compute_type="int8")
    except (MemoryError, RuntimeError) as e:
        if modelo == "small":
            raise
        print(f"No cargó {modelo} ({e}): uso small.")
        m, modelo = WhisperModel("small", device="cpu", compute_type="int8"), "small"
    audio = pcm(audio, offset, hasta)
    out, _ = m.transcribe(audio, language="es", initial_prompt=PROMPT, vad_filter=True, word_timestamps=True)
    segs, faltan = [], []
    for s in out:
        segs.append({"start": round(offset + s.start, 2), "end": round(offset + s.end, 2), "text": s.text.strip()})
        if len(segs) > 1 and int(segs[-1]["start"] // 600) > int(segs[-2]["start"] // 600):
            print(f"  ... {hms(segs[-1]['start'])}", flush=True)
        if limite and time.time() > limite:
            faltan = [[segs[-1]["end"], round(offset + len(audio) / 16000, 2)]]
            break
    return {"modelo": modelo, "faltan": faltan, "segments": segs}


def unir(archivos):
    """{video_id: resultado}. Los tramos que no llegaron (job caído) se suman a "faltan" como [desde, hasta]."""
    por_video = {}
    for f in archivos:
        p = json.loads(Path(f).read_text(encoding="utf-8"))
        por_video.setdefault(p["video"], {})[p["desde"]] = p
    res = {}
    for v, hechos in por_video.items():
        ps = [hechos[d] for d in sorted(hechos)]
        faltan = sorted([[e["desde"], e["hasta"]] for e in partes(v, ps[0]["total"]) if e["desde"] not in hechos]
                        + [f for p in ps for f in p["faltan"]])
        res[v] = {"modelo": "/".join(sorted({p["modelo"] for p in ps})), "completo": not faltan, "faltan": faltan,
                  "segments": [s for p in ps for s in p["segments"]]}
    return res


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
    if res.get("faltan"):
        tramos = ", ".join(f"{hms(a)}–{hms(b)}" for a, b in res["faltan"])
        lineas += [f"**Parcial:** sin transcribir {tramos} (job caído o fuera de tiempo).", ""]
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


def publicar(video_id, res, args, concejales):
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


def plan(videos):
    from concejo_videos import youtube
    key = os.environ["YOUTUBE_API_KEY"]
    todas = []
    for v in youtube("videos", key, part="contentDetails", id=",".join(videos)):
        total = segundos_iso(v["contentDetails"]["duration"])
        if not total:  # YouTube da P0D a un vivo en curso o programado
            print(f"{v['id']}: duración 0 (¿vivo?). Correr a mano cuando termine: workflow_dispatch con video_id.")
        todas += partes(v["id"], total)
    print(f"{len(todas)} tramos de {TRAMO // 60} min para {len(videos)} videos.")
    with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a", encoding="utf-8") as f:
        f.write(f"partes={json.dumps(todas)}\n")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("videos", nargs="*")
    ap.add_argument("--plan", action="store_true", help="calcula los tramos de los videos para el workflow")
    ap.add_argument("--parte", nargs=4, metavar=("ID", "DESDE", "HASTA", "TOTAL"))
    ap.add_argument("--unir", type=Path, help="carpeta con los tramos de --parte")
    ap.add_argument("--audio", type=Path, help="audio ya bajado (un solo video)")
    ap.add_argument("--modelo", default="medium")
    ap.add_argument("--minutos", type=float, help="corta la transcripción pasados estos minutos y la marca parcial")
    ap.add_argument("--salida", type=Path, default=Path("data/concejo-transcripciones"))
    ap.add_argument("--sin-subir", action="store_true", help="no sube al dataset ni comenta: imprime el resumen")
    args = ap.parse_args()
    limite = time.time() + args.minutos * 60 if args.minutos else None
    if args.plan:
        plan(args.videos)
        return 0
    if args.parte:
        v, desde, hasta, total = args.parte[0], *map(int, args.parte[1:])
        with tempfile.TemporaryDirectory() as tmp:
            res = transcribir(bajar_audio(v, tmp), args.modelo, limite, desde, hasta)
        args.salida.mkdir(parents=True, exist_ok=True)
        (args.salida / f"{v}_{desde:06d}.json").write_text(json.dumps(
            {"video": v, "desde": desde, "hasta": hasta, "total": total, **res}, ensure_ascii=False), encoding="utf-8")
        print(f"Tramo {hms(desde)}–{hms(hasta)}: {len(res['segments'])} segmentos, faltan={res['faltan']}.")
        return 0
    concejales = yaml.safe_load((Path(__file__).parent / "concejales.yml").read_text(encoding="utf-8"))["concejales"]
    if args.unir:
        archivos = sorted(args.unir.glob("*.json"))
        if not archivos:
            raise SystemExit(f"No hay tramos en {args.unir}: fallaron todos los jobs de transcripción.")
        for v, res in unir(archivos).items():
            publicar(v, res, args, concejales)
        return 0
    for v in args.videos:
        with tempfile.TemporaryDirectory() as tmp:
            res = transcribir(args.audio or bajar_audio(v, tmp), args.modelo, limite)
        publicar(v, {**res, "completo": not res["faltan"]}, args, concejales)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
