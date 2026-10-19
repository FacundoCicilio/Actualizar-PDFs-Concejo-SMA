import json
import unittest
from pathlib import Path

import buscador as b
import test_boletin
from actas_parser import process_pdf

TESTS = Path(__file__).with_name("tests")
PERIODO = json.loads((TESTS / "prensa_notas_2026-06_10.json").read_text(encoding="utf-8"))
SO01 = process_pdf((TESTS / "orden-del-dia-01-2026.pdf").read_bytes(), "Orden-del-Dia-de-la-S.O.-No-01-26.pdf")
RECORDS = (b.build_search_records_from_acta(SO01, "so01") + b.prensa_records(PERIODO)
           + b.boletin_records(test_boletin.store()))


def notas(hits):
    return {h["id"] for h in hits if h["level"] == "prensa"}


class BuscadorTest(unittest.TestCase):
    def test_saloniti_en_todas_las_fuentes_y_con_variantes(self):
        for q in ("Saloniti", "Salonitti", "SALONITÍ", "saloniti"):
            hits = b.search_records(RECORDS, q)
            self.assertEqual(notas(hits), {1270, 1505, 1552}, q)  # también las notas políticas: el buscador muestra todo
            self.assertTrue(any(h["level"] == "acta" and h["doc_id"] == "so01" for h in hits), q)  # apertura, S.O. N° 1
        nota = next(h for h in b.search_records(RECORDS, "Salonitti") if h.get("id") == 1552)
        self.assertEqual((nota["fuente"], nota["fecha"]), ("Notas de Prensa", "04/09/2026"))
        self.assertIn("Saloniti", nota["_snippet"])

    def test_letras_dobles_si_cifras_no(self):
        self.assertEqual(b.squash("salonitti carroza"), "saloniti caroza")
        self.assertEqual(b.squash("100 2026"), "100 2026")

    def test_filtro_por_fuente(self):
        hits = b.search_records(RECORDS, "Saloniti", fuentes=("Notas de Prensa",))
        self.assertEqual({h["fuente"] for h in hits}, {"Notas de Prensa"})

    def test_ordenanzas(self):
        [h] = [h for h in b.search_records(RECORDS, "Club Atlético Cumbres") if h["level"] == "ordenanza"]
        self.assertEqual((h["ordenanza"], h["boletin"], h["sesion"], h["fecha"]), ("15.740/26", 826, "Sesión N° 21", "02/10/2026"))
        self.assertIn("Cumbres", h["_snippet"])

    def test_ordenanza_de_particular_solo_numero_y_fecha(self):
        for q in ("15.751", "Ordenanza 15751"):
            [h] = [h for h in b.search_records(RECORDS, q) if h["level"] == "ordenanza"]
            self.assertTrue(h["particular"])
            self.assertEqual((h["ordenanza"], h["fecha"], h["_snippet"], h["item_title"]), ("15.751/26", "02/10/2026", "", ""))
        # el contenido no se guarda: buscar por lo que dice no la encuentra
        self.assertFalse([h for h in b.search_records(RECORDS, "PRUEBA") if h["level"] == "ordenanza"])

    def test_quisiste_decir(self):
        self.assertEqual(b.search_records(RECORDS, "Salonetti"), [])
        self.assertEqual(b.suggest(RECORDS, "Salonetti"), "saloniti")
        self.assertIsNone(b.suggest(RECORDS, "xqzwv"))
        self.assertIsNone(b.suggest(RECORDS, "Saloniti"))  # ya está: no hay nada que sugerir


if __name__ == "__main__":
    unittest.main()
