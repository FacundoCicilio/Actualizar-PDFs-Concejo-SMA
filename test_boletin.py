import json
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import boletin
import prensa

FIX = Path(__file__).with_name("tests") / "boletin"
# Texto extraído de los boletines 821 y 826: encabezados y articulado reales; las ordenanzas que nombran particulares
# tienen articulado ficticio con la misma forma ("Sra. Ana PRUEBA", DNI 11.111.1xx, Chacra 4).
TEXTO = {n: boletin.clean((FIX / f"bom-{n}.txt").read_text(encoding="utf-8")) for n in (821, 826)}
URL = "http://boletinoficial.sma.gob.ar/wp-content/uploads/2026/10/bom-{}.pdf"
PERIODO = json.loads((Path(__file__).with_name("tests") / "prensa_notas_2026-06_10.json").read_text(encoding="utf-8"))


def store():
    ords = {}
    for n, t in TEXTO.items():
        ords.update({str(o["numero"]): o for o in boletin.parse(t, n, URL.format(n))})
    return {"boletines": {str(n): {"url": URL.format(n), "size": 1, "sha256": "x"} for n in TEXTO}, "ordenanzas": ords}


def notas(st):
    return {o["numero"]: boletin.as_note(o) for o in st["ordenanzas"].values() if "text" in o}


class DeteccionTest(unittest.TestCase):
    def test_portada(self):
        found = boletin.discover((FIX / "portada.html").read_text(encoding="utf-8"))
        self.assertEqual(sorted(found), [824, 825, 826])  # también la edición especial; no los decretos sueltos
        self.assertTrue(found[824].endswith("bom-824-edicion-especial.pdf"))

    def test_numero_siguiente_en_este_mes_y_el_anterior(self):
        urls = boletin.probe_urls(826, date(2026, 10, 19))
        self.assertIn(f"{boletin.BASE}/wp-content/uploads/2026/10/bom-827.pdf", urls)
        self.assertIn(f"{boletin.BASE}/wp-content/uploads/2026/09/bom-827-edicion-especial.pdf", urls)
        self.assertIn(f"{boletin.BASE}/wp-content/uploads/2025/12/bom-827.pdf", boletin.probe_urls(826, date(2026, 1, 5)))

    def run_load(self, guardado, sitio, head=None):
        """sitio: {url: bytes}; la portada lista lo que está en sitio; head: {url: Content-Length} para los HEAD."""
        portada = "".join(f'<a href="{u}">x</a>' for u in sitio if "bom-827" not in u or (head or {}).get("listar"))
        guardados = []

        def get(url, method="GET"):
            if url == boletin.BASE + "/":
                return SimpleNamespace(text=portada, status_code=200, raise_for_status=lambda: None)
            if method == "HEAD":
                ok = url in sitio
                return SimpleNamespace(status_code=200 if ok else 404,
                                       headers={"Content-Length": str((head or {}).get(url, len(sitio.get(url, b""))))})
            return SimpleNamespace(content=sitio[url], status_code=200, raise_for_status=lambda: None)

        with mock.patch.object(boletin, "_get", get), \
                mock.patch.object(boletin, "load_json", lambda repo, path: json.loads(json.dumps(guardado))), \
                mock.patch.object(boletin, "save_json", lambda repo, path, data, msg: guardados.append(data)), \
                mock.patch.object(boletin, "pdf_text", lambda data: TEXTO[int(data.decode())]):
            return boletin.load(date(2026, 10, 19)), guardados

    def test_boletin_nuevo_desde_la_portada(self):
        st, guardados = self.run_load({"boletines": {}, "ordenanzas": {}}, {URL.format(821): b"821", URL.format(826): b"826"})
        self.assertEqual(sorted(st["boletines"]), ["821", "826"])
        self.assertIn("15740", st["ordenanzas"])
        self.assertEqual(len(guardados), 1)

    def test_sin_novedades_no_guarda(self):
        st = store()
        st["boletines"] = {"826": {"url": URL.format(826), "size": 3, "sha256": "x"}}
        _, guardados = self.run_load(st, {URL.format(826): b"826"})
        self.assertEqual(guardados, [])

    def test_numero_siguiente_que_la_portada_no_lista(self):
        st = {"boletines": {"826": {"url": URL.format(826), "size": 3, "sha256": "x"}}, "ordenanzas": {}}
        nuevo = f"{boletin.BASE}/wp-content/uploads/2026/10/bom-827-edicion-especial.pdf"
        with mock.patch.dict(TEXTO, {827: TEXTO[821]}):
            st, _ = self.run_load(st, {URL.format(826): b"826", nuevo: b"827"})
        self.assertEqual(st["boletines"]["827"]["url"], nuevo)
        self.assertEqual(st["ordenanzas"]["15710"]["boletin"], 827)

    def test_pdf_reemplazado_con_el_mismo_nombre_se_reprocesa(self):
        st = {"boletines": {"826": {"url": URL.format(826), "size": 3, "sha256": "viejo"}}, "ordenanzas": {}}
        st, guardados = self.run_load(st, {URL.format(826): b"826"}, head={URL.format(826): 999})
        self.assertIn("15740", st["ordenanzas"])
        self.assertEqual(len(guardados), 1)


