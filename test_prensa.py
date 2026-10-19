import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image, ImageDraw

import instagram_carousel as ic
import prensa
from test_instagram import DOC

# Notas reales de https://prensacd.cdsma.gob.ar/prensa/ (API de WordPress), tal como las guarda prensa.fetch().
NOTAS = json.loads(Path(__file__).with_name("tests").joinpath("prensa_notas.json").read_text(encoding="utf-8"))


def key_of(note_id):
    s = prensa.session_of(NOTAS[str(note_id)])
    return s and s["key"]


class SesionTest(unittest.TestCase):
    def test_aprobadas_con_cada_verbo(self):
        casos = {
            1508: "2026-O18",  # "aprobó, en la 18ª Sesión Ordinaria"
            1857: "2026-O18",  # "fue declarado" (publicada en octubre, aprobada en agosto)
            1818: "2026-O23",  # "distinguió" / "otorgó la Distinción al Mérito"
            1815: "2026-O23",  # "fue declarada de interés municipal"
            1753: "2026-O22",  # "La declaración fue aprobada durante la 22ª"
            1777: "2026-O22",  # "exime del 70%"
            1782: "2026-O22",  # "Lo hizo el jueves 1 de octubre … durante la 22ª": el verbo está en el título
            1758: "2026-O22",
        }
        for note_id, key in casos.items():
            self.assertEqual(key_of(note_id), key, NOTAS[str(note_id)]["title"])

    def test_excluidas(self):
        for note_id in (1351,   # "tratará, durante la 17ª" (el título dice "declaró")
                        1552,   # "rechazó" en la Sesión Especial N.° 1
                        1621,   # Concejo Deliberante Estudiantil
                        1763,   # audiencia pública
                        1556):  # convocatoria a audiencia pública
            self.assertIsNone(key_of(note_id), NOTAS[str(note_id)]["title"])

    def test_formatos_de_sesion(self):
        base = {"id": 1, "date": "2026-09-04T10:00:00", "title": "Nota"}
        casos = {
            "aprobó, en la Sesión Especial N.° 1 del año 2026 realizada este jueves 3 de septiembre, una ordenanza": ("2026-E1", date(2026, 9, 3)),
            "aprobó, durante la Sesión Ordinaria N°20 , realizada el jueves 10 de septiembre, la adhesión": ("2026-O20", date(2026, 9, 10)),
            "autorizó durante la 3º Sesión Ordinaria del año 2025 la venta": ("2025-O3", None),
        }
        for texto, (key, d) in casos.items():
            s = prensa.session_of({**base, "text": texto})
            self.assertEqual((s["key"], s["date"]), (key, d), texto)

    def test_fecha_de_la_sesion_sale_de_cualquier_nota(self):
        s = prensa.sessions(NOTAS)
        self.assertEqual(s["2026-O18"]["date"], date(2026, 8, 27))  # la nota de la tasa no la dice; la del Plan de Deudas sí
        self.assertEqual(s["2026-O23"]["date"], date(2026, 10, 8))

    def test_destacados_y_orden(self):
        s = prensa.sessions(NOTAS)
        ids = [n["id"] for n in prensa.rank(s["2026-O18"]["notes"])]
        self.assertEqual(ids[:2], [1508, 1482])  # tasa comercial y plan de deudas: Bolsillo, alcance general
        destacados = {n["id"] for ss in s.values() for n in ss["notes"] if prensa.is_destacado(n)}
        self.assertTrue({1508, 1482, 1777} <= destacados)
        self.assertFalse({1818, 1815, 1857, 1758} & destacados)  # distinciones y declaraciones de interés

    def test_educacion(self):
        self.assertIn("📚 Educación", prensa.impact_tags(NOTAS["1531"]))  # cesión de tierras para la EPET N°30

    def test_reconocimientos_quedan_solo_en_cultura(self):
        # nombran escuelas de paso ("cooperadoras escolares", "estudiantes de la EPET"), pero no cambian nada en ellas
        for note_id in (1818, 1758, 1787):  # distinción a CIMAS, Embajadores Educativos, interés municipal Legado ECO (EPET 21)
            self.assertEqual(prensa.impact_tags(NOTAS[str(note_id)]), ["🎭 Cultura/Eventos"], NOTAS[str(note_id)]["title"])

    def test_texto_para_claude_anonimizado(self):
        texto = prensa.note_text({"title": "Pedido", "text": "Solicitud del Sr. David Figueroa de baja"})
        self.assertNotIn("Figueroa", texto)


