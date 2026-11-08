import unittest
from unittest import mock

import concejo_transcribir as ct

SEGS = [
    {"start": 60.0, "end": 65.0, "text": "Pasamos al Dictamen 1445 / 2026 sobre la tarifa de agua."},
    {"start": 125.5, "end": 130.0, "text": "La concejal Petagna pide un pedido de informes por 20 millones."},
    {"start": 200.0, "end": 205.0, "text": "Aprobado por unanimidad. Ordenanza 13250 y ordenanza N° 999."},
    {"start": 300.0, "end": 305.0, "text": "Hay que aguantar. Se pasa a cuarto intermedio. Abstención de Vita."},
    {"start": 400.0, "end": 405.0, "text": "El dictamen 1445/2026 vuelve con giro a comisión."},
]
RES = {"modelo": "medium", "completo": True, "segments": SEGS}


class AnalizarTest(unittest.TestCase):
    def setUp(self):
        self.h = ct.analizar(SEGS, ["Brondo", "Petagna", "Vita", "Durando"])

    def test_dictamen_normalizado_junta_las_dos_menciones(self):
        self.assertEqual(self.h["Dictámenes"], {"1445/2026": [60.0, 400.0]})

    def test_ordenanzas_con_y_sin_numero(self):
        self.assertEqual(self.h["Ordenanzas"], {"ordenanza 13250": [200.0], "ordenanza n° 999": [200.0]})

    def test_votacion(self):
        self.assertEqual(set(self.h["Votación"]),
                         {"aprobado", "unanimidad", "cuarto intermedio", "abstención", "giro a comisión"})

    def test_concejales_y_temas(self):
        self.assertEqual(self.h["Concejales"], {"petagna": [125.5], "vita": [300.0]})
        self.assertEqual(set(self.h["Temas"]), {"tarifa", "agua", "pedido de informes", "millones"})

    def test_agua_no_matchea_aguantar(self):
        self.assertEqual(self.h["Temas"]["agua"], [60.0])

    def test_comentario_con_links_al_minuto_y_aviso(self):
        c = ct.comentario("XYZ", self.h, RES)
        self.assertIn("[0:01:00](https://youtube.com/watch?v=XYZ&t=60s)", c)
        self.assertIn("[0:02:05](https://youtube.com/watch?v=XYZ&t=125s)", c)
        self.assertIn(ct.AVISO, c)
        self.assertNotIn("Parcial", c)

    def test_comentario_parcial_y_recortado(self):
        h = {"Temas": {"agua": [float(i) for i in range(40)]}}
        c = ct.comentario("XYZ", h, {**RES, "completo": False})
        self.assertIn("**Parcial:** se cortó en 0:06:45", c)
        self.assertIn("y 15 más", c)

    def test_comenta_en_el_issue_del_video(self):
        issues = [{"number": 1, "body": "https://www.youtube.com/watch?v=OTRO"},
                  {"number": 7, "body": "https://www.youtube.com/watch?v=XYZ\n..."}]
        with mock.patch.object(ct.requests, "get") as get, mock.patch.object(ct.requests, "post") as post:
            get.return_value.json.return_value = issues
            self.assertTrue(ct.comentar("XYZ", "hola", "a/b", "t"))
        self.assertTrue(post.call_args.args[0].endswith("/issues/7/comments"))

    def test_sin_issue_no_comenta(self):
        with mock.patch.object(ct.requests, "get") as get, mock.patch.object(ct.requests, "post") as post:
            get.return_value.json.return_value = []
            self.assertFalse(ct.comentar("XYZ", "hola", "a/b", "t"))
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