class ExtraccionTest(unittest.TestCase):
    def setUp(self):
        self.ords = {n: {o["numero"]: o for o in boletin.parse(t, n, URL.format(n))} for n, t in TEXTO.items()}

    def test_cantidad_y_numeros(self):
        self.assertEqual(sorted(self.ords[821]), list(range(15707, 15718)))
        self.assertEqual(len(self.ords[826]), 37)
        o = self.ords[826][15740]
        self.assertEqual((o["label"], o["decreto"], o["promulgada"], o["boletin"]), ("15.740/26", 2489, "2026-10-02", 826))
        self.assertEqual(self.ords[826][15770]["promulgada"], "2026-10-28")  # "DE FECHA 028/10/2026" (sic)

    def test_variantes_de_encabezado(self):
        for head in ("ORDENANZA Nº 15.699/2026  \nPROMULGADA POR DECRETO Nº2141  DE FECHA 21/08/2026",
                     "ORDENANZA N° 15699/26 PROMULGADA POR DECRETO N° 2141/26 DE FECHA 21/08/2026"):
            [o] = boletin.parse(f"{head}\nARTÍCULO 1º.- APRUÉBASE el pliego.- ARTÍCULO 2º.- Comuníquese al Departamento "
                                f"Ejecutivo Municipal.-", 820, "u")
            self.assertEqual((o["numero"], o["decreto"], o["label"]), (15699, 2141, "15.699/26"), head)

    def test_anexos_audiencias_y_decretos_no_son_ordenanzas(self):
        texto = TEXTO[826]
        self.assertIn("RESUMEN AUDIENCIA PÚBLICA", texto)
        self.assertNotIn("PRUEBA", self.ords[826][15740]["text"])  # el convenio del Anexo I no es articulado
        self.assertNotIn("ANEXO", self.ords[826][15740]["text"])
        self.assertTrue(self.ords[826][15740]["text"].endswith("Comuníquese al Departamento Ejecutivo Municipal.-"))
        tabla = "2141 Promulga Ordenanza Nº 15.699/2026 \n2142 Promulga Ordenanza Nº 15.700/2026"
        self.assertEqual(boletin.parse(tabla, 820, "u"), [])

    def test_pie_de_pagina(self):
        self.assertEqual(boletin.clean("a \nBOLETÍN OFICIAL MUNICIPAL Nº 826  -09 DE OCTUBRE DEL 2026 \n 23 \nb").split(),
                         ["a", "b"])


class SesionTest(unittest.TestCase):
    def test_por_titulo_de_sesion(self):
        ords = {o["numero"]: o for o in boletin.parse(TEXTO[826], 826, "u")}
        self.assertEqual(ords[15739]["session"], {"key": "2026-O21", "number": "21", "kind": "Ordinaria", "year": 2026,
                                                  "date": "2026-09-24"})
        self.assertEqual(ords[15764]["session"]["key"], "2026-O22")
        self.assertEqual(ords[15780]["session"]["key"], "2026-O23")
        self.assertFalse(any(o["a_revisar"] for o in ords.values()))

    def test_respaldo_por_texto_y_a_revisar(self):
        base = ("ORDENANZA Nº 15.790/2026 PROMULGADA POR DECRETO Nº 2600/26 DE FECHA 20/10/2026\nARTÍCULO 1º.- APRUÉBASE "
                "el pliego{}.- ARTÍCULO 2º.- Comuníquese al Departamento Ejecutivo Municipal.-")
        [o] = boletin.parse(base.format(", aprobado en la Sesión Ordinaria n° 24 del 15 de octubre de 2026"), 827, "u")
        self.assertEqual((o["session"]["key"], o["session"]["date"], o["a_revisar"]), ("2026-O24", "2026-10-15", False))
        [o] = boletin.parse(base.format(""), 827, "u")
        self.assertEqual((o["session"], o["a_revisar"]), (None, True))