class VigenciaTest(unittest.TestCase):
    def test_plan_de_deudas_vencido(self):
        # "Regirá desde el 1 y hasta el 30 de septiembre de 2026"; sessions() le suma las etiquetas de impacto
        plan = next(n for n in prensa.sessions(NOTAS)["2026-O18"]["notes"] if n["id"] == 1482)
        self.assertTrue(prensa.is_destacado(plan))  # Bolsillo: sería destacado...
        self.assertEqual(prensa.vigencia_hasta(plan), date(2026, 9, 30))
        self.assertFalse(prensa.vencida(plan, date(2026, 9, 30)))  # el último día todavía rige
        self.assertTrue(prensa.vencida(plan, date(2026, 10, 1)))   # ...pero desde octubre ya no

    def test_vigentes_o_sin_plazo(self):
        self.assertEqual(prensa.vigencia_hasta(NOTAS["1508"]), date(2026, 12, 31))  # "hasta diciembre de 2026"
        self.assertFalse(prensa.vencida(NOTAS["1508"], date(2026, 10, 18)))
        self.assertIsNone(prensa.vigencia_hasta(NOTAS["1777"]))  # la exención no informa plazo

    def test_formatos(self):
        base = {"date": "2026-08-20T10:00:00"}
        casos = {
            "Rige del 1 al 30 de septiembre.": date(2026, 9, 30),  # sin año: el de la nota
            "La inscripción vence el 5 de octubre de 2026.": date(2026, 10, 5),
            "Aplica de agosto a diciembre de 2026.": date(2026, 12, 31),
            "Del 1 al 15 de septiembre o hasta el 31 de marzo de 2027.": date(2027, 3, 31),  # la más tardía
            "La ordenanza entrará en vigencia a partir de su publicación.": None,
        }
        for texto, esperado in casos.items():
            self.assertEqual(prensa.vigencia_hasta({**base, "text": texto}), esperado, texto)


def respuesta(texto):
    return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=texto)])


