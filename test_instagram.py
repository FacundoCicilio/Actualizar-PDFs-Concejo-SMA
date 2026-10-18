import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import anthropic
import httpx2
from PIL import Image

import instagram_carousel as ic


def _item(key, tags, que_dice):
    return {"key": key, "que_dice": que_dice, "title": que_dice, "impact_tags": tags}


DOC = {
    "session_number": "23", "date": "08/10/2026",
    "items": [
        _item("lote", ["🏘 Urbanismo"], "Dictamen: Autorización para el pago del TCI de los lotes 14 y 15 de la Manzana J, a la Sra. Ana Ayala"),
        _item("decl", ["🎭 Cultura/Eventos"], "Declaración de Interés Municipal el Seminario No Arancelado organizado por el Dr. Víctor Borrajo"),
        _item("pedido", ["🚦 Tránsito/Seguridad"], "Dictamen: Solicitud del Sr. David Figueroa de baja de la reserva de estacionamiento"),
        _item("convenio", [], "Dictamen: Convenio con Clubes de esquí para el Programa de Esquí Escolar"),
        _item("taxis", ["💰 Bolsillo"], "Dictamen: Modificación Ordenanza 4166/2001 Tarifa taxis y remisses."),
        _item("agua", ["🚰 Servicios"], "Dictamen: Decreto N° 456/26 para la contratación directa de la Sra. Silvia Antileo para el abastecimiento de agua potable a diversos barrios"),
    ],
}


class AnonimizarTest(unittest.TestCase):
    def test_particulares(self):
        casos = {
            "a favor de la Sra. Andrea Suazo y del Sr. Jorge Suazo.-": "a favor de vecinos.-",
            "Solicitud del Sr. David Figueroa de baja": "Solicitud de un vecino de baja",
            "por parte del vecino Luis De Brun, de soporte": "por parte de un vecino, de soporte",
            "organizado por el Dr. Víctor Borrajo. -": "organizado por un profesional. -",
            "Barrio El Arenal, propiedad de Olga Diana Posse.-": "Barrio El Arenal, propiedad de un particular.-",
            "propiedad de Patricio Daly y María Lina Fantón.-": "propiedad de particulares.-",
            "la participación del joven sanmartinense Pedro Martínez Viademonte.": "la participación de un joven sanmartinense.",
        }
        for original, esperado in casos.items():
            self.assertEqual(ic.anonymize(original), esperado)

    def test_se_mantienen_funcionarios_e_instituciones(self):
        for texto in ["a cargo del Sr. Intendente Municipal Dr. Carlos Saloniti",
                      "Miembro Informante: Concejal Brondo",
                      "los 20 años de la Asociación Puentes de Luz",
                      "propiedad de Municipalidad de San Martín de los Andes"]:
            self.assertEqual(ic.anonymize(texto), texto)


class SeleccionTest(unittest.TestCase):
    def test_solo_alcance_general_y_mayor_impacto_primero(self):
        # fuera: trámite de lotes, declaración de interés y pedido personal; adentro aunque nombre a una contratista: agua a barrios
        self.assertEqual([it["key"] for it in ic.select_items(DOC)], ["taxis", "agua", "convenio"])

    def test_item_con_varios_proyectos_muestra_el_primero(self):
        body = "Declaración de Interés la “Peña del Club Lácar”. – Proyecto 2: - Dictamen s/ Autorización de venta"
        self.assertEqual(ic.slide_text(body), "Declaración de Interés la “Peña del Club Lácar”. (+1 proyecto más)")


class ApiKeyTest(unittest.TestCase):
    # .env de prueba en una carpeta temporal: nunca se toca el .env real del repo
    def _key_con_dotenv(self, contenido, entorno=None):
        with tempfile.TemporaryDirectory() as d:
            dotenv = ic.Path(d) / ".env"
            if contenido is not None:
                dotenv.write_text(contenido, encoding="utf-8")
            with mock.patch.object(ic, "DOTENV", dotenv), mock.patch.dict(os.environ, entorno or {}, clear=True):
                return ic.api_key()

    def test_lee_la_key_del_dotenv(self):
        self.assertEqual(self._key_con_dotenv("# comentario\nOTRA=1\nANTHROPIC_API_KEY=sk-test-123\n"), "sk-test-123")
        self.assertEqual(self._key_con_dotenv('export ANTHROPIC_API_KEY = "sk-test-123"\n'), "sk-test-123")

    def test_vacia_o_sin_archivo_es_ausente(self):
        self.assertIsNone(self._key_con_dotenv("ANTHROPIC_API_KEY=\n"))
        self.assertIsNone(self._key_con_dotenv(None))

    def test_dotenv_sin_la_linea_avisa_sin_mostrar_el_contenido(self):
        with mock.patch.object(ic, "_warned_dotenv", set()), mock.patch("builtins.print") as fake_print:
            self.assertIsNone(self._key_con_dotenv("sk-ant-usr-SECRETO-pegado-sin-nombre\n"))
            self.assertIsNone(self._key_con_dotenv("sk-ant-usr-SECRETO-pegado-sin-nombre\n"))
        self.assertEqual(fake_print.call_count, 1)  # una sola vez por ejecución
        aviso = fake_print.call_args.args[0]
        self.assertIn("ANTHROPIC_API_KEY=", aviso)
        self.assertNotIn("SECRETO", aviso)

    def test_la_variable_de_entorno_gana(self):
        self.assertEqual(self._key_con_dotenv("ANTHROPIC_API_KEY=del-archivo\n", {"ANTHROPIC_API_KEY": "del-entorno"}), "del-entorno")


