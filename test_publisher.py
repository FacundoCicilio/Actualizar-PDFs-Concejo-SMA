import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import instagram_carousel as ic
import instagram_publisher as pub

TOKEN = "IGTOKEN-secreto-de-prueba"
T0 = datetime(2026, 10, 18, 10, 5, tzinfo=timezone.utc)

GENERAL = {"session_number": "23", "session_year": 2026, "date": "08/10/2026", "items": [
    {"key": "1492/2026", "que_dice": "Dictamen: Modificación Ordenanza 4166/2001 Tarifa taxis y remisses.",
     "title": "Modificación Ordenanza 4166/2001 Tarifa taxis y remisses.", "impact_tags": ["💰 Bolsillo"]}]}
SIN_GENERALES = {"session_number": "24", "session_year": 2026, "date": "15/10/2026", "items": [
    {"key": "1", "que_dice": "Declaración de Interés Municipal la Peña", "title": "Declaración de Interés Municipal la Peña",
     "impact_tags": ["🎭 Cultura/Eventos"]}]}


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
        self.valid_tokens = {TOKEN}

    def __call__(self, method, path, token, **p):
        self.calls.append((method, path, p))
        if token not in self.valid_tokens:
            raise pub.IGError("Instagram: token inválido", 190)
        if path == "me":
            return {"user_id": "17841"}
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
        self.assertIn("19/10 a las 10:00 UTC (07:00 en Argentina)", issue["body"])
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
    def test_prueba_abre_issue_sin_tocar_el_estado(self):
        self.index.append(rec("od23", 23))
        self.docs["od23"] = GENERAL
        self.assertEqual(pub.run_preview(23, 2026), 0)
        [issue] = self.gh.by_label("instagram-prueba")
        self.assertIn("PRUEBA", issue["title"])
        self.assertIn("no se publica", issue["body"])
        self.assertNotIn(pub.STATE_PATH, self.hf)
        self.assertEqual(self.ig.calls, [])


if __name__ == "__main__":
    unittest.main()