class ClaudeNotasTest(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(ic, "DOTENV", ic.Path(tempfile.gettempdir()) / "no-existe-actas-sma.env"),
                  mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"})):
            p.start()
            self.addCleanup(p.stop)
        self.nota = prensa.note_text(NOTAS["1508"])

    def cliente(self, texto):
        create = mock.Mock(return_value=respuesta(texto))
        return mock.patch.object(ic, "_client", SimpleNamespace(messages=SimpleNamespace(create=create))), create

    def test_cifras_que_no_estan_en_la_nota(self):
        self.assertTrue(ic.only_note_numbers("Baja un 35% y el punto queda en $4.440 hasta diciembre de 2026.", self.nota))
        self.assertFalse(ic.only_note_numbers("Baja un 40% la tasa comercial.", self.nota))

    def test_linea_con_cifra_inventada_se_descarta(self):
        patch, _ = self.cliente("El Concejo aprobó bajar un 40% la tasa comercial.")
        with patch:
            self.assertIsNone(ic.note_line(self.nota))
        patch, _ = self.cliente("El Concejo aprobó bajar un 35% la tasa comercial hasta fin de año.")
        with patch:
            self.assertEqual(ic.note_line(self.nota), "El Concejo aprobó bajar un 35% la tasa comercial hasta fin de año.")

    def test_destacado_no_inventa(self):
        datos = {"titular": "El Concejo modificó la Tasa COM y la tasa ENSATUR", "que_cambia": "La tasa comercial baja un 35%.",
                 "a_quien_afecta": "Comercios del Sr. Juan Pérez", "desde_cuando": "Desde marzo de 2031."}
        patch, create = self.cliente(json.dumps(datos))
        with patch:
            campos = ic.destacado_fields(self.nota)
        self.assertEqual(campos["que_cambia"], "La tasa comercial baja un 35%.")
        self.assertEqual(campos["a_quien_afecta"], "Comercios de un vecino")
        self.assertEqual(campos["desde_cuando"], "")  # 2031 no está en la nota: "la nota no lo informa"
        self.assertEqual(create.call_args.kwargs["output_config"]["format"]["type"], "json_schema")

    def test_titular_valorativo_o_solo_una_baja_se_descarta(self):
        # la nota baja la Tasa COM y duplica la ENSATUR
        for titular in ("Alivio fiscal para los comercios", "Un beneficio para el turismo", "Golpe a los comercios",
                        "Baja la tasa comercial", "El Concejo duplicó la tasa ENSATUR"):
            patch, create = self.cliente(json.dumps({"titular": titular, "que_cambia": "", "a_quien_afecta": "", "desde_cuando": ""}))
            with patch, mock.patch("builtins.print"):
                self.assertIsNone(ic.destacado_fields(self.nota), titular)
            self.assertEqual(create.call_count, 2)  # se pide otro titular antes de descartar
        for titular in ("El Concejo modificó la Tasa COM y la tasa ENSATUR para comercios",
                        "Baja la Tasa COM y se duplica la tasa ENSATUR"):
            self.assertTrue(ic.titular_ok(titular, self.nota), titular)
        self.assertTrue(ic.titular_ok("Baja la tasa de habilitación", "El Concejo aprobó reducir un 10% la tasa."))
        # "beneficiarios" describe a quién alcanza, no califica la medida
        self.assertTrue(ic.titular_ok("Exención del 70% para beneficiarios de Neuquén Habita", "exime del 70%"))

    def test_segundo_intento_con_titular_neutral(self):
        malo = {"titular": "Alivio fiscal para comercios", "que_cambia": "", "a_quien_afecta": "", "desde_cuando": ""}
        bueno = {**malo, "titular": "El Concejo modificó la Tasa COM y la tasa ENSATUR"}
        create = mock.Mock(side_effect=[respuesta(json.dumps(malo)), respuesta(json.dumps(bueno))])
        with mock.patch.object(ic, "_client", SimpleNamespace(messages=SimpleNamespace(create=create))), mock.patch("builtins.print"):
            self.assertEqual(ic.destacado_fields(self.nota)["titular"], bueno["titular"])

    def test_periodo_de_vigencia_no_se_borra(self):
        # la nota 1508 dice "de agosto a diciembre de 2026" y "a partir de dicha publicación": los controles lo dejan
        desde = "Rige desde su publicación en el Boletín Oficial Municipal, para los meses de agosto a diciembre de 2026."
        patch, _ = self.cliente(json.dumps({"titular": "El Concejo modificó la Tasa COM y la Tasa ENSATUR", "que_cambia": "",
                                            "a_quien_afecta": "", "desde_cuando": desde}))
        with patch:
            self.assertEqual(ic.destacado_fields(self.nota)["desde_cuando"], desde)

    def test_destacado_sin_titular_no_sale(self):
        patch, _ = self.cliente(json.dumps({"titular": "", "que_cambia": "", "a_quien_afecta": "", "desde_cuando": ""}))
        with patch:
            self.assertIsNone(ic.destacado_fields(self.nota))


class PlacasTest(unittest.TestCase):
    def setUp(self):
        self.s = prensa.sessions(NOTAS)["2026-O18"]
        self.sdoc = prensa.session_doc(self.s)

    def test_que_se_aprobo(self):
        notas = [{**n, "line": "El Concejo aprobó un alivio para los comercios."} for n in prensa.rank(self.s["notes"])]
        with tempfile.TemporaryDirectory() as out:
            paths = ic.render_aprobado(self.sdoc, notas, out, ext="jpg")
            caption = (Path(out) / "caption.txt").read_text(encoding="utf-8")
            sizes = []
            for p in paths:
                with Image.open(p) as img:
                    sizes.append(img.size)
        self.assertEqual(len(paths), len(notas) + 2)
        self.assertEqual(set(sizes), {(1080, 1350)})
        self.assertTrue(caption.startswith("✅ Qué se aprobó · Sesión N° 18 · 27 de agosto de 2026"))
        self.assertIn(ic.FUENTE, caption)
        self.assertLessEqual(len(caption), 2200)

    def test_maximo_8_placas(self):
        notas = [{"line": f"Tema {i}", "impact_tags": []} for i in range(12)]
        with tempfile.TemporaryDirectory() as out:
            self.assertEqual(len(ic.render_aprobado(self.sdoc, notas, out)), 10)  # portada + 8 + cierre

    def test_destacado(self):
        campos = {"titular": "Baja la tasa comercial", "que_cambia": "La tasa baja un 35%.", "a_quien_afecta": "", "desde_cuando": ""}
        with tempfile.TemporaryDirectory() as out:
            paths = ic.render_destacado(self.sdoc, campos, ["💰 Bolsillo"], out)
            caption = (Path(out) / "caption.txt").read_text(encoding="utf-8")
        self.assertEqual(len(paths), 2)
        self.assertIn("¿A quién afecta? La nota no lo informa.", caption)
        self.assertIn(ic.FUENTE, caption)

    def test_destacado_tres_respuestas_mismo_tamano(self):
        campos = {"titular": "x", "que_cambia": "Baja un 35% la Tasa COM.",  # corta: sola, entraría más grande
                  "a_quien_afecta": "Al sector comercial y turístico de San Martín de los Andes, alcanzado por la Tasa "
                                    "COM y la Tasa de Promoción Turística ENSATUR, y a todos los contribuyentes alcanzados.",
                  "desde_cuando": ""}
        respuestas = [campos["que_cambia"], campos["a_quien_afecta"], ic.NO_INFO]
        tamanos = set()
        original = ImageDraw.ImageDraw.text

        def text(draw, xy, txt, *args, **kw):
            if any(txt in r for r in respuestas):
                tamanos.add(kw["font"].size)
            return original(draw, xy, txt, *args, **kw)

        with mock.patch.object(ImageDraw.ImageDraw, "text", text):
            ic.destacado_slide(self.sdoc, campos)
        self.assertEqual(len(tamanos), 1, tamanos)

    def test_orden_del_dia_cierra_con_y_n_temas_mas(self):
        items = ic.select_items(DOC)
        self.assertEqual(ic.more_count(DOC, items), 3)
        self.assertIn("…y 3 temas más.", ic.build_caption(DOC, items))
        self.assertEqual(ic.y_mas(1), "y 1 tema más")


if __name__ == "__main__":
    unittest.main()
