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
            if any(re.search(r"\d+/\d+$", t) for _, t in textos):  # placa con pie (número de página)
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

    def test_puesta_al_dia(self):
        from datetime import date
        sesiones = prensa.sessions(NOTAS)
        notas = [(n, s) for key in ("2026-O18", "2026-O23") for s in [sesiones[key]] for n in prensa.rank(s["notes"])]
        for tipo, texto in TEXTOS.items():
            items = [{"sdoc": prensa.session_doc(s), "titular": texto if tipo == "largo" else "El Concejo modificó la Tasa COM",
                      "line": texto, "impact_tags": n["impact_tags"]} for n, s in notas]
            self.verificar(f"puesta al día, {tipo}",
                           lambda out: ic.render_puesta(date(2026, 6, 1), date(2026, 10, 18), items, out))

    def test_si_no_entra_achica_o_recorta(self):
        blocks = [ic.text_block(LARGO, (72, 60, 48), ic.INK, 44, 900, bold=True),
                  ic.text_block(LARGO, (40, 36), ic.MUTED, 44, 600)] + ic.impact_blocks(["💰 Bolsillo"], LARGO)
        dibujo = ic.layout(blocks, ic.CONTENT_BOTTOM - ic.CONTENT_TOP)
        self.assertLessEqual(sum(ic.block_height(b) for b in dibujo) - dibujo[-1][5], ic.CONTENT_BOTTOM - ic.CONTENT_TOP)
        self.verificar("placa con todo al máximo", lambda out: ic.content_slide("ORDEN DEL DÍA", "Sesión N° 18", blocks, "Ref. 1492/2026", "9/10")
                       .save(Path(out) / "x.png"))


if __name__ == "__main__":
    unittest.main()
