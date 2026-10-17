import re
import unittest
from pathlib import Path

from actas_parser import process_pdf, infer_impact

TESTS = Path(__file__).parent / "tests"
CULTURA = ["🎭 Cultura/Eventos"]


def _parse(fixture, filename):
    # filename = nombre que genera safe_filename_from_url en producción (el "°" pasa a "_")
    return process_pdf((TESTS / fixture).read_bytes(), filename)


def _items_by_key(r):
    return {it["key"]: it for it in r["items"]}


class OrdenesDelDiaRealesTest(unittest.TestCase):
    """PDFs reales del Concejo: si un cambio de regex rompe la detección, falla acá y no en el sitio."""

    @classmethod
    def setUpClass(cls):
        cls.so01 = _parse("orden-del-dia-01-2026.pdf", "Orden-del-Dia-de-la-S.O.-No-01-26.pdf")
        cls.so12 = _parse("orden-del-dia-12-2026.pdf", "PDF-S.O.-N_-12-2026.pdf")
        cls.so23 = _parse("orden-del-dia-23-2026.pdf", "PDF-S.O.-N_-23-2026.pdf")
        cls.todos = (cls.so01, cls.so12, cls.so23)

    def test_metadata(self):
        for r, num, fecha in [(self.so01, "1", "26/02/2026"), (self.so12, "12", "11/06/2026"), (self.so23, "23", "08/10/2026")]:
            self.assertEqual(r["session_type"], "ORDEN DEL DIA")
            self.assertEqual(r["session_number"], num)
            self.assertEqual(r["session_year"], 2026)
            self.assertEqual(r["date"], fecha)

    def test_cantidad_de_items(self):
        # Contados a mano sobre los PDFs. Si cambian, revisar los ítems antes de actualizar el número.
        self.assertEqual(len(self.so01["items"]), 6)
        self.assertEqual(len(self.so12["items"]), 9)
        self.assertEqual(len(self.so23["items"]), 16)

    def test_sin_items_basura(self):
        # encabezados de dictamen ("05001-317/2009 (s/e) 29/09/2026 Miembro Informante: …", "05001-2/2016 05/01/2026 Vives")
        for r in self.todos:
            for it in r["items"]:
                self.assertNotIn("Miembro Informante", it["que_dice"], it["que_dice"])
                self.assertNotRegex(it["que_dice"], r"\d{5}-\d+\s*/\d{4} \d{2}/\d{2}/\d{4}", it["que_dice"])

    def test_sin_nombre_de_comision_al_final(self):
        so23 = _items_by_key(self.so23)
        self.assertNotIn("Vivienda", so23["1492/2026"]["que_dice"])
        self.assertNotIn("🏘 Urbanismo", so23["1492/2026"]["impact_tags"])
        self.assertNotIn("🚦 Tránsito/Seguridad", so23["05000-68/2013"]["impact_tags"])
        self.assertNotIn("Comisiones", so23["1581/2026"]["que_dice"])

    def test_items_que_cruzan_de_pagina_quedan_enteros(self):
        so23 = _items_by_key(self.so23)
        self.assertIn("Aprendiendo en la Nieve 2026", so23["1530/2026"]["que_dice"])
        self.assertIn("Cimas por su extensa trayectoria", so23["1361/2026"]["que_dice"])
        self.assertNotIn("Golpe de Estado", _items_by_key(self.so12)["376/2025"]["que_dice"])  # lema del encabezado

    def test_item_no_se_parte_en_dos(self):
        for r in self.todos:
            keys = [it["key"] for it in r["items"]]
            self.assertEqual(len(keys), len(set(keys)), keys)
        self.assertTrue(_items_by_key(self.so12)["2175/2025"]["que_dice"].startswith("Dictamen: Regularización dominial del lote 9"))

    def test_sin_prefijos_repetidos(self):
        for r in self.todos:
            for it in r["items"]:
                qd = it["que_dice"]
                self.assertNotRegex(qd, r"^[\s—–-]", qd)
                self.assertNotRegex(qd, r"^(?:\w+: )?(?:Tema:|Dictamen s/|Proyecto \d)", qd)
                self.assertIsNone(re.match(r"^([^:]{3,40}): \1", qd), qd)  # "Decreto: Decreto N°…"

    def test_textos_esperados(self):
        so01, so23 = _items_by_key(self.so01), _items_by_key(self.so23)
        self.assertTrue(so01["95/2026"]["que_dice"].startswith("Dictamen: Decreto N° 200/26"))
        self.assertTrue(so23["1621/2026"]["que_dice"].startswith("Declaración de Interés Municipal por los 20 años"))

    def test_declaraciones_de_interes_son_cultura(self):
        items = _items_by_key(self.so23)
        self.assertEqual(items["1521/2026"]["impact_tags"], CULTURA)  # Seminario "No Arancelado"
        self.assertEqual(items["1621/2026"]["impact_tags"], CULTURA)  # "Puentes de Luz": no es obra ni servicio


class InferImpactTest(unittest.TestCase):
    def test_no_arancelado_no_es_bolsillo(self):
        self.assertNotIn("💰 Bolsillo", infer_impact("Curso no arancelado de primeros auxilios")["impact_tags"])

    def test_arancel_real_sigue_siendo_bolsillo(self):
        self.assertIn("💰 Bolsillo", infer_impact("Actualización del arancel de habilitaciones")["impact_tags"])

    def test_sin_tildes(self):
        self.assertIn("🚰 Servicios", infer_impact("Corte de energia en el barrio")["impact_tags"])


if __name__ == "__main__":
    unittest.main()
