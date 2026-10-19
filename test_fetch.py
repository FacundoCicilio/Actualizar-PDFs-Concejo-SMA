import unittest

from auto_fetch import canonicalize_url, safe_filename_from_url

BASE = "https://prensacd.cdsma.gob.ar/wp-content/uploads/2026/06/"
EN_LA_PAGINA = BASE + "PDF-S.O.-N°-12-2026.pdf"         # como aparece en el href
DEL_SERVIDOR = BASE + "PDF-S.O.-N%C2%B0-12-2026.pdf"    # como la devuelve requests (r.url) y quedó en el índice


class UrlsTest(unittest.TestCase):
    def test_grado_codificado_y_sin_codificar_son_la_misma_url(self):
        self.assertEqual(canonicalize_url(EN_LA_PAGINA), canonicalize_url(DEL_SERVIDOR))

    def test_se_ignora_el_fragmento(self):
        self.assertEqual(canonicalize_url(DEL_SERVIDOR + "#page=2"), canonicalize_url(EN_LA_PAGINA))

    def test_nombre_de_archivo_estable(self):
        # El doc_id de lo ya guardado se calculó con este nombre: si cambia, todo se re-sube duplicado.
        self.assertEqual(safe_filename_from_url(DEL_SERVIDOR), "PDF-S.O.-N_C2_B0-12-2026.pdf")


class MediosTest(unittest.TestCase):
    """Órdenes del día de sesiones especiales subidas al sitio sin enlazar en la página del año."""

    def media(self, respuesta):
        from types import SimpleNamespace
        from unittest import mock
        import auto_fetch as af

        def get(url, timeout, params):
            if isinstance(respuesta, Exception):
                raise respuesta
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: respuesta.get(params["search"], []))
        with mock.patch.object(af.session, "get", get), mock.patch.object(af, "log", lambda m: None):
            return af.fetch_special_media_links(2026)

    def test_solo_especiales(self):
        m = lambda t, f: {"title": {"rendered": t}, "source_url": "https://prensacd.cdsma.gob.ar/wp-content/uploads/2026/09/" + f}  # noqa: E731
        links = self.media({"especial": [m("Orden del D&iacute;a Sesi&oacute;n Especial N° 1", "SE-N-01-2026.pdf"),
                                         m("COMISIONES ESPECIALES CD 2026", "COMISIONES-ESPECIALES-CD-2026.pdf")],
                            "extraordinaria": [m("Orden del Día de la S.O. N° 18 – 2026", "Orden-del-Dia-de-la-S.O.-N°-18-2026.pdf")]})
        self.assertEqual(links, ["https://prensacd.cdsma.gob.ar/wp-content/uploads/2026/09/SE-N-01-2026.pdf"])

    def test_si_la_api_falla_sigue_con_la_pagina(self):
        import requests
        self.assertEqual(self.media(requests.ConnectionError()), [])


if __name__ == "__main__":
    unittest.main()
