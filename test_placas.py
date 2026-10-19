# Placas reales de las sesiones 18 y 23 (orden del día, "Qué se aprobó", destacado y puesta al día), con textos
# normales y con textos al límite: ningún texto puede pisar a otro ni a la marca o los logos (en particular, el
# contenido nunca invade el pie), y la marca va siempre dentro de la zona del pie.
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import ImageDraw

import instagram_carousel as ic
import prensa
from actas_parser import process_pdf

TESTS = Path(__file__).with_name("tests")
NOTAS = json.loads((TESTS / "prensa_notas.json").read_text(encoding="utf-8"))
LARGO = ("El Concejo Deliberante aprobó una modificación integral del régimen tarifario municipal que alcanza a "
         "contribuyentes, comercios, prestadores turísticos e instituciones intermedias de todos los barrios ") * 3
TEXTOS = {"corto": "El Concejo aprobó bajar la tasa comercial.", "largo": ic.shorten(LARGO, 260)}


class TextosDibujados:
    """Registra, por placa, la caja de cada texto dibujado y de cada imagen de marca pegada (marca del pie y logos)."""

    def __init__(self):
        self.por_placa, self.imagenes = {}, {}  # por id de la placa
        self._vivas = []  # mantiene vivas las placas: su id no se reusa
        self._original = ImageDraw.ImageDraw.text
        self._paste = ic.paste_asset

    def __enter__(self):
        registro = self

        def text(draw, xy, txt, *args, **kw):
            box = draw.textbbox(xy, txt, font=kw.get("font"))
            registro.placa(draw._image).append((box, txt))
            return registro._original(draw, xy, txt, *args, **kw)

        def paste_asset(img, im, xy):
            box = registro._paste(img, im, xy)
            registro.placa(img)
            registro.imagenes.setdefault(id(img), []).append(box)
            return box

        self._patches = [mock.patch.object(ImageDraw.ImageDraw, "text", text), mock.patch.object(ic, "paste_asset", paste_asset)]
        for p in self._patches:
            p.start()
        return self

    def placa(self, img):
        if id(img) not in self.por_placa:
            self._vivas.append(img)
            self.por_placa[id(img)] = []
        return self.por_placa[id(img)]

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


