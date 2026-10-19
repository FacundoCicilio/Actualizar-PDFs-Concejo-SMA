import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import instagram_carousel as ic
import instagram_publisher as pub
import prensa

TOKEN = "IGTOKEN-secreto-de-prueba"
T0 = datetime(2026, 10, 18, 10, 5, tzinfo=timezone.utc)

GENERAL = {"session_number": "23", "session_year": 2026, "date": "08/10/2026", "items": [
    {"key": "1492/2026", "que_dice": "Dictamen: Modificación Ordenanza 4166/2001 Tarifa taxis y remisses.",
     "title": "Modificación Ordenanza 4166/2001 Tarifa taxis y remisses.", "impact_tags": ["💰 Bolsillo"]}]}
SIN_GENERALES = {"session_number": "24", "session_year": 2026, "date": "15/10/2026", "items": [
    {"key": "1", "que_dice": "Declaración de Interés Municipal la Peña", "title": "Declaración de Interés Municipal la Peña",
     "impact_tags": ["🎭 Cultura/Eventos"]}]}


def nota(note_id, n, dia, titulo, verbo="aprobó", vigencia="La norma rige desde enero de 2027."):
    """Nota de Prensa de la sesión n, realizada el día `dia` de octubre de 2026 y publicada ese mismo día."""
    texto = (f"El Concejo Deliberante de San Martín de los Andes {verbo}, durante la {n}ª Sesión Ordinaria del año 2026, "
             f"realizada el jueves {dia} de octubre, {titulo.lower()}. {vigencia}")
    return {"id": note_id, "date": f"2026-10-{dia:02d}T12:00:00", "modified": f"2026-10-{dia:02d}T12:00:00",
            "link": f"https://prensacd.cdsma.gob.ar/{note_id}/", "title": titulo, "text": texto}


TASA = "Una reducción del 35% en la tasa comercial"
INTERES = "La declaración de interés municipal de la Peña del Club Lácar"
CAMPOS = {"titular": "Baja la tasa comercial", "que_cambia": "Se reduce un 35% la tasa comercial.",
          "a_quien_afecta": "A los comercios.", "desde_cuando": ""}


def rec(doc_id, n):
    return {"doc_id": doc_id, "session_year": 2026, "session_number": str(n), "created_at": "2026-10-01"}


class FakeGitHub:
    def __init__(self):
        self.issues, self.labels_created = {}, []

    def __call__(self, method, path, ok=(200, 201), **kw):
        body = kw.get("json") or {}
        if path == "/labels":
            self.labels_created.append(body["name"])
            return {}
        if path.startswith("/git/"):
            return {"object": {"sha": "abc"}}
        if method == "POST" and path == "/issues":
            n = len(self.issues) + 1
            self.issues[n] = {"number": n, "title": body["title"], "body": body["body"], "state": "open",
                              "labels": [{"name": x} for x in body["labels"]], "comments": [], "html_url": f"https://gh/{n}"}
            return self.issues[n]
        if method == "GET" and path == "/issues":
            label = kw["params"]["labels"]
            return [i for i in self.issues.values() if i["state"] == "open" and label in {lb["name"] for lb in i["labels"]}]
        m = re.fullmatch(r"/issues/(\d+)(/comments)?", path)
        issue = self.issues[int(m.group(1))]
        if m.group(2):
            issue["comments"].append(body["body"])
        elif method == "PATCH":
            issue["state"] = body["state"]
        return issue

    def by_label(self, label):
        return [i for i in self.issues.values() if label in {lb["name"] for lb in i["labels"]}]

    def all_text(self):
        return "\n".join(i["title"] + i["body"] + "".join(i["comments"]) for i in self.issues.values())


