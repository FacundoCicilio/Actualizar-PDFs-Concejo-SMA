import unittest
from datetime import date

import newsletter as nl


class TestReglas(unittest.TestCase):
    def test_excluye_declaraciones_y_aniversarios(self):
        self.assertTrue(nl.excluded("Declaran de interés el Motor Fest Patagonia y el Encuentro de Emprendedores"))
        self.assertTrue(nl.excluded("Por sus 20 años, el Concejo reconoció la trayectoria de Puentes de Luz"))
        self.assertTrue(nl.excluded("Reconocimiento a una asociación", "declaró de interés municipal la conmemoración del 20° aniversario"))

    def test_no_excluye_si_hay_otra_decision(self):
        self.assertFalse(nl.excluded("Parque Cervecero: el Concejo autorizó la venta de alcohol y lo declaró de Interés Municipal"))
        self.assertFalse(nl.excluded("El Concejo Deliberante aprobó el nuevo régimen de terrazas gastronómicas"))

    def test_problemas_del_borrador(self):
        mat = [{"url": "https://prensacd.cdsma.gob.ar/a/"}, {"url": "https://x.gob.ar/PDF-N°-28.pdf"}]
        ok = "Ver [nota](https://prensacd.cdsma.gob.ar/a/). Fuente: https://x.gob.ar/PDF-N%C2%B0-28.pdf."
        self.assertEqual(nl.problems(ok, mat), [])
        bad = "| a | b |\n|---|---|\n[otra](https://inventada.com/x)"
        p = nl.problems(bad, mat)
        self.assertTrue(any("tabla" in x for x in p))
        self.assertTrue(any("inventada.com" in x for x in p))
        self.assertEqual(sum("falta el documento" in x for x in p), 2)

    def test_privado(self):
        junta = ("El Concejo designó a los integrantes de la Junta Electoral Municipal. Los titulares son Martín Jorge "
                 "Rodríguez, Andrés Freisztav y Mario César Enrique Orozco.")
        self.assertIn("Freisztav", nl.privado(junta))  # cargo público: con nombre
        taxi = ("AUTORÍZASE, por vía de excepción, al Sr. Juan Pérez, DNI 20.123.456, la continuidad del servicio de taxi "
                "con la licencia de taxi Nº 45 y el dominio AA-350-PL.")
        out = nl.privado(taxi)
        self.assertIn("Juan Pérez", out)  # trato distinto al general: con nombre
        for x in ("20.123.456", "Nº 45", "AA-350-PL"):
            self.assertNotIn(x, out)

    def test_corte_del_viernes(self):
        from datetime import datetime
        desde, hasta = date(2026, 11, 14), date(2026, 11, 20)
        self.assertTrue(nl.en_rango(datetime(2026, 11, 13, 14, 8, tzinfo=nl.ARG), desde, hasta))  # Boletín 831
        self.assertFalse(nl.en_rango(datetime(2026, 11, 13, 8, 0, tzinfo=nl.ARG), desde, hasta))
        self.assertTrue(nl.en_rango(datetime(2026, 11, 20, 23, 0, tzinfo=nl.ARG), desde, hasta))
        self.assertFalse(nl.en_rango(None, desde, hasta))

    def test_substack(self):
        xml = (b'<?xml version="1.0"?><rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item>'
               b'<title>El agua sube 79%</title><pubDate>Fri, 13 Nov 2026 12:00:00 GMT</pubDate>'
               b'<content:encoded><![CDATA[<p>La <b>tarifa</b> del agua.</p>]]></content:encoded></item></channel></rss>')
        self.assertEqual(nl.substack(xml), [(date(2026, 11, 13), "El agua sube 79%", "La tarifa del agua.")])

    def test_problemas_interes_e_ids(self):
        mat = [{"url": "https://p/1"}]
        ok = ("# El Concejo autorizó el Parque Cervecero\n- **Parque Cervecero**: autoriza la venta de alcohol y lo "
              "declaró de interés. [Fuente](https://p/1)")
        self.assertEqual(nl.problems(ok, mat), [])
        mal = ok + "\n- **Declaración de interés municipal del Parque Cervecero**: el Concejo también aprobó declararlo."
        self.assertTrue(any("declaración de interés" in x for x in nl.problems(mal, mat)))
        self.assertTrue(any("aniversario" in x for x in nl.problems("# Por sus 20 años, reconocen a un club\n" + ok, mat)))
        self.assertTrue(any("documentos o números" in x for x in nl.problems(ok + " Licencia de taxi Nº 45.", mat)))

    def test_sesiones(self):
        def nota(i, fecha, texto):
            return {"id": i, "date": f"{fecha}T09:00:00", "title": texto[:60], "text": texto, "link": f"https://p/{i}"}
        index = {"a": {"date": "19/11/2026", "session_number": 28, "source_url": "od28"},
                 "b": {"date": "12/11/2026", "session_number": 27, "source_url": "od27"},
                 "c": {"date": "26/11/2026", "session_number": 29, "source_url": "od29"}}
        notes = {"1": nota(1, "2026-11-20", "El Concejo designó a los integrantes de la Junta Electoral, durante la 28ª "
                                            "Sesión Ordinaria del año 2026, realizada el jueves 19 de noviembre.")}
        estados = {c["doc"]["session_number"]: e for c, e, _ in nl.sesiones(index, notes, date(2026, 11, 14), date(2026, 11, 20))}
        self.assertEqual(estados, {"28": "aprobado", "29": "a tratar"})  # la 27 es de la semana anterior y no tuvo notas
        estados = {c["doc"]["session_number"]: e for c, e, _ in nl.sesiones(index, {}, date(2026, 11, 14), date(2026, 11, 20))}
        self.assertEqual(estados["28"], "pendiente")

    def test_problemas_sesion_realizada(self):
        mat = [{"tipo": "sesión realizada, sin lo aprobado publicado", "sesion": "Sesión N° 28 (19 de noviembre de 2026)",
                "texto": "Se realizó el 19 de noviembre de 2026; el Concejo todavía no publicó lo aprobado.", "url": "https://od/28.pdf"}]
        p = nl.problems("## Qué va a tratar el Concejo: Sesión N° 28\n[Orden del día](https://od/28.pdf)", mat)
        self.assertTrue(any("ya se realizó" in x for x in p))
        self.assertTrue(any("tal cual" in x for x in p))
        ok = "## Sesión N° 28\nSe realizó el 19 de noviembre de 2026; el Concejo todavía no publicó lo aprobado. [Orden del día](https://od/28.pdf)"
        self.assertEqual(nl.problems(ok, mat), [])
        self.assertTrue(nl.problems(ok + " DNI 12.345.678", mat))

    def test_ordenanzas_por_numero_y_cantidad(self):
        pdf = "https://bo/bom-831.pdf"  # comparten el PDF: el link solo no alcanza
        mat = [{"tipo": "ordenanza", "ordenanza": f"15.{n}/26", "boletin": 831, "texto": "", "url": pdf} for n in (816, 817)]
        self.assertEqual(nl.conteos(mat), {831: 2})
        ok = f"# El Boletín 831 trae 2 ordenanzas\n- **Agua**: ordenanzas 15.816/26 y 15817. [PDF]({pdf})"
        self.assertEqual(nl.problems(ok, mat), [])
        self.assertEqual(nl.problems(ok.replace(" y 15817", ""), mat), [])  # agrupadas: no hace falta cada número
        p = nl.problems(ok.replace("trae 2", "trae 22"), mat)
        self.assertTrue(any("22 ordenanzas" in x and "son 2" in x for x in p))
        self.assertTrue(nl.problems(ok.replace("trae 2", "trae agua y otras 2"), mat))  # serían 3
        self.assertEqual(nl.problems(ok.replace("trae 2", "trae agua y otras 1"), mat), [])
        self.assertTrue(any("título dice 79" in x for x in nl.problems("# El agua sube 79%\n" + ok, mat)))

    def test_relleno(self):
        mat = [{"url": "https://p/1"}]
        for relleno in ("- **Anexo**: ver el ítem de arriba.", "- **Anexo I**: ver el bloque del agua.",
                        "Sobre el Anexo, véase la sección anterior.", "- **Tarifa**: ídem."):
            p = nl.problems("- **Agua**: sube. [F](https://p/1)\n" + relleno, mat)
            self.assertTrue(any("remite a otro" in x for x in p), relleno)
        self.assertEqual(nl.problems("- **Agua**: ver la tarifa nueva en el link. [F](https://p/1)", mat), [])

    def test_numeros_solo_de_los_documentos(self):
        mat = [{"url": "https://p/1", "texto": "Actualización del 79 % en diez tramos desde el 1 de diciembre de 2026."}]
        ok = "# El agua sube 79%\n- **Agua**: 79 % desde el 1 de diciembre de 2026. [F](https://p/1)"
        self.assertEqual(nl.problems(ok, mat), [])
        p = nl.problems(ok.replace("diciembre", "diciembre, unos 1.500 pesos más"), mat)
        self.assertTrue(any("texto dice 1.500" in x for x in p))
        semana = "Semana del 14/11/2026 al 20/11/2026."
        self.assertEqual(nl.problems(ok + "\nCubre del 14 al 20 de noviembre.", mat, contexto=semana), [])

    def test_titulo_de_lo_aprobado_en_la_semana(self):
        ses = "Sesión N° 28 (19 de noviembre de 2026)"
        mat = [{"tipo": "aprobado", "sesion": ses, "url": "https://p/1",
                "titulo": "El Concejo Deliberante designó a los integrantes de la Junta Electoral Municipal"},
               {"tipo": "ordenanza 15.816/26 (Boletín Oficial N° 831)", "ordenanza": "15.816/26", "boletin": 831,
                "url": "https://bo/831.pdf"}]
        cuerpo = "\n- **Junta**: designó. [F](https://p/1)\n- **Agua**: 15.816/26. [BO](https://bo/831.pdf)"
        self.assertEqual(nl.problems("# El Concejo designó la Junta Electoral" + cuerpo, mat, held=[ses]), [])
        for mal in ("# El Boletín Oficial publicó la tarifa del agua", "# La tarifa del agua sube"):
            self.assertTrue(any("tiene que ser lo aprobado" in x for x in nl.problems(mal + cuerpo, mat, held=[ses])))
        self.assertEqual(nl.problems("# La tarifa del agua sube" + cuerpo, mat), [])  # sin sesión en la semana

    def test_nombres_de_quien_se_nombra(self):
        junta = ("El Concejo designó a los integrantes titulares de la Junta Electoral. Los titulares son Ana Prueba, "
                 "Juan Ficticio y Pedro J. Inventado. Los suplentes son Rosa Ejemplo y Luis Muestra.")
        self.assertEqual(nl.nombres(junta), ["Ana Prueba", "Juan Ficticio", "Pedro J. Inventado", "Rosa Ejemplo",
                                             "Luis Muestra"])
        taxi = "AUTORÍZASE, por vía de excepción, al Sr. Leandro Alberto FALSARIO la continuidad del taxi."
        self.assertEqual(nl.nombres(taxi), ["Leandro Alberto FALSARIO"])
        # título cortado de la ordenanza + texto: "Leandro Alberto" no es otra persona
        self.assertEqual(nl.nombres("AUTORÍZASE al Sr. Leandro Alberto - " + taxi), ["Leandro Alberto FALSARIO"])
        mat = [{"url": "https://p/1", "titulo": "Junta Electoral", "texto": junta},
               {"url": "https://p/1", "titulo": "Taxi", "texto": taxi}]
        ok = "- **Junta**: Ana Prueba, Juan Ficticio, Pedro J. Inventado; suplentes Rosa Ejemplo y Luis Muestra. [F](https://p/1)"
        self.assertTrue(any("nombre de Leandro Alberto FALSARIO" in x for x in nl.problems(ok, mat)))
        self.assertEqual(nl.problems(ok + "\n- **Taxi**: a Leandro Alberto Falsario.", mat), [])
        p = nl.problems(ok.replace(" y Luis Muestra", "") + "\n- **Taxi**: Falsario.", mat)
        self.assertEqual(p, ["\"Junta Electoral\" va con el nombre de Luis Muestra: escribilo junto a la ordenanza o el tema"])
        social = "CONDÓNASE la deuda de la Sra. Ana Prueba por razones de salud."  # sin nombre: no se exige
        self.assertEqual(nl.problems("- **Tasa**: condonación. [F](https://p/1)", [{"url": "https://p/1", "titulo": "T",
                                                                                     "texto": social}]), [])

    def test_boletin_agrupado(self):
        pdf = "https://bo/831.pdf"
        mat = [{"url": pdf, "ordenanza": "15.816/26", "boletin": 831}]
        sec = "## Ordenanzas publicadas en el Boletín Oficial\n" + "".join(f"- **Tema {i}**: x. [BO]({pdf})\n" for i in "abcd")
        self.assertEqual(nl.problems(sec + "## Fuentes (links)\n- a\n- b\n", mat), [])
        self.assertTrue(any("agrupá" in x for x in nl.problems(sec + "- **Tema e**: x.\n", mat)))
        # 60 = "**Tema d**:" (2) + 56 + "fin." + "[BO](url)"
        larga = sec.replace("- **Tema d**: x.", "- **Tema d**: " + "palabra " * 56 + "fin.")
        self.assertEqual(nl.problems(larga, mat), [])
        p = nl.problems(larga.replace("fin.", "fin otra."), mat)
        self.assertTrue(any("tiene 61 palabras" in x for x in p), p)

    def test_sin_resultado(self):
        doc = {"items": [
            {"key": "05001-73/2008", "que_dice": "Dictamen: designación ciudadanos para integrar la Junta Electoral Municipal "
                                                 "en el marco del artículo 197 de la Carta Orgánica Municipal."},
            {"key": "1926/2026", "que_dice": "Dictamen: Convocatoria al Registro de adhesión para la Obra “Nexo de agua” "
                                             "(captación, impulsión y acumulación) para el Barrio Villa Lolog.",
             "impact_tags": ["💧 Servicios"]},
            {"key": "2", "que_dice": "EE-2026-21429-DAD #SG 11/11/2026"}]}  # resto del PDF: no es un punto
        nota = {"title": "El Concejo designó a los integrantes de la Junta Electoral Municipal",
                "text": "Designó la Junta Electoral según el artículo 197 de la Carta Orgánica Municipal."}
        out = nl.sin_resultado(doc, [nota], "Sesión N° 28 (19 de noviembre de 2026)", "https://od/28.pdf")
        self.assertEqual(out[0]["texto"], "De los 2 puntos del orden del día de la Sesión N° 28, el Concejo publicó el "
                                          "resultado de 1.")  # cifras del código, no de Haiku
        p = nl.problems(out[0]["texto"] + " [OD](https://od/28.pdf) 1926/2026", out[:2])
        self.assertTrue(any("negrita" in x for x in p))
        self.assertEqual([m.get("expediente") for m in out[1:]], ["1926/2026"])
        self.assertTrue(any("1926/2026" in x for x in nl.problems(out[0]["texto"] + " [OD](https://od/28.pdf)", out)))

    def test_sin_comentarios_sobre_el_newsletter(self):
        mat = [{"url": "https://p/1"}]
        for c in ("Este resumen incluye también lo aprobado en la Sesión 27.", "Como dijimos, el agua sube.",
                  "La nota estaba pendiente."):
            self.assertTrue(any("comenta el propio" in x for x in nl.problems(c + " [F](https://p/1)", mat)), c)
        self.assertEqual(nl.problems("El Concejo designó seis ciudadanos. [F](https://p/1)", mat), [])  # en letras: va

    def test_anexo_con_total_y_sin_tope(self):
        bo, dig = "https://bo/831.pdf", "https://digesto/15816.pdf"
        mat = [{"url": bo, "ordenanza": "15.816/26", "boletin": 831, "texto": ""},
               {"tipo": "Anexo de la ordenanza 15.816/26", "titulo": "Anexo 15.816/26", "url": dig,
                "texto": "Actualización total del setenta y nueve por ciento (79 %) en diez tramos: 20 % en octubre."}]
        larga = ("## Ordenanzas publicadas en el Boletín Oficial\n- **Agua**: la 15.816/26 actualiza la tarifa un 79 % en "
                 "diez tramos, 20 % en octubre" + " y sigue" * 30 + f". [BO]({bo}) · [Anexo]({dig})\n")
        self.assertEqual(nl.problems(larga, mat), [])  # más de 60 palabras: el Anexo no se recorta
        p = nl.problems(larga.replace("79 %", "79 puntos"), mat)
        self.assertTrue(any("total del 79 %" in x for x in p), p)


if __name__ == "__main__":
    unittest.main()
