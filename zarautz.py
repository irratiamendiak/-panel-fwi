"""
Zarautz combina dos estaciones reales de Euskalmet, no una: temperatura, humedad, viento y
dirección de Zarautz (C064), y precipitación de Inurritza (C086), en el mismo municipio, porque
Zarautz no tiene sensor de lluvia. A diferencia de Mutriku, aquí todo son mediciones reales; solo
cambia que las cuatro variables no salen de un único sensor físico.

Este módulo no descarga nada por sí solo: da las piezas (detección de sensores y lectura de un
día) que usan tanto el proceso diario (actualizar_fwi.py) como el histórico (zarautz_historico.py).
"""
import euskalmet_fwi_datos as ew

ESTACION = "C064"          # Zarautz: temperatura, humedad, viento, dirección
ESTACION_LLUVIA = "C086"   # Inurritza: precipitación
NOMBRE = "Zarautz"
NOTA = ("La lluvia de este punto se toma de la estación de Inurritza (C086), en el mismo "
        "municipio: Zarautz (C064) no tiene sensor de precipitación.")


def sensores_parciales(cli, cod, dia, medidas):
    """Como euskalmet_fwi_datos.detectar_sensores, pero solo busca las medidas pedidas (dict
    variable -> "tipo/medida") y no falla si falta alguna: devuelve lo que encuentre."""
    ruta = (f"/euskalmet/readings/aggregated/summarized/byDay/forStation/"
            f"{cod}/at/{dia:%Y}/{dia:%m}/{dia:%d}")
    r = cli.get(ruta)
    if r.status_code != 200:
        return {}
    claves = sorted({it.get("key") for it in r.json().get("items", []) if it.get("key")})
    res = {}
    for var, sufijo in medidas.items():
        if not sufijo:
            continue
        cand = [k for k in claves if k.endswith("/" + sufijo)]
        if cand:
            sensor, tipo, medida = cand[0].split("/")
            res[var] = {"sensor": sensor, "tipo": tipo, "medida": medida}
    return res


def asegurar_sensores(cli, sens, dia, alm=None, hora=None):
    """Completa 'sens' (dict mutable) con los sensores que falten de Zarautz y de Inurritza.

    Devuelve True si ya están las cuatro variables necesarias (temperatura, humedad, viento,
    lluvia). La dirección es opcional: igual que en las demás estaciones, si no aparece en el
    catálogo de resúmenes diarios se prueba directamente con el sensor de viento (misma veleta),
    y solo se guarda si esa prueba devuelve un dato real (necesita 'alm' y 'hora')."""
    faltan = [v for v in ("temperatura", "humedad", "viento") if v not in sens]
    if faltan:
        medidas = {v: ew.MEDIDAS.get(v) for v in faltan}
        sens.update(sensores_parciales(cli, ESTACION, dia, medidas))
    if "direccion" not in sens and "viento" in sens:
        candidato = sensores_parciales(cli, ESTACION, dia, {"direccion": ew.MEDIDAS_OPCIONALES["direccion"]}).get("direccion")
        if not candidato:
            candidato = dict(sens["viento"], medida="mean_direction")
        if candidato and alm is not None and hora is not None:
            try:
                d = alm.hora(ESTACION, "direccion", {"direccion": candidato}, dia, hora)
                if d.get((hora, 0)) is not None:
                    sens["direccion"] = candidato
            except RuntimeError:
                pass
    if "lluvia" not in sens:
        nuevo = sensores_parciales(cli, ESTACION_LLUVIA, dia, {"lluvia": ew.MEDIDAS["lluvia"]})
        if "lluvia" in nuevo:
            sens["lluvia"] = nuevo["lluvia"]
    return all(v in sens for v in ("temperatura", "humedad", "viento", "lluvia"))


