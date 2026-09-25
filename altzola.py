"""
Altzola combina una estación real de Euskalmet (C078: temperatura, humedad y lluvia) con el
viento (velocidad y dirección) que estima el modelo de Open-Meteo en sus mismas coordenadas.
Altzola no tiene sensor de viento, y no hay ninguna estación cercana de la que tomarlo prestado
como se hizo con la lluvia de Zarautz: el viento es la variable más sensible al lugar exacto
(relieve, exposición del valle), así que tomarlo del modelo justo en ese punto es más fiable que
tomarlo prestado de una estación real pero en otro sitio.

Temperatura, humedad y lluvia SÍ son mediciones reales de la propia estación.
"""
import euskalmet_fwi_datos as ew
import zarautz as zr   # reutiliza sensores_parciales(), sin más dependencia entre ambas

ESTACION = "C078"
NOMBRE = "Altzola"
COORD = (43.2365, -2.4002, 30)
NOTA = ("Puntu honetako haizea (abiadura eta norabidea) Open-Meteo ereduaren estimazioa da "
        "Altzolako koordenatuetan, ez neurketa bat: estazioak (C078) ez du haize-neurgailurik, "
        "eta ez dago ondoan halakorik duen beste estaziorik. Tenperatura, hezetasuna eta euria "
        "estazio errealekoak dira.")


def asegurar_sensores(cli, sens, dia, alm=None, hora=None):
    """Completa 'sens' con los sensores que falten de Altzola (temperatura, humedad, lluvia).

    Devuelve True si están las tres. No hace falta viento: eso lo pone el modelo."""
    faltan = [v for v in ("temperatura", "humedad") if v not in sens]
    if faltan:
        medidas = {v: ew.MEDIDAS[v] for v in faltan}
        sens.update(zr.sensores_parciales(cli, ESTACION, dia, medidas))
    if "lluvia" not in sens:
        nuevo = zr.sensores_parciales(cli, ESTACION, dia, {"lluvia": ew.MEDIDAS["lluvia"]})
        if "lluvia" in nuevo:
            sens["lluvia"] = nuevo["lluvia"]
    return all(v in sens for v in ("temperatura", "humedad", "lluvia"))


def leer_dia(cli, alm, sens, dia, hora, viento, lluvia_manual=None, rellenar_huecos=True):
    """(fila, motivo) de un día. 'viento' es (velocidad_kmh, direccion_o_None) ya sacado del
    modelo para ese día, o None si el modelo no tiene ese día. 'lluvia_manual' (mm) sustituye a la
    lluvia de la estación ese día, para huecos que se conocen de otra forma (p. ej. "no llovió").
    Si Altzola ha cambiado de sensor, se vuelve a detectar ese mismo día y se reintenta; lo que
    siga faltando se rellena con la regla general (ew.rellenar)."""
    def leer_principales():
        t = ew.valor_puntual(alm, ESTACION, sens, "temperatura", dia, hora)
        h = ew.valor_puntual(alm, ESTACION, sens, "humedad", dia, hora)
        return t, h

    t, h = leer_principales()
    if None in (t, h):
        nuevos = zr.sensores_parciales(cli, ESTACION, dia,
                                       {"temperatura": ew.MEDIDAS["temperatura"], "humedad": ew.MEDIDAS["humedad"]})
        if any(nuevos.get(v) and nuevos[v] != sens.get(v) for v in nuevos):
            alm.olvidar(ESTACION, [dia])
            sens.update(nuevos)
            t, h = leer_principales()
    w, dv = viento if viento is not None else (None, None)   # viento: siempre del modelo, por diseño

    minimo = ew.LECTURAS_POR_DIA - 6
    origen = []
    if lluvia_manual is not None:
        ll, n = lluvia_manual, ew.LECTURAS_POR_DIA
        origen.append("lluvia:manual")
    else:
        ll, n = ew.lluvia_24h(alm, ESTACION, sens, dia, hora)
        if n < minimo:
            nuevo = zr.sensores_parciales(cli, ESTACION, dia, {"lluvia": ew.MEDIDAS["lluvia"]})
            if nuevo.get("lluvia") and nuevo["lluvia"] != sens.get("lluvia"):
                alm.olvidar(ESTACION, [dia])
                sens["lluvia"] = nuevo["lluvia"]
                ll, n = ew.lluvia_24h(alm, ESTACION, sens, dia, hora)

    if not rellenar_huecos and ew.faltas(t, h, w, n, minimo):
        return None, "falta " + ew.faltas(t, h, w, n, minimo) + " (Altzola)"
    # 1) temperatura y humedad que falten a la hora exacta: valor más desfavorable de Altzola en ±50 min
    #    (el viento no: es siempre del modelo)
    vals = {"temperatura": t, "humedad": h}
    _, notas = ew.completar_ventana(alm, ESTACION, sens, dia, hora, vals, None)
    t, h = vals["temperatura"], vals["humedad"]
    origen += notas
    # 2) regla general: lluvia de la vecina más cercana (Arrasate, luego Bidania) y, si no, de Open-Meteo;
    #    temperatura y humedad que sigan faltando, de Open-Meteo.
    res, motivo = ew.rellenar(cli, alm, ESTACION, dia, hora, t, h, w, dv, ll, n, minimo, origen=origen)
    if res is None:
        return None, motivo + " (Altzola)"
    t, h, w, dv, ll, origen = res

    fila = {"fecha": dia.isoformat(), "temperatura": round(t, 1), "humedad": round(h, 1),
            "viento": round(w, 1), "lluvia": round(ll, 2),
            "direccion": round(dv) if dv is not None else None, "origen": origen}
    return fila, (f"rellenado: {origen}" if origen else "")
