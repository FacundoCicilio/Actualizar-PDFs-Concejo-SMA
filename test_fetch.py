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


if __name__ == "__main__":
    unittest.main()
