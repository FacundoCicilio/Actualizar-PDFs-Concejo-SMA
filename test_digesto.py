import unittest

import digesto

# Forma real del Boletín 831 y del PDF del Digesto de la 15.816/26 (texto recortado).
HEAD = "ORDENANZA Nº {} PROMULGADA POR DECRETO Nº 2705/26 DE FECHA 29/10/2026\n"
BOLETIN = (HEAD.format("15.816/26") + "ARTÍCULO 1º. AUTORÍZASE ... como Anexo I forma parte de la presente.\n"
           "Anexo I de la Ordenanza 15501/2025 y 7158/2006.\nARTÍCULO 6°. Comuníquese al Departamento Ejecutivo Municipal.\n"
           + HEAD.format("15.817/26") + "ARTÍCULO 1º. APRUÉBASE el convenio que como Anexo I forma parte.\n"
           "ANEXO I\nCONVENIO ...\n")
DIGESTO = ("ARTÍCULO 3°. DETERMÍNASE que dichos suministros no abonarán los cargos establecidos en el Artículo 2 del\n"
           "Anexo I de la Ordenanza 15501/2025 y 7158/2006.\nARTÍCULO 6°. Comuníquese.\n"
           "ANEXO I ORDENANZA N° 15.816/26\nEsquema de actualización. ARTÍCULO 1.- Cronograma. Actualización única y "
           "transitoria total del setenta y nueve por ciento (79 %) sobre régimen vigente.")


class AnexoTest(unittest.TestCase):
    def test_anexo_en_boletin(self):
        self.assertFalse(digesto.anexo_en_boletin(BOLETIN, 15816))  # la cita en minúsculas no es el Anexo
        self.assertTrue(digesto.anexo_en_boletin(BOLETIN, 15817))
        self.assertFalse(digesto.anexo_en_boletin(BOLETIN, 99999))

    def test_anexo_text(self):
        a = digesto.anexo_text(DIGESTO)
        self.assertTrue(a.startswith("ANEXO I ORDENANZA N° 15.816/26"))
        self.assertIn("setenta y nueve por ciento (79 %)", a)
        self.assertEqual(digesto.anexo_text("ARTÍCULO 1º. Sin anexo."), "")


if __name__ == "__main__":
    unittest.main()