def se_pisan(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


class SinSuperposicionTest(unittest.TestCase):
    def verificar(self, nombre, render):
        with TextosDibujados() as reg, tempfile.TemporaryDirectory() as out:
            render(out)
        self.assertTrue(reg.por_placa, nombre)
        for placa, textos in reg.por_placa.items():
            imagenes = reg.imagenes.get(placa, [])
            marcas = [b for b in imagenes if (b[2] - b[0]) > 3 * (b[3] - b[1])]  # la marca es apaisada; el logo del cierre, no
            pie = [b for b in marcas if b[1] >= ic.CONTENT_BOTTOM]
            for i, (a, ta) in enumerate(textos):
                self.assertTrue(0 <= a[0] and a[2] <= ic.W and 0 <= a[1] and a[3] <= ic.H, f"{nombre}: '{ta}' sale de la placa")
                self.assertNotIn("OBSERVATORIO DE ACTAS", ta.upper(), f"{nombre}: la firma de texto se reemplazó por la marca")
                for b, tb in textos[i + 1:]:
                    self.assertFalse(se_pisan(a, b), f"{nombre}: '{ta}' se superpone con '{tb}'")
                for b in imagenes:
                    self.assertFalse(se_pisan(a, b), f"{nombre}: '{ta}' se superpone con la marca o un logo en {b}")
            for b in imagenes:
                self.assertTrue(0 <= b[0] and b[2] <= ic.W and 0 <= b[1] and b[3] <= ic.H, f"{nombre}: imagen fuera de la placa {b}")
            for b in marcas:  # en el pie (placas de contenido) o arriba, sobre la línea de la barra (portadas)
                self.assertTrue(b in pie or b[3] <= 130, f"{nombre}: la marca {b} no está ni en el pie ni arriba")
            for b in pie:
                self.assertEqual(b[3] - b[1], ic.MARK_H)
            # placa con pie (número de página abajo; "Ordenanza 15.740/26" en una portada no lo es)
            if any(re.search(r"\d+/\d+$", t) and a[1] >= ic.CONTENT_BOTTOM for a, t in textos):
                self.assertEqual(len(pie), 1, f"{nombre}: placa con pie sin marca")

    def test_orden_del_dia(self):
        for n in (18, 23):
            doc = process_pdf((TESTS / f"orden-del-dia-{n}-2026.pdf").read_bytes(), f"orden-del-dia-{n}-2026.pdf")
            k = len(ic.select_items(doc))
            self.assertGreater(k, 0)
            for tipo, linea in [("sin criollo", None), *TEXTOS.items()]:
                self.verificar(f"orden del día {n}, {tipo}",
                               lambda out: ic.render_carousel(doc, out, simple_lines=[linea] * k))

    def test_que_se_aprobo_y_destacado(self):
        sesiones = prensa.sessions(NOTAS)
        for key in ("2026-O18", "2026-O23"):
            s = sesiones[key]
            sdoc = prensa.session_doc(s)
            notas = prensa.rank(s["notes"])
            for tipo, linea in TEXTOS.items():
                self.verificar(f"qué se aprobó {key}, {tipo}",
                               lambda out: ic.render_aprobado(sdoc, [{**x, "line": linea} for x in notas], out))
            campos = {
                "completo": {"titular": "Baja la tasa comercial", "que_cambia": TEXTOS["corto"],
                             "a_quien_afecta": "A los comercios.", "desde_cuando": "De agosto a diciembre de 2026."},
                "largo": {"titular": ic.shorten(LARGO, 120), "que_cambia": TEXTOS["largo"],
                          "a_quien_afecta": TEXTOS["largo"], "desde_cuando": TEXTOS["largo"]},
                "sin datos": {"titular": "Baja la tasa comercial", "que_cambia": "", "a_quien_afecta": "", "desde_cuando": ""},
            }
            # el dibujo del destacado solo depende de las etiquetas: una nota por cada combinación (sean destacables o no)
            for nota in {tuple(x["impact_tags"][:2]): x for x in notas}.values():
                for tipo, c in campos.items():
                    self.verificar(f"destacado {key} nota {nota['id']}, {tipo}",
                                   lambda out: ic.render_destacado(sdoc, c, nota["impact_tags"], out))
            for tipo, c in campos.items():  # del Boletín Oficial: número de ordenanza en la portada y arriba de la placa
                self.verificar(f"destacado {key} ordenanza, {tipo}",
                               lambda out: ic.render_destacado(sdoc, c, ["Tierras"], out,
                                                               ordenanza={"label": "15.740/26", "boletin": 826}))

    def test_puesta_al_dia(self):
        from datetime import date
        sesiones = prensa.sessions(NOTAS)
        notas = [(n, s) for key in ("2026-O18", "2026-O23") for s in [sesiones[key]] for n in prensa.rank(s["notes"])]
        for tipo, texto in TEXTOS.items():
            items = [{"sdoc": prensa.session_doc(s), "titular": texto if tipo == "largo" else "El Concejo modificó la Tasa COM",
                      "line": texto, "impact_tags": ["Tierras"] + n["impact_tags"]} for n, s in notas]
            items[-1] = {"titular": "Servicios: 3 decisiones", "lines": [texto] * 3, "impact_tags": ["🚰 Servicios"]}
            items[0]["ordenanza"] = {"label": "15.740/26", "boletin": 826}
            items[1] = {"titular": "Se adjudicaron 6 lotes en Chacra 4 a familias", "impact_tags": ["Tierras"],
                        "lines": [] if tipo != "largo" else [texto],
                        "kicker": "TIERRAS · BOLETÍN OFICIAL", "ref": "Fuente: Boletín Oficial N° 821, 826"}
            auds = [{"line": texto if tipo == "largo" else "Comodato de la cancha Elías Sapag", "fecha": date(2026, 8, 10)}] * 8
            self.verificar(f"puesta al día, {tipo}",
                           lambda out: ic.render_puesta(date(2026, 6, 1), date(2026, 10, 18), items, out, audiencias=auds))

    def test_portada_de_la_puesta_cuenta_decisiones(self):
        from datetime import date
        sdoc = prensa.session_doc(prensa.sessions(NOTAS)["2026-O23"])
        items = [{"sdoc": sdoc, "titular": "Una", "line": "x", "impact_tags": []},
                 {"notes": [{"id": i} for i in range(6)], "titular": "Tierras: 6 decisiones", "lines": ["x"] * 6},
                 {"notes": [{"id": i} for i in range(6)], "titular": "Se adjudicaron 7 lotes en Chacra 4 a familias",
                  "lines": []}]  # adjudicaciones: 6 ordenanzas, sin líneas
        self.assertEqual(ic.decisions(items), 13)
        auds = [{"line": "Propuesta de comodato", "fecha": date(2026, 8, 10)}] * 5  # las audiencias no cuentan
        with TextosDibujados() as reg, tempfile.TemporaryDirectory() as out:
            ic.render_puesta(date(2026, 6, 1), date(2026, 10, 18), items, out, audiencias=auds)
        textos = [t for placa in reg.por_placa.values() for _, t in placa]
        self.assertIn("13 decisiones  →", textos)
        self.assertEqual(ic.decisions(items[:1]), 1)

    def test_historia(self):
        """1080x1920, marca y frase dentro de la zona segura, la frase debajo de la portada; los cuatro tipos."""
        from datetime import date
        from PIL import Image
        s = prensa.sessions(NOTAS)["2026-O23"]
        sdoc = prensa.session_doc(s)
        campos = {"titular": "Baja la tasa comercial", "que_cambia": "", "a_quien_afecta": "", "desde_cuando": ""}
        od = {"session_number": "23", "session_year": 2026, "date": "08/10/2026", "items": [
            {"key": "1492/2026", "que_dice": "Modificación Ordenanza 4166/2001 Tarifa taxis y remisses.",
             "title": "Modificación Ordenanza 4166/2001 Tarifa taxis y remisses.", "impact_tags": ["💰 Bolsillo"]}]}
        renders = {
            "orden del día": lambda out: ic.render_carousel(od, out, simple_lines=[None]),
            "qué se aprobó": lambda out: ic.render_aprobado(sdoc, [{**s["notes"][0], "line": TEXTOS["corto"]}], out),
            "destacado": lambda out: ic.render_destacado(sdoc, campos, ["💰 Bolsillo"], out),
            "puesta al día": lambda out: ic.render_puesta(date(2026, 6, 1), date(2026, 10, 18), [], out),
        }
        frases = [ic.STORY_TEXT, ic.story_text_puesta(date(2026, 6, 1), date(2026, 10, 18)), ic.shorten(LARGO, 160)]
        top, bottom = ic.STORY_SAFE
        for tipo, render in renders.items():
            for frase in frases:
                with tempfile.TemporaryDirectory() as out:
                    cover = render(out)[0]
                    with TextosDibujados() as reg:
                        path = ic.render_story(cover, Path(out) / "historia.jpg", frase)
                    with Image.open(path) as im:
                        self.assertEqual(im.size, (ic.STORY_W, ic.STORY_H))
                        fondo = im.getpixel((5, ic.STORY_H // 2))
                [(placa, textos)] = reg.por_placa.items()
                self.assertNotIn(placa, reg.imagenes)  # solo la marca de la portada, no otra arriba
                card_bottom = ic.STORY_CARD_Y + round(ic.H * 0.85)
                self.assertGreaterEqual(ic.STORY_CARD_Y, top)
                if frase != frases[-1]:  # las frases reales entran enteras
                    self.assertEqual(" ".join(t for _, t in textos), frase)
                for box, t in textos:
                    self.assertTrue(card_bottom <= box[1] and box[3] <= bottom and 0 <= box[0] and box[2] <= ic.STORY_W,
                                    f"historia {tipo}: '{t}' fuera de la zona segura o sobre la portada")
                self.assertLess(sum(fondo), 3 * 200, tipo)  # fondo oscuro: la frase blanca se lee

    def test_si_no_entra_achica_o_recorta(self):
        blocks = [ic.text_block(LARGO, (72, 60, 48), ic.INK, 44, 900, bold=True),
                  ic.text_block(LARGO, (40, 36), ic.MUTED, 44, 600)] + ic.impact_blocks(["💰 Bolsillo"], LARGO)
        dibujo = ic.layout(blocks, ic.CONTENT_BOTTOM - ic.CONTENT_TOP)
        self.assertLessEqual(sum(ic.block_height(b) for b in dibujo) - dibujo[-1][5], ic.CONTENT_BOTTOM - ic.CONTENT_TOP)
        self.verificar("placa con todo al máximo", lambda out: ic.content_slide("ORDEN DEL DÍA", "Sesión N° 18", blocks, "Ref. 1492/2026", "9/10")
                       .save(Path(out) / "x.png"))


if __name__ == "__main__":
    unittest.main()
