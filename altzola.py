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


def leer_dia(cli, alm, sens, dia, hora, viento):
    """(fila, motivo) de un día. 'viento' es (velocidad_kmh, direccion_o_None) ya sacado del
    modelo para ese día, o None si el modelo no tiene ese día. Si Altzola ha cambiado de sensor,
    se vuelve a detectar ese mismo día y se reintenta, igual que con Zarautz."""
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
    if None in (t, h):
        faltan = [k for k, v in (("temperatura", t), ("humedad", h)) if v is None]
        return None, f"sin lectura a las {hora:02d}:00 de " + ", ".join(faltan) + " (Altzola)"

    if viento is None:
        return None, "sin viento del modelo (Open-Meteo) ese día"
    w, dv = viento

    ll, n = ew.lluvia_24h(alm, ESTACION, sens, dia, hora)
    if n < ew.LECTURAS_POR_DIA - 6:
        nuevo = zr.sensores_parciales(cli, ESTACION, dia, {"lluvia": ew.MEDIDAS["lluvia"]})
        if nuevo.get("lluvia") and nuevo["lluvia"] != sens.get("lluvia"):
            alm.olvidar(ESTACION, [dia])
            sens["lluvia"] = nuevo["lluvia"]
            ll, n = ew.lluvia_24h(alm, ESTACION, sens, dia, hora)
    if n == 0:
        return None, "sin lecturas de lluvia (Altzola)"
    if n < ew.LECTURAS_POR_DIA - 6:
        return None, f"lluvia incompleta en Altzola ({n}/{ew.LECTURAS_POR_DIA})"

    fila = {"fecha": dia.isoformat(), "temperatura": round(t, 1), "humedad": round(h, 1),
            "viento": round(w, 1), "lluvia": round(ll, 2),
            "direccion": round(dv) if dv is not None else None}
    return fila, ""