class CriolloTest(unittest.TestCase):
    def setUp(self):
        # sin esto, con una key real en el .env del repo estos tests llamarían a la API
        patcher = mock.patch.object(ic, "DOTENV", ic.Path(tempfile.gettempdir()) / "no-existe-actas-sma.env")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_sin_api_key_no_hay_linea(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(ic.criollo("Modificación de la tarifa de taxis"))

    def test_si_la_api_falla_no_hay_linea(self):
        cliente = SimpleNamespace(messages=SimpleNamespace(create=mock.Mock(
            side_effect=anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")))))
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}), mock.patch.object(ic, "_client", cliente):
            self.assertIsNone(ic.criollo("Modificación de la tarifa de taxis"))

    def test_respuesta_se_limpia_y_anonimiza(self):
        resp = SimpleNamespace(stop_reason="end_turn",
                               content=[SimpleNamespace(type="text", text="En criollo: “Le cambian el precio al taxi del Sr. Juan Pérez”")])
        create = mock.Mock(return_value=resp)
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}), \
                mock.patch.object(ic, "_client", SimpleNamespace(messages=SimpleNamespace(create=create))):
            self.assertEqual(ic.criollo("Modificación de la tarifa de taxis"), "Le cambian el precio al taxi de un vecino")
        self.assertEqual(create.call_args.kwargs["model"], "claude-haiku-5-5")

    def test_si_da_el_tema_por_aprobado_se_descarta(self):
        for texto in ["Se aprueba la nueva tarifa de taxis.", "El Concejo ya se aprobó el convenio.",
                      "Se modifica la ordenanza de taxis.", "La tarifa se modificó en 2001.", "SE APRUEBA el convenio"]:
            resp = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=texto)])
            with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}), mock.patch("builtins.print"), \
                    mock.patch.object(ic, "_client", SimpleNamespace(messages=SimpleNamespace(create=mock.Mock(return_value=resp)))):
                self.assertIsNone(ic.criollo("Modificación de la tarifa de taxis"), texto)

    def test_tema_a_tratar_se_usa(self):
        texto = "El Concejo trata un cambio en la tarifa de taxis y remises, que afectaría a choferes y pasajeros."
        resp = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=texto)])
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}), \
                mock.patch.object(ic, "_client", SimpleNamespace(messages=SimpleNamespace(create=mock.Mock(return_value=resp)))):
            self.assertEqual(ic.criollo("Modificación de la tarifa de taxis"), texto)

    def test_lineas_todas_o_ninguna_si_la_api_falla(self):
        items = ic.select_items(DOC)
        respuestas = [SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="El Concejo trata algo.")]),
                      anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))]
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}),                 mock.patch.object(ic, "_client", SimpleNamespace(messages=SimpleNamespace(create=mock.Mock(side_effect=respuestas)))):
            lineas, error = ic.criollo_lines(items)
        self.assertEqual(lineas, [None, None, None])  # la primera había salido bien, pero no se mezclan
        self.assertIn("APIConnectionError", error)
        self.assertNotIn("test", error)  # nunca la key

    def test_lineas_sin_key_avisa(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            lineas, error = ic.criollo_lines(ic.select_items(DOC))
        self.assertEqual(lineas, [None, None, None])
        self.assertIn("ANTHROPIC_API_KEY", error)

    def test_respuesta_incompleta_no_se_usa(self):
        resp = SimpleNamespace(stop_reason="max_tokens", content=[SimpleNamespace(type="text", text="Le cambian el")])
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}), \
                mock.patch.object(ic, "_client", SimpleNamespace(messages=SimpleNamespace(create=mock.Mock(return_value=resp)))):
            self.assertIsNone(ic.criollo("Modificación de la tarifa de taxis"))


class SalidaTest(unittest.TestCase):
    def test_caption_anonimizado(self):
        cap = ic.build_caption(DOC, ic.select_items(DOC))
        self.assertIn("Sesión N° 23 · 8 de octubre de 2026", cap)
        self.assertIn("trata 6 puntos", cap)
        self.assertIn("• Modificación Ordenanza 4166/2001 Tarifa taxis y remisses.", cap)  # sin "Dictamen:"
        self.assertNotIn("Antileo", cap)
        self.assertIn("contratación directa de una vecina", cap)
        self.assertIn("link en la bio", cap)
        self.assertIn("#SanMartinDeLosAndes", cap)
        self.assertLessEqual(len(cap), 2200)  # límite de Instagram

    def test_placas_1080x1350_con_y_sin_criollo(self):
        for simple in (None, "El Concejo trata un cambio en la tarifa de taxis."):
            with tempfile.TemporaryDirectory() as out:
                paths = ic.render_carousel(DOC, out, simple_lines=[simple] * 3, ext="jpg")
                self.assertEqual(len(paths), 5)  # portada + 3 ítems relevantes + cierre
                for p in paths:
                    with Image.open(p) as img:
                        self.assertEqual(img.size, (1080, 1350))

    def test_sin_items_relevantes_solo_portada_y_cierre(self):
        doc = {**DOC, "items": DOC["items"][:3]}
        with tempfile.TemporaryDirectory() as out:
            self.assertEqual(len(ic.render_carousel(doc, out)), 2)
            self.assertNotIn("Deslizá", (ic.Path(out) / "caption.txt").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