class DuplicadosTest(unittest.TestCase):
    def setUp(self):
        self.ord = notas(store())
        self.sesiones = prensa.sessions(PERIODO)

    def match(self, numero):
        n = self.ord[numero]
        return boletin.match(n, self.sesiones[boletin.session_of(store()["ordenanzas"][str(numero)])["key"]]["notes"])

    def test_epet_30_ya_la_publico_prensa(self):
        status, nota = self.match(15710)
        self.assertEqual((status, nota["id"]), ("duplicado", 1531))

    def test_comodato_de_cumbres_es_nueva(self):
        self.assertEqual(self.match(15740), ("nueva", None))
        self.assertTrue(prensa.is_destacado(self.ord[15740]))
        self.assertEqual(prensa.themes(self.ord[15740]), ["Tierras"])

    def test_dudoso(self):
        n = {"title": "Convenio con el Club Lácar", "text": "Por 1.500 metros cuadrados."}
        prensa_nota = {"id": 9, "title": "Un predio del Lácar", "text": "de 1.500 metros"}  # 2 datos en común
        self.assertEqual(boletin.match(n, [prensa_nota]), ("dudoso", prensa_nota))

    def test_audiencias_resueltas_por_ordenanza(self):
        ords = list(self.ord.values())
        por_id = {str(a["notes"][0]["id"]): a for a in prensa.audiencias(PERIODO, date(2026, 6, 1), date(2026, 10, 19))}
        for nota_id, label in [("1119", "15.741/26"), ("1172", "15.710/26"), ("1179", "15.740/26"), ("1334", None)]:
            o = boletin.decided(por_id[nota_id]["notes"], ords)
            self.assertEqual(o and o["ordenanza"]["label"], label, nota_id)  # Obispado, EPET 30, Cumbres; Don Márquez no


class AnonimizacionTest(unittest.TestCase):
    def setUp(self):
        self.st = store()

    def test_particulares_sin_texto_y_adjudicaciones_solo_agregadas(self):
        ords = self.st["ordenanzas"]
        self.assertNotIn("PRUEBA", json.dumps(self.st, ensure_ascii=False))  # ni nombres ni DNI en lo que se guarda
        self.assertNotIn("11.111", json.dumps(self.st))
        for n in ("15750", "15751", "15754", "15756", "15770", "15712"):
            self.assertTrue(ords[n]["particular"], n)
            self.assertNotIn("text", ords[n])
            self.assertEqual(ords[n]["adjudicacion"]["chacra"], "4")
        self.assertNotIn("adjudicacion", ords["15739"])  # licencia o distinción a una persona: no sale de ninguna forma
        [g] = boletin.adjudicaciones(sorted(ords.values(), key=lambda o: o["numero"]))
        self.assertEqual((g["chacra"], g["lotes"]), ("4", 7))
        self.assertEqual(g["labels"], ["15.712/26", "15.750/26", "15.751/26", "15.754/26", "15.756/26", "15.770/26"])

    def test_control_final(self):
        self.assertTrue(boletin.leaks("Se adjudica a la Sra. Ana Prueba el lote"))
        self.assertTrue(boletin.leaks("titular DNI N° 22.222.222"))
        self.assertTrue(boletin.leaks("CUIL 27-22222222-3"))
        self.assertFalse(boletin.leaks("contratación de la firma GROOM TECH S.R.L., CUIT N° 30-71642628-5"))
        self.assertFalse(boletin.leaks("autoriza al Sr. Intendente Municipal a firmar"))

    def test_texto_guardado_sin_documentos(self):
        texto = self.st["ordenanzas"]["15707"]["text"]  # contratación a una empresa: el texto queda, sin el CUIT
        self.assertIn("GROOM TECH", texto)
        self.assertNotIn("30-71642628-5", texto)


if __name__ == "__main__":
    unittest.main()