def leer_dia(cli, alm, sens, dia, hora, lluvia_manual=None, rellenar_huecos=True):
    """(fila, motivo) de un día. fila es None si no se puede construir ese día.

    Si Zarautz ha cambiado de sensor de temperatura/humedad/viento (o Inurritza, de lluvia),
    se vuelve a detectar ese mismo día y se reintenta, igual que con las demás estaciones.
    Lo que siga faltando se rellena con la regla general (ew.rellenar): lluvia de la vecina más
    cercana y, si no, de Open-Meteo; temperatura, humedad y viento, de Open-Meteo.
    'lluvia_manual' (mm) sustituye a la lluvia ese día (p. ej. "no llovió")."""
    def leer_principales():
        t = ew.valor_puntual(alm, ESTACION, sens, "temperatura", dia, hora)
        h = ew.valor_puntual(alm, ESTACION, sens, "humedad", dia, hora)
        w = ew.valor_puntual(alm, ESTACION, sens, "viento", dia, hora)
        return t, h, w

    t, h, w = leer_principales()
    if None in (t, h, w):
        nuevos = sensores_parciales(cli, ESTACION, dia,
                                    {"temperatura": ew.MEDIDAS["temperatura"], "humedad": ew.MEDIDAS["humedad"],
                                     "viento": ew.MEDIDAS["viento"]})
        if any(nuevos.get(v) and nuevos[v] != sens.get(v) for v in nuevos):
            alm.olvidar(ESTACION, [dia])
            sens.update(nuevos)
            t, h, w = leer_principales()
    dv = None
    if w is not None and "direccion" in sens:
        dv = ew.valor_puntual(alm, ESTACION, sens, "direccion", dia, hora)
    if dv is None and w is not None and "direccion" in sens:
        cand = dict(sens["viento"], medida="mean_direction")
        try:
            d = alm.hora(ESTACION, "direccion", {"direccion": cand}, dia, hora)
            if d.get((hora, 0)) is not None:
                sens["direccion"] = cand
                dv = ew.valor_puntual(alm, ESTACION, sens, "direccion", dia, hora)
        except RuntimeError:
            pass

    minimo = ew.LECTURAS_POR_DIA - 6
    origen = []
    if lluvia_manual is not None:
        ll, n = lluvia_manual, ew.LECTURAS_POR_DIA
        origen.append("lluvia:manual")
    else:
        ll, n = ew.lluvia_24h(alm, ESTACION_LLUVIA, sens, dia, hora)
        if n < minimo:
            nuevo = sensores_parciales(cli, ESTACION_LLUVIA, dia, {"lluvia": ew.MEDIDAS["lluvia"]})
            if nuevo.get("lluvia") and nuevo["lluvia"] != sens.get("lluvia"):
                alm.olvidar(ESTACION_LLUVIA, [dia])
                sens["lluvia"] = nuevo["lluvia"]
                ll, n = ew.lluvia_24h(alm, ESTACION_LLUVIA, sens, dia, hora)

    # Regla general: lluvia de la vecina más cercana a Inurritza (Zizurkil, luego Bidania) y, si no,
    # de Open-Meteo; temperatura, humedad y viento que falten, de Open-Meteo en las coordenadas de Zarautz.
    w_kmh = w * 3.6 if w is not None else None
    if not rellenar_huecos and ew.faltas(t, h, w, n, minimo):
        return None, "falta " + ew.faltas(t, h, w, n, minimo) + " (Zarautz)"
    res, motivo = ew.rellenar(cli, alm, ESTACION, dia, hora, t, h, w_kmh, dv, ll, n, minimo,
                              cod_lluvia=ESTACION_LLUVIA, origen=origen)
    if res is None:
        return None, motivo + " (Zarautz)"
    t, h, w_kmh, dv, ll, origen = res

    fila = {"fecha": dia.isoformat(), "temperatura": round(t, 1), "humedad": round(h, 1),
            "viento": round(w_kmh, 1), "lluvia": round(ll, 2),
            "direccion": round(dv) if dv is not None else None, "origen": origen}
    return fila, (f"rellenado: {origen}" if origen else "")