class FakeIG:
    def __init__(self):
        self.calls, self.fail_child, self.status, self.recent = [], None, {}, []  # status: estado por contenedor
        self.valid_tokens, self.username = {TOKEN}, "concejo.sma"

    def __call__(self, method, path, token, **p):
        self.calls.append((method, path, p))
        if token not in self.valid_tokens:
            raise pub.IGError("Instagram: token inválido", 190)
        if path == "me":
            return {"user_id": "17841", "username": self.username}
        if method == "POST" and path.endswith("/media"):
            if p.get("is_carousel_item"):
                n = sum(1 for c in self.calls if c[2].get("is_carousel_item"))
                if n == self.fail_child:
                    raise pub.IGError("Instagram: no se pudo descargar la imagen", 9004)
                return {"id": f"child{n}"}
            return {"id": "carousel1"}
        if method == "POST" and path.endswith("/media_publish"):
            return {"id": "media1"}
        if method == "GET" and path.endswith("/media"):
            return {"data": self.recent}
        if p.get("fields") == "status_code":
            return {"status_code": self.status.get(path, "FINISHED")}
        if p.get("fields") == "permalink":
            return {"permalink": "https://www.instagram.com/p/ABC/"}
        raise AssertionError(f"llamada inesperada {method} {path}")

    def published(self):
        return [c for c in self.calls if c[1].endswith("/media_publish")]


