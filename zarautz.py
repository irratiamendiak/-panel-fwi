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


def leer_dia(alm, sens, dia, hora):
    """(fila, motivo) de un día. fila es None si no se puede construir ese día."""
    t = ew.valor_puntual(alm, ESTACION, sens, "temperatura", dia, hora)
    h = ew.valor_puntual(alm, ESTACION, sens, "humedad", dia, hora)
    w = ew.valor_puntual(alm, ESTACION, sens, "viento", dia, hora)
    if None in (t, h, w):
        faltan = [k for k, v in (("temperatura", t), ("humedad", h), ("viento", w)) if v is None]
        return None, f"sin lectura a las {hora:02d}:00 de " + ", ".join(faltan) + " (Zarautz)"
    dv = ew.valor_puntual(alm, ESTACION, sens, "direccion", dia, hora) if "direccion" in sens else None
    ll, n = ew.lluvia_24h(alm, ESTACION_LLUVIA, sens, dia, hora)
    if n == 0:
        return None, "sin lecturas de lluvia (Inurritza)"
    if n < ew.LECTURAS_POR_DIA - 6:
        return None, f"lluvia incompleta en Inurritza ({n}/{ew.LECTURAS_POR_DIA})"
    fila = {"fecha": dia.isoformat(), "temperatura": round(t, 1), "humedad": round(h, 1),
            "viento": round(w * 3.6, 1), "lluvia": round(ll, 2),
            "direccion": round(dv) if dv is not None else None}
    return fila, ""
