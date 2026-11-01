import unittest
from datetime import date
from unittest import mock

import requests

import health_check as hc


class ControlTest(unittest.TestCase):
    def test_pdf_de_la_pagina_sin_indexar(self):
        index = [{"source_url": "https://x/24.pdf"}]
        self.assertEqual(hc.missing_links(["https://x/24.pdf", "https://x/25.pdf"], index), ["https://x/25.pdf"])

    def test_expediente_sin_guiones_perdido(self):
        # Caso real de la S.O. 25/2026: el cuadro tarifario de agua no quedó como ítem.
        text = "1445/2026 Tema: Dictamen s/ cuadro tarifario\n1525/2026 --- Tema: Dictamen s/ otro"
        self.assertEqual(hc.lost_expedientes(text, [{"key": "1525/2026", "title": "otro"}]), ["1445/2026"])

    def test_clave_municipal_no_es_perdida(self):
        # Caso real de la S.O. 8/2026: el ítem se guarda con el expediente municipal, no con el del proyecto.
        text = "429/2026 --- Tema: Dictamen s/ Licencia comercial a término a favor del Sr."
        items = [{"key": "05001-112/2024", "title": "Dictamen s/ Licencia comercial a termino a favor del Sr. X"}]
        self.assertEqual(hc.lost_expedientes(text, items), [])


class LaMontanaTest(unittest.TestCase):
    BASE = "https://www.lamontana.com.ar/noticias/"
    HOY = date(2026, 10, 31)
    TARIFAZO = "Aprobaron la actualización de las tarifas de agua y cloacas de San Martín de los Andes"

    def test_ignora_notas_viejas_y_otros_links(self):
        nueva = self.BASE + "2026/10/29/19110-agua"
        otra_url = self.BASE + "2026/10/29/19110-agua-y-cloacas"
        urls = [nueva, otra_url, self.BASE + "2026/08/06/17416-vieja", "https://www.lamontana.com.ar/politica"]
        self.assertEqual(hc.recent_news(urls, self.HOY), [nueva])

    def test_mencion_sin_item(self):
        # Caso real: el tarifazo de la S.O. 25/2026 no quedó como ítem.
        self.assertFalse(hc.has_item(self.TARIFAZO, [{"title": "Licencia comercial a término", "que_dice": "Licencia"}]))

    def test_mencion_con_item(self):
        items = [{"title": "Proyecto de Ordenanza s/ cuadro tarifario de Agua Potable y Saneamiento, cloacas", "que_dice": ""}]
        self.assertTrue(hc.has_item(self.TARIFAZO, items))

    def test_texto_de_aprobacion(self):
        self.assertTrue(hc.APROBADO_PAT.search("El Concejo Deliberante de San Martín de los Andes aprobó una actualización"))
        self.assertIsNone(hc.APROBADO_PAT.search("El Concejo Deliberante recibió a los vecinos."))

    def test_si_el_sitio_falla_no_hay_advertencias(self):
        with mock.patch.object(hc.session, "get", side_effect=requests.ConnectionError()), mock.patch.object(hc, "log"):
            self.assertEqual(hc.check_news([], self.HOY), [])


class TokenTest(unittest.TestCase):
    HOY = date(2026, 11, 1)

    def avisos(self, vence, hoy=HOY):
        with mock.patch.dict("os.environ", {"IG_TOKEN_EXPIRES": vence}, clear=True):
            return hc.check_token_expiry(hoy)

    def test_avisa_desde_siete_dias_antes(self):
        self.assertEqual(self.avisos("2026-12-31", date(2026, 12, 23)), [])
        self.assertIn("31/12/2026", self.avisos("2026-12-31", date(2026, 12, 24))[0])

    def test_sigue_avisando_si_ya_vencio(self):
        self.assertIn("31/10/2026", self.avisos("2026-10-31")[0])

    def test_sin_fecha_valida_avisa_que_falta(self):
        for vence in ("", "1/12/2026"):
            self.assertIn("IG_TOKEN_EXPIRES", self.avisos(vence)[0])


class AlertaTest(unittest.TestCase):
    def correr(self, problems=(), warnings=()):
        with mock.patch.object(hc, "load_index", return_value=[]), mock.patch.object(hc, "log"), \
                mock.patch.object(hc, "check_links", return_value=list(problems)), \
                mock.patch.object(hc, "check_items", return_value=[]), \
                mock.patch.object(hc, "check_news", return_value=list(warnings)),                 mock.patch.object(hc, "check_token_expiry", return_value=[]), \
                mock.patch.object(hc, "send_alert") as send:
            return hc.main(), send

    def test_sin_secrets_no_manda_y_avisa(self):
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(hc, "log"):
            self.assertFalse(hc.send_alert(["x"]))

    def test_con_problemas_el_run_queda_en_rojo(self):
        code, send = self.correr(problems=["falta"])
        self.assertEqual(code, 1)
        send.assert_called_once_with(["falta"])

    def test_la_advertencia_va_al_mail_sin_poner_en_rojo(self):
        code, send = self.correr(warnings=["ojo"])
        self.assertEqual(code, 0)
        send.assert_called_once_with(["ojo"])

    def test_sin_problemas_no_hay_mail(self):
        code, send = self.correr()
        self.assertEqual(code, 0)
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
