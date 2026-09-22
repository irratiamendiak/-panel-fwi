"""
Previsión del FWI a 0-3 días con Open-Meteo (modelo best_match), corregida por estación.

Cómo funciona (ver evaluacion_prevision.md):
  * Se toman las previsiones horarias de temperatura, humedad, viento y lluvia de cada estación.
  * A las 12:00 de cada día se corrigen con los ajustes por estación y antelación
    (datos/correcciones_prevision.json) y se hace avanzar la cascada FFMC/DMC/DC desde el último día real.
  * El rango se obtiene de los errores históricos de la previsión (datos/incertidumbre_prevision.json):
    contiene aproximadamente el 80 % de los casos evaluados en 2025-2026.

Fuente de datos meteorológicos: Open-Meteo.com (licencia CC BY 4.0, uso no comercial gratuito).
"""
import json
import math
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

URL = "https://api.open-meteo.com/v1/forecast"
MODELO = "best_match"
BINS = [0.0, 5.2, 11.2, 21.3, 38.0, math.inf]
ATRIBUCION = "Previsión: Open-Meteo.com (CC BY 4.0), modelo best_match corregido con datos de las estaciones."


def cargar_json(ruta):
    ruta = Path(ruta)
    return json.loads(ruta.read_text(encoding="utf-8")) if ruta.exists() else None


def descargar_horario(lat, lon, alt, reintentos=3, espera=15):
    """Devuelve {'AAAA-MM-DDTHH:MM': (T, HR, viento_kmh, lluvia_mm_de_la_hora_previa)}."""
    params = {"latitude": lat, "longitude": lon, "elevation": alt, "timezone": "Europe/Madrid",
              "hourly": "temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m,precipitation",
              "past_days": 3, "forecast_days": 5, "models": MODELO}
    ultimo = None
    for i in range(reintentos):
        try:
            r = requests.get(URL, params=params, timeout=60)
        except requests.RequestException as e:
            ultimo = type(e).__name__
            time.sleep(espera)
            continue
        if r.status_code == 200:
            h = r.json().get("hourly", {})
            t = h.get("time", [])
            cols = [h.get(k) for k in ("temperature_2m", "relative_humidity_2m", "wind_speed_10m", "wind_direction_10m", "precipitation")]
            if not t or any(c is None for c in cols):
                raise RuntimeError("Open-Meteo devolvió una respuesta sin las variables esperadas")
            return {ts: tuple(c[i] for c in cols) for i, ts in enumerate(t)}
        ultimo = f"HTTP {r.status_code}"
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(espera * (i + 1))
            continue
        raise RuntimeError(f"Open-Meteo respondió {ultimo}: {r.text[:200]}")
    raise RuntimeError(f"Open-Meteo no responde ({ultimo})")


def entradas_dia(tabla, dia, hora=12):
    """(T, HR, viento, direccion, lluvia24h, horas_lluvia) de un día; None si falta algún dato.

    horas_lluvia son las 24 parejas (marca, mm) de la ventana de lluvia: de las 13:00 del día anterior
    a las 12:00 del día (la previsión de una hora es la lluvia de la hora previa)."""
    ts = f"{dia.isoformat()}T{hora:02d}:00"
    v = tabla.get(ts)
    if not v or any(x is None for x in v[:3]):
        return None
    ini = datetime(dia.year, dia.month, dia.day, hora) - timedelta(hours=23)
    horas = []
    for i in range(24):
        m = (ini + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M")
        x = tabla.get(m)
        if not x or x[4] is None:
            return None
        horas.append((m, float(x[4])))
    return (v[0], v[1], v[2], v[3], sum(p for _, p in horas), horas)


def corregir(corr, T, H, W):
    """Aplica la corrección por estación (sesgo de T, recta de HR, factor de viento)."""
    c = corr["T"]
    if c[0] == "suma":
        T = T + c[1]
    c = corr["H"]
    if c[0] == "lineal":
        H = min(100.0, max(5.0, c[1] + c[2]*H))
    c = corr["W"]
    if c[0] == "factor":
        W = max(0.0, W*c[1])
    return T, H, W


def componente_sur(direccion_grados):
    """1 = viento del sur en calma perfecta, 0 = viento del norte; None si no hay dato."""
    if direccion_grados is None:
        return None
    return max(0.0, -math.cos(math.radians(direccion_grados)))


def rango(inc, h, fwi):
    """Intervalo [previsto+P10, previsto+P90] del FWI (≥ 0) según el horizonte h y el tramo del valor previsto."""
    filas = inc[str(min(3, max(0, h)))]
    f = filas[next(i for i in range(len(BINS) - 1) if fwi < BINS[i + 1])]
    return max(0.0, fwi + f["p10"]), max(0.0, fwi + f["p90"])