class PublisherTest(unittest.TestCase):
    def setUp(self):
        self.hf = {}
        self.index = [rec("viejo1", 21), rec("viejo2", 22)]
        self.docs = {"viejo1": GENERAL, "viejo2": GENERAL}
        self.notes = {"1": nota(1, 23, 8, INTERES)}  # sesión anterior a la primera corrida
        self.gh, self.ig, self.clock = FakeGitHub(), FakeIG(), [T0]
        self.refreshes = []
        tmp = tempfile.mkdtemp()
        patches = [
            mock.patch.object(ic, "DOTENV", ic.Path(tmp) / "no-existe.env"),  # nunca el .env real
            mock.patch.dict(os.environ, {"IG_ACCESS_TOKEN": TOKEN, "IG_USER_ID": "17841", "GITHUB_TOKEN": "gh"}, clear=True),
            mock.patch.object(pub, "load_index", lambda repo: self.index),
            mock.patch.object(pub, "load_json", lambda repo, path: self.hf.get(path)),
            mock.patch.object(pub, "save_json", lambda repo, path, data, msg: self.hf.__setitem__(path, __import__("copy").deepcopy(data))),
            mock.patch.object(pub, "load_processed_json", lambda repo, doc_id: self.docs.get(doc_id)),
            mock.patch.object(pub, "gh", self.gh),
            mock.patch.object(pub, "ig", self.ig),
            mock.patch.object(pub, "upload_images", lambda paths, folder: [f"https://raw/{folder}/{p.name}" for p in paths]),
            mock.patch.object(pub, "refresh", self._refresh),
            mock.patch.object(pub, "now", lambda: self.clock[0]),
            mock.patch.object(pub.time, "sleep", lambda s: None),
            mock.patch.object(ic, "criollo_lines", lambda items: ([None] * len(items), None)),
            mock.patch.object(ic, "note_lines", lambda notes: (["El Concejo aprobó algo."] * len(notes), None)),
            mock.patch.object(ic, "destacado", lambda note: (dict(CAMPOS), None)),
            mock.patch.object(prensa, "load_notes", lambda save=True: self.notes),
            mock.patch.object(pub, "_token", None),
            mock.patch("builtins.print"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.hf[pub.TOKEN_PATH] = {"token": TOKEN, "expires_at": pub.iso(T0 + timedelta(days=50))}

    def _refresh(self, token):
        self.refreshes.append(token)
        return token, self.clock[0] + timedelta(days=60)

    def run_pub(self, hours_later=0):
        self.clock[0] = T0 + timedelta(hours=hours_later)
        pub._token = None  # cada corrida es un proceso nuevo
        return pub.run()

    def nueva_orden(self, doc_id="nuevo", n=23, doc=GENERAL):
        self.index.append(rec(doc_id, n))
        self.docs[doc_id] = doc

    @property
    def state(self):
        return self.hf[pub.STATE_PATH]

    # --- disparo
    def test_primera_corrida_marca_las_existentes_y_no_publica(self):
        self.assertEqual(self.run_pub(), 0)
        self.assertEqual(self.state["seen"], ["viejo1", "viejo2"])
        self.assertEqual(self.gh.issues, {})
        self.assertEqual(self.ig.published(), [])

    def test_orden_nueva_abre_vista_previa_sin_publicar(self):
        self.run_pub()
        self.nueva_orden()
        self.assertEqual(self.run_pub(1), 0)
        [issue] = self.gh.by_label("instagram-vista-previa")
        self.assertIn("Sesión N° 23", issue["title"])
        self.assertIn("no-publicar", issue["body"])
        self.assertIn("![Placa 1](https://raw/", issue["body"])
        self.assertIn("19/10 a las 10:17 UTC (07:17 en Argentina)", issue["body"])
        self.assertIn("nuevo", self.state["pending"])
        self.assertEqual(self.ig.published(), [])

    def test_sin_items_de_alcance_general_no_publica_y_queda_vista(self):
        self.run_pub()
        self.nueva_orden(doc=SIN_GENERALES, n=24)
        self.run_pub(1)
        self.assertEqual(self.gh.issues, {})
        self.assertIn("nuevo", self.state["seen"])

    def test_pdf_corregido_de_la_misma_sesion_no_se_repite(self):
        self.run_pub()
        self.nueva_orden("nuevo", 23)
        self.run_pub(1)
        self.nueva_orden("corregido", 23)
        self.run_pub(2)
        self.assertEqual(len(self.gh.by_label("instagram-vista-previa")), 1)
        self.assertIn("corregido", self.state["seen"])

    # --- período de prueba
    def test_publica_en_la_corrida_siguiente_y_cierra_el_issue(self):
        self.run_pub()
        self.nueva_orden()
        self.run_pub(1)
        self.run_pub(2)  # misma jornada: todavía no
        self.assertEqual(self.ig.published(), [])
        self.assertEqual(self.run_pub(24), 0)
        self.assertEqual(len(self.ig.published()), 1)
        issue = self.gh.issues[1]
        self.assertEqual(issue["state"], "closed")
        self.assertIn("https://www.instagram.com/p/ABC/", issue["comments"][-1])
        self.assertEqual(self.state["approved"], 1)
        self.assertEqual(self.state["published"]["nuevo"]["status"], "done")

    def test_con_no_publicar_se_descarta(self):
        self.run_pub()
        self.nueva_orden()
        self.run_pub(1)
        self.gh.issues[1]["labels"].append({"name": "no-publicar"})
        self.run_pub(24)
        self.assertEqual(self.ig.published(), [])
        self.assertEqual(self.gh.issues[1]["state"], "closed")
        self.assertEqual(self.state["approved"], 0)
        self.assertEqual(self.state["pending"], {})

    def test_cerrado_a_mano_tampoco_se_publica(self):
        self.run_pub()
        self.nueva_orden()
        self.run_pub(1)
        self.gh.issues[1]["state"] = "closed"
        self.run_pub(24)
        self.assertEqual(self.ig.published(), [])

    def test_tres_aprobaciones_pasan_a_publicacion_directa(self):
        self.run_pub()
        for i, n in enumerate((23, 24, 25)):
            self.nueva_orden(f"od{n}", n)
            self.run_pub(1 + 48 * i)
            self.run_pub(25 + 48 * i)
        self.assertEqual(self.state["approved"], 3)
        self.assertEqual(self.state["mode"], "direct")
        self.assertIn("termina el período de prueba", self.gh.issues[3]["comments"][-1])
        self.nueva_orden("od26", 26)
        self.run_pub(200)
        self.assertEqual(len(self.gh.by_label("instagram-vista-previa")), 3)  # sin Issue nuevo
        self.assertEqual(len(self.ig.published()), 4)

    # --- nunca incompleto, nunca duplicado
    def test_si_falla_una_imagen_no_se_publica_y_se_reintenta(self):
        self.run_pub()
        self.nueva_orden()
        self.run_pub(1)
        self.ig.fail_child = 2
        self.assertEqual(self.run_pub(24), 1)
        self.assertEqual(self.ig.published(), [])
        self.assertIn("nuevo", self.state["pending"])
        [err] = self.gh.by_label("instagram-error")
        self.assertIn("no se pudo descargar la imagen", err["body"])

    def test_contenedor_con_error_no_se_publica(self):
        self.state_with_pending()
        self.ig.status = {"carousel1": "ERROR"}  # las imágenes salen bien; falla el carrusel
        self.assertEqual(self.run_pub(24), 1)
        self.assertEqual(self.ig.published(), [])

    def test_ya_publicado_no_se_repite_aunque_haya_fallado_cerrar_el_issue(self):
        self.state_with_pending()
        self.state["published"]["nuevo"] = {"status": "done", "permalink": "https://www.instagram.com/p/X/"}
        self.run_pub(24)
        self.assertEqual(self.ig.published(), [])
        self.assertEqual(self.gh.issues[1]["state"], "closed")

    def test_publicacion_a_medias_se_encuentra_en_instagram(self):
        self.state_with_pending()
        caption = self.state["pending"]["nuevo"]["caption"]
        self.state["published"]["nuevo"] = {"status": "publishing", "images": [], "caption": caption}
        self.ig.recent = [{"id": "m9", "caption": caption, "permalink": "https://www.instagram.com/p/YA/"}]
        self.run_pub(24)
        self.assertEqual(self.ig.published(), [])
        self.assertIn("https://www.instagram.com/p/YA/", self.gh.issues[1]["comments"][-1])

    def state_with_pending(self):
        self.run_pub()
        self.nueva_orden()
        self.run_pub(1)

    # --- Prensa
    def nueva_nota(self, note_id, n, dia, titulo):
        self.notes[str(note_id)] = nota(note_id, n, dia, titulo)

    def test_prensa_primera_vez_no_publica_sesiones_anteriores(self):
        self.run_pub()
        self.run_pub(1)
        self.assertEqual(self.state["prensa_since"], "2026-10-18")
        self.nueva_nota(2, 23, 8, TASA)  # nota nueva de una sesión anterior: tampoco
        self.run_pub(24 * 10)
        self.assertEqual(self.gh.issues, {})

    def test_destacado_sale_enseguida_y_el_resumen_a_los_7_dias(self):
        self.run_pub()
        self.run_pub(1)
        self.nueva_nota(10, 25, 22, TASA)
        self.nueva_nota(11, 25, 22, INTERES)
        self.clock[0] = T0
        self.run_pub(24 * 4 + 1)  # 22/10: hay sesión nueva con una nota de Bolsillo
        [destacado] = self.gh.by_label("instagram-vista-previa")
        self.assertIn("Destacado · Baja la tasa comercial", destacado["title"])
        self.assertIn("¿Desde cuándo? La nota no lo informa.", destacado["body"])
        self.assertIn("Fuente: Prensa del Concejo Deliberante (prensacd.cdsma.gob.ar)", destacado["body"])
        self.run_pub(24 * 10 + 1)  # 28/10: todavía no pasaron 7 días
        self.assertFalse([i for i in self.gh.issues.values() if "Qué se aprobó" in i["title"]])
        self.run_pub(24 * 11 + 1)  # 29/10
        [resumen] = [i for i in self.gh.issues.values() if "Qué se aprobó" in i["title"]]
        self.assertIn("Sesión N° 25 (22 de octubre de 2026)", resumen["title"])
        self.assertIn("![Placa 4]", resumen["body"])  # portada + 2 notas + cierre
        self.run_pub(24 * 12 + 1)
        self.assertEqual(len(self.gh.by_label("instagram-vista-previa")), 2)  # sin duplicados
        self.assertEqual(len(self.ig.published()), 2)  # cada uno, en la corrida siguiente a su Issue

    def test_destacado_con_plazo_vencido_queda_solo_en_el_resumen(self):
        self.run_pub()
        self.run_pub(1)
        self.notes["40"] = nota(40, 25, 19, "El Plan de Regularización de Deudas Municipales",
                                vigencia="Regirá desde el 19 y hasta el 20 de octubre de 2026.")
        self.clock[0] = T0
        self.run_pub(24 * 3 + 1)  # 21/10: el plan ya no rige
        self.assertEqual(self.gh.by_label("instagram-vista-previa"), [])
        self.run_pub(24 * 8 + 1)  # 26/10: pasaron 7 días de la sesión
        [resumen] = self.gh.by_label("instagram-vista-previa")
        self.assertIn("Qué se aprobó", resumen["title"])
        self.assertIn("![Placa 3]", resumen["body"])  # portada + la nota del plan + cierre

    def test_si_el_plazo_vence_antes_de_publicar_no_se_publica(self):
        self.run_pub()
        self.run_pub(1)
        self.notes["41"] = nota(41, 25, 19, TASA, vigencia="Rige del 19 al 19 de octubre de 2026.")
        self.run_pub(25)  # 19/10: vista previa (todavía rige)
        [issue] = self.gh.by_label("instagram-vista-previa")
        self.assertEqual(self.state["pending"]["destacado:41"]["until"], "2026-10-19")
        self.run_pub(49)  # 20/10: ya venció
        self.assertEqual(self.ig.published(), [])
        self.assertEqual(issue["state"], "closed")
        self.assertIn("el plazo que informa la nota terminó el 19/10/2026", issue["comments"][-1])
        self.assertEqual(pub.counters(self.state, "destacado")["approved"], 0)

    def test_periodo_de_prueba_por_tipo(self):
        self.run_pub()
        self.run_pub(1)
        for i in range(3):
            self.nueva_nota(20 + i, 30 + i, 19, f"{TASA} número {i}")
            self.run_pub(24 * (i + 1) + 1)
        self.run_pub(24 * 5)
        self.assertEqual(pub.counters(self.state, "destacado"), {"approved": 3, "mode": "direct"})
        self.assertEqual((self.state["approved"], self.state["mode"]), (0, "review"))  # el de órdenes del día no cambia
        self.nueva_orden()
        self.run_pub(24 * 6)
        self.assertIn("nuevo", self.state["pending"])  # la orden del día sigue con vista previa

    def test_si_falla_anthropic_no_publica_placas_vacias_y_reintenta(self):
        self.run_pub()
        self.run_pub(1)
        self.nueva_nota(30, 25, 22, TASA)
        with mock.patch.object(ic, "destacado", lambda note: (None, "La API de Anthropic falló (APIConnectionError): ")):
            self.assertEqual(self.run_pub(24 * 4 + 1), 1)
        self.assertEqual(self.gh.by_label("instagram-vista-previa"), [])
        [err] = self.gh.by_label("instagram-error")
        self.assertEqual(err["title"], pub.ERROR_TITLES["anthropic"])
        self.run_pub(24 * 5 + 1)
        self.assertEqual(len(self.gh.by_label("instagram-vista-previa")), 1)

    def test_si_el_sitio_de_prensa_falla_avisa(self):
        self.run_pub()
        with mock.patch.object(prensa, "load_notes", mock.Mock(side_effect=pub.requests.ConnectionError())):
            self.assertEqual(self.run_pub(1), 1)
        [err] = self.gh.by_label("instagram-error")
        self.assertEqual(err["title"], pub.ERROR_TITLES["prensa"])

    # --- token
    def test_token_se_renueva_cuando_faltan_menos_de_15_dias(self):
        self.hf[pub.TOKEN_PATH]["expires_at"] = pub.iso(T0 + timedelta(days=10))
        self.run_pub()
        self.run_pub(24)
        self.assertEqual(self.refreshes, [TOKEN])
        self.assertEqual(self.hf[pub.TOKEN_PATH]["expires_at"], pub.iso(T0 + timedelta(hours=24, days=60)))

    def test_token_lejos_del_vencimiento_no_se_renueva(self):
        self.run_pub()
        self.run_pub(24)
        self.assertEqual(self.refreshes, [])

    def test_si_el_guardado_vencio_toma_el_secret_nuevo(self):
        self.hf[pub.TOKEN_PATH] = {"token": "viejo-vencido", "expires_at": pub.iso(T0)}
        self.run_pub()
        self.run_pub(24)
        self.assertEqual(self.hf[pub.TOKEN_PATH]["token"], TOKEN)

    def test_sin_ningun_token_valido_abre_issue(self):
        self.ig.valid_tokens = set()
        self.run_pub()
        self.assertEqual(self.run_pub(24), 1)
        [err] = self.gh.by_label("instagram-error")
        self.assertEqual(err["title"], pub.ERROR_TITLES["token"])

    # --- errores
    def test_el_mismo_error_comenta_en_vez_de_abrir_otro_issue(self):
        self.ig.valid_tokens = set()
        self.run_pub()
        self.run_pub(24)
        self.run_pub(48)
        [err] = self.gh.by_label("instagram-error")
        self.assertEqual(len(err["comments"]), 1)

    def test_si_falla_anthropic_abre_issue_y_sigue_sin_en_criollo(self):
        self.run_pub()
        self.nueva_orden()
        with mock.patch.object(ic, "criollo_lines", lambda items: ([None] * len(items), "La API de Anthropic falló (BadRequestError): credit balance is too low")):
            self.run_pub(1)
        [err] = self.gh.by_label("instagram-error")
        self.assertEqual(err["title"], pub.ERROR_TITLES["anthropic"])
        self.assertEqual(len(self.gh.by_label("instagram-vista-previa")), 1)

    def test_ningun_issue_incluye_el_token(self):
        self.ig.valid_tokens = set()
        self.run_pub()
        self.nueva_orden()
        self.run_pub(1)
        self.run_pub(24)
        self.assertTrue(self.gh.issues)
        self.assertNotIn(TOKEN, self.gh.all_text())

    # --- workflow de prueba
    def preview(self):
        self.index.append(rec("od23", 23))
        self.docs["od23"] = GENERAL
        code = pub.run_preview(23, 2026)
        [issue] = self.gh.by_label("instagram-prueba")
        self.assertNotIn(TOKEN, issue["body"])
        self.assertEqual([(m, path) for m, path, _ in self.ig.calls], [("GET", "me")] * len(self.ig.calls))  # nunca publica
        self.assertNotIn(pub.STATE_PATH, self.hf)
        return code, issue

    def test_prueba_abre_issue_con_token_valido(self):
        code, issue = self.preview()
        self.assertEqual(code, 0)
        self.assertIn("PRUEBA", issue["title"])
        self.assertIn("no se publica", issue["body"])
        self.assertIn("✅ Token válido: cuenta @concejo.sma.", issue["body"])

    def test_prueba_con_token_de_otra_cuenta(self):
        self.ig.username = "otra.cuenta"
        code, issue = self.preview()
        self.assertEqual(code, 1)
        self.assertIn("❌ El token es de la cuenta @otra.cuenta, no de @concejo.sma.", issue["body"])
        self.assertIn("![Placa 1]", issue["body"])  # la vista previa sale igual

    def test_prueba_con_token_invalido(self):
        self.ig.valid_tokens = set()
        code, issue = self.preview()
        self.assertEqual(code, 1)
        self.assertIn("❌ El token no funciona: Instagram: token inválido", issue["body"])

    def test_prueba_sin_secret(self):
        del os.environ["IG_ACCESS_TOKEN"]
        code, issue = self.preview()
        self.assertEqual(code, 1)
        self.assertIn("❌ Falta el secret `IG_ACCESS_TOKEN`.", issue["body"])
        self.assertEqual(self.ig.calls, [])

    def test_prueba_que_se_aprobo_y_destacado(self):
        self.notes["2"] = nota(2, 23, 8, TASA)
        self.assertEqual(pub.run_preview("23", 2026, "que-se-aprobo"), 0)
        self.assertEqual(pub.run_preview("23", 2026, "destacado"), 0)
        titles = [i["title"] for i in self.gh.by_label("instagram-prueba")]
        self.assertEqual(titles, ["PRUEBA Instagram: Qué se aprobó · Sesión N° 23 (8 de octubre de 2026)",
                                  "PRUEBA Instagram: Destacado · Baja la tasa comercial"])
        self.assertNotIn(pub.STATE_PATH, self.hf)
        self.assertEqual(self.ig.published(), [])

    def test_prueba_destacado_omite_plazos_vencidos(self):
        self.notes["2"] = nota(2, 23, 8, TASA, vigencia="Rige del 1 al 30 de septiembre de 2026.")
        pub.run_preview("23", 2026, "destacado")
        [issue] = self.gh.by_label("instagram-prueba")
        self.assertIn("con un plazo vigente", issue["body"])

    def test_prueba_destacado_sin_notas_destacables(self):
        pub.run_preview("23", 2026, "destacado")
        [issue] = self.gh.by_label("instagram-prueba")
        self.assertIn("no tiene notas aprobadas de Bolsillo", issue["body"])

    def test_prueba_con_ig_user_id_distinto(self):
        os.environ["IG_USER_ID"] = "999"
        code, issue = self.preview()
        self.assertEqual(code, 1)
        self.assertIn("IG_USER_ID", issue["body"])

if __name__ == "__main__":
    unittest.main()
