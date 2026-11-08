import json
import unittest
from unittest import mock

import concejo_videos as cv

VIDEO = {"video_id": "abc", "title": "Sesión", "published_at": "2026-10-29T12:00:00Z", "duration": "PT4H11M38S",
         "description": "hola @alguien"}
ENV = {"YOUTUBE_API_KEY": "k", "GITHUB_REPOSITORY": "a/b", "GITHUB_TOKEN": "t"}


class ConcejoVideosTest(unittest.TestCase):
    def correr(self, index, videos=(VIDEO,)):
        with mock.patch.dict("os.environ", ENV), mock.patch.object(cv, "latest_videos", return_value=list(videos)), \
                mock.patch.object(cv, "load_json", return_value=index), mock.patch.object(cv, "save_json") as save, \
                mock.patch.object(cv, "open_issue") as issue:
            self.assertEqual(cv.main(), 0)
        return save, issue

    def test_sin_clave_no_llama_a_nada(self):
        with mock.patch.dict("os.environ", {"YOUTUBE_API_KEY": ""}), mock.patch.object(cv.requests, "get") as get:
            self.assertEqual(cv.main(), 0)
        get.assert_not_called()

    def test_la_clave_no_va_en_la_url(self):
        with mock.patch.object(cv.requests, "get") as get:
            get.return_value.json.return_value = {"items": []}
            cv.youtube("videos", "SECRETO", id="x")
        self.assertNotIn("SECRETO", json.dumps([get.call_args.args, get.call_args.kwargs["params"]]))

    def test_arma_el_registro_con_los_campos_pedidos(self):
        crudo = {"id": "abc", "snippet": {"title": "Sesión", "publishedAt": VIDEO["published_at"],
                                          "description": VIDEO["description"]},
                 "contentDetails": {"duration": VIDEO["duration"]}}
        with mock.patch.object(cv, "youtube", side_effect=[[{"contentDetails": {"videoId": "abc"}}], [crudo]]) as yt:
            self.assertEqual(cv.latest_videos("k"), [VIDEO])
        self.assertEqual(yt.call_args_list[0].kwargs["playlistId"], "UUaJbax3QkHRsO4BHKPcxfEw")

    def test_primera_corrida_crea_el_indice_sin_issues(self):
        save, issue = self.correr(None)
        self.assertEqual(save.call_args.args[1:3], ("concejo-videos/_index.json", {"abc": VIDEO}))
        issue.assert_not_called()

    def test_canal_vacio_igual_crea_el_indice(self):
        save, _ = self.correr(None, ())
        self.assertEqual(save.call_args.args[2], {})

    def test_video_nuevo_se_guarda_y_abre_issue(self):
        save, issue = self.correr({"viejo": {"video_id": "viejo"}})
        self.assertEqual(set(save.call_args.args[2]), {"viejo", "abc"})
        issue.assert_called_once_with(VIDEO, "a/b", "t")

    def test_pasa_los_nuevos_al_job_de_transcripcion(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "out")
            with mock.patch.dict("os.environ", {"GITHUB_OUTPUT": out}):
                self.correr({"viejo": {"video_id": "viejo"}})
            with open(out, encoding="utf-8") as f:
                self.assertEqual(f.read(), 'nuevos=["abc"]\n')

    def test_sin_novedades_no_escribe_ni_abre_issues(self):
        save, issue = self.correr({"abc": VIDEO})
        save.assert_not_called()
        issue.assert_not_called()

    def test_la_descripcion_no_rompe_el_bloque_de_codigo(self):
        cuerpo = cv.issue_body({**VIDEO, "description": "a ``` @b"})
        self.assertEqual(cuerpo.count("```"), 2)

    def test_error_de_github_al_abrir_issue_se_propaga(self):
        with mock.patch.object(cv.requests, "post") as post:
            post.return_value.status_code = 201
            post.return_value.raise_for_status.side_effect = cv.requests.HTTPError("403")
            with self.assertRaises(cv.requests.HTTPError):
                cv.open_issue(VIDEO, "a/b", "t")


if __name__ == "__main__":
    unittest.main()
