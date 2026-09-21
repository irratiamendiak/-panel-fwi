#!/usr/bin/env python3
"""
Actualización diaria del panel FWI. Pensado para GitHub Actions, pero también se
puede ejecutar a mano.

Qué hace:
  1. Lee datos/historial.csv (columnas: fecha,estacion,temperatura,humedad,viento,lluvia).
  2. Descarga de Euskalmet los días que faltan hasta hoy: valores de las 12:00 y lluvia
     de las 24 h anteriores. Reutiliza euskalmet_fwi_datos.py (debe estar al lado).
  3. Recalcula la cascada FFMC/DMC/DC y de ahí ISI, BUI y FWI de cada estación.
  4. Escribe docs/data/fwi.json (lo lee la web) y actualiza datos/historial.csv.

Uso a mano:
  py actualizar_fwi.py --clave privateKey.pem --email TU_EMAIL

Opciones:
  --hasta AAAA-MM-DD   último día a descargar (por defecto, hoy si ya pasó la hora del dato
                       más una hora; si no, ayer)
  --desde AAAA-MM-DD   primer día si una estación no tiene histórico todavía
  --hora 12            hora del dato
  --ffmc 85 --dmc 6 --dc 15   códigos antes del primer día del histórico

El email también puede darse con la variable de entorno EUSKALMET_EMAIL.
"""
import argparse
import csv
import json
import math
import os
import sys
from bisect import bisect_left, bisect_right
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import euskalmet_fwi_datos as ew

ZONA = ZoneInfo("Europe/Madrid")
HISTORIAL = Path("datos/historial.csv")
SENSORES = Path("datos/sensores.json")
SALIDA = Path("docs/data/fwi.json")
CAMPOS = ["fecha", "estacion", "temperatura", "humedad", "viento", "lluvia"]
# Factores de duración del día por mes (Van Wagner y Pickett, 1985; hemisferio norte)
DMC_L = [6.5, 7.5, 9.0, 12.8, 13.9, 13.9, 12.4, 10.9, 9.4, 8.0, 7.0, 6.0]
DC_L = [-1.6, -1.6, -1.6, 0.9, 3.8, 5.8, 6.4, 5.0, 2.4, 0.4, -1.6, -1.6]
HUECO_MAX = 14       # días: con un hueco mayor entre dos lecturas se reinician los códigos
CALENTAMIENTO = 45   # días tras un reinicio (o el inicio del histórico) con valores aún poco fiables
DIAS_JSON = 400      # días recientes que se publican en la web
CLASES = [("Muy bajo", 5.2), ("Bajo", 11.2), ("Moderado", 21.3),
          ("Alto", 38.0), ("Muy alto", 50.0), ("Extremo", math.inf)]


# ---------------------------------------------------------------- fórmulas FWI
def ffmc_step(T, H, W, r, F0):
    mo = 147.2 * (101 - F0) / (59.5 + F0)
    if r > 0.5:
        rf = r - 0.5
        mr = mo + 42.5 * rf * math.exp(-100 / (251 - mo)) * (1 - math.exp(-6.93 / rf))
        if mo > 150:
            mr += 0.0015 * (mo - 150) ** 2 * math.sqrt(rf)
        mo = min(mr, 250)
    Ed = 0.942 * H ** 0.679 + 11 * math.exp((H - 100) / 10) + 0.18 * (21.1 - T) * (1 - math.exp(-0.115 * H))
    if mo > Ed:
        ko = 0.424 * (1 - (H / 100) ** 1.7) + 0.0694 * math.sqrt(W) * (1 - (H / 100) ** 8)
        kd = ko * 0.581 * math.exp(0.0365 * T)
        m = Ed + (mo - Ed) * 10 ** (-kd)
    else:
        Ew = 0.618 * H ** 0.753 + 10 * math.exp((H - 100) / 10) + 0.18 * (21.1 - T) * (1 - math.exp(-0.115 * H))
        if mo < Ew:
            k1 = 0.424 * (1 - ((100 - H) / 100) ** 1.7) + 0.0694 * math.sqrt(W) * (1 - ((100 - H) / 100) ** 8)
            kw = k1 * 0.581 * math.exp(0.0365 * T)
            m = Ew - (Ew - mo) * 10 ** (-kw)
        else:
            m = mo
    F = 59.5 * (250 - m) / (147.2 + m)
    return min(101.0, max(0.0, F))


def dmc_step(T, H, r, Do, Le):
    if T < -1.1:
        T = -1.1
    rk = 1.894 * (T + 1.1) * (100 - H) * Le * 0.0001
    P = Do
    if r > 1.5:
        re = 0.92 * r - 1.27
        mo = 20 + math.exp(5.6348 - Do / 43.43)
        if Do <= 33:
            b = 100 / (0.5 + 0.3 * Do)
        elif Do <= 65:
            b = 14 - 1.3 * math.log(Do)
        else:
            b = 6.2 * math.log(Do) - 17.2
        mr = mo + 1000 * re / (48.77 + b * re)
        P = max(0.0, 244.72 - 43.43 * math.log(mr - 20))
    return P + rk


def dc_step(T, r, Do, Lf):
    if T < -2.8:
        T = -2.8
    pe = max(0.0, (0.36 * (T + 2.8) + Lf) / 2)
    P = Do
    if r > 2.8:
        rd = 0.83 * r - 1.27
        Qo = 800 * math.exp(-Do / 400)
        Qr = Qo + 3.937 * rd
        P = max(0.0, 400 * math.log(800 / Qr))
    return P + pe


def isi_calc(F, W):
    fW = math.exp(0.05039 * W)
    m = 147.2 * (101 - F) / (59.5 + F)
    fF = 91.9 * math.exp(-0.1386 * m) * (1 + m ** 5.31 / 4.93e7)
    return 0.208 * fW * fF


def bui_calc(P, D):
    if P + 0.4 * D == 0:
        return 0.0
    if P <= 0.4 * D:
        U = 0.8 * P * D / (P + 0.4 * D)
    else:
        U = P - (1 - 0.8 * D / (P + 0.4 * D)) * (0.92 + (0.0114 * P) ** 1.7)
    return max(0.0, U)


def fwi_calc(isi, bui):
    fD = 0.626 * bui ** 0.809 + 2 if bui <= 80 else 1000 / (25 + 108.64 * math.exp(-0.023 * bui))
    B = 0.1 * isi * fD
    return math.exp(2.72 * (0.434 * math.log(B)) ** 0.647) if B > 1 else B


def clase(f):
    for nombre, tope in CLASES:
        if f <= tope:
            return nombre
    return CLASES[-1][0]


def cascada(filas, inicial):
    """filas: lista de dicts de UNA estación ordenados por fecha.

    Si entre dos lecturas hay más de HUECO_MAX días, los códigos se reinician con los valores
    iniciales, porque la humedad del combustible ya no se puede seguir. Los CALENTAMIENTO días
    siguientes se marcan como "calentando": el resultado es orientativo y no cuenta para la
    climatología.
    """
    F, M, D = inicial
    prev = None
    inicio = None
    salida = []
    for f in filas:
        d = date.fromisoformat(f["fecha"])
        if prev is None:
            inicio = d
        elif (d - prev).days > HUECO_MAX:
            F, M, D = inicial
            inicio = d
        hueco = prev is not None and (d - prev).days != 1
        prev = d
        T, H, W, R = f["temperatura"], f["humedad"], f["viento"], f["lluvia"]
        F = ffmc_step(T, H, W, R, F)
        M = dmc_step(T, H, R, M, DMC_L[d.month - 1])
        D = dc_step(T, R, D, DC_L[d.month - 1])
        isi = isi_calc(F, W)
        bui = bui_calc(M, D)
        fwi = fwi_calc(isi, bui)
        salida.append({
            "fecha": f["fecha"], "T": T, "H": H, "W": W, "R": R,
            "ffmc": round(F, 1), "dmc": round(M, 1), "dc": round(D, 1),
            "isi": round(isi, 1), "bui": round(bui, 1), "fwi": round(fwi, 1),
            "clase": clase(fwi), "hueco": hueco,
            "calentando": (d - inicio).days < CALENTAMIENTO,
        })
    return salida


def referencia(cascadas):
    """(estación, mes) -> lista ordenada de FWI históricos, sin los días de calentamiento."""
    ref = {}
    for nombre, dias in cascadas.items():
        for d in dias:
            if not d["calentando"]:
                ref.setdefault((nombre, int(d["fecha"][5:7])), []).append(d["fwi"])
    for v in ref.values():
        v.sort()
    return ref


def percentil_valor(v, q):
    return v[min(len(v) - 1, max(0, math.ceil(q * len(v)) - 1))]


def rango_percentil(v, x):
    """Percentil (0-100) de x dentro de la lista ordenada v, con rango medio para los empates."""
    return round(100 * (bisect_left(v, x) + bisect_right(v, x)) / 2 / len(v))


# ---------------------------------------------------------------- histórico
def leer_historial():
    datos = {n: {} for n in ew.ESTACIONES}
    if HISTORIAL.exists():
        with open(HISTORIAL, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                n = r.get("estacion")
                if n not in datos:
                    continue
                try:
                    date.fromisoformat(r["fecha"])
                    datos[n][r["fecha"]] = {
                        "fecha": r["fecha"],
                        "temperatura": float(r["temperatura"]),
                        "humedad": float(r["humedad"]),
                        "viento": float(r["viento"]),
                        "lluvia": float(r["lluvia"]),
                    }
                except (ValueError, KeyError):
                    continue
    return datos


def obtener_dia(cli, alm, sensores, cod, dia, dia_fin, hora):
    """Devuelve (fila, mensaje). fila es None si no se puede guardar todavía."""
    # En los últimos días se exige la lluvia completa (puede que aún lleguen datos);
    # en los antiguos se admite hasta un 10 % de huecos.
    minimo = ew.LECTURAS_POR_DIA if dia >= dia_fin - timedelta(days=2) else ew.MIN_LLUVIA
    datos, n, motivo = ew.obtener_dia(cli, alm, sensores, cod, dia, hora, minimo)
    if datos is None:
        return None, motivo + ("; se reintentará" if dia >= dia_fin - timedelta(days=6) else "")
    t, h, w, ll = datos
    aviso = "" if n >= ew.LECTURAS_POR_DIA else f" (lluvia: {n}/{ew.LECTURAS_POR_DIA} lecturas)"
    fila = {"fecha": dia.isoformat(), "temperatura": round(t, 1), "humedad": round(h, 1),
            "viento": round(w * 3.6, 1), "lluvia": ll}
    return fila, aviso


def descargar(cli, sensores, datos, dia_fin, primera, hora, max_dias):
    alm = ew.Almacen(cli)
    nuevas = 0
    for nombre, cod in ew.ESTACIONES.items():
        sens = sensores.get(cod)
        if not sens:
            print(f"{nombre}: sin sensores detectados, se omite.")
            continue
        fechas = datos[nombre]
        desde = date.fromisoformat(max(fechas)) + timedelta(days=1) if fechas else primera
        desde = min(desde, dia_fin - timedelta(days=6))      # reintenta huecos de la última semana
        desde = max(desde, dia_fin - timedelta(days=max_dias))  # tope de seguridad
        dia = desde
        while dia <= dia_fin:
            if dia.isoformat() not in fechas:
                try:
                    fila, msg = obtener_dia(cli, alm, sensores, cod, dia, dia_fin, hora)
                except RuntimeError as e:
                    fila, msg = None, str(e)[:120]
                if fila:
                    fechas[fila["fecha"]] = fila
                    nuevas += 1
                    print(f"{dia} {nombre}: T={fila['temperatura']} HR={fila['humedad']} "
                          f"V={fila['viento']} km/h lluvia={fila['lluvia']} mm{msg}")
                else:
                    print(f"{dia} {nombre}: omitido ({msg})")
            dia += timedelta(days=1)
    return nuevas


# ---------------------------------------------------------------- salida
def escribir(datos, inicial, hora, ahora, dias_json):
    orden = list(ew.ESTACIONES)
    filas_csv = []
    cascadas = {}
    for nombre in ew.ESTACIONES:
        filas = [datos[nombre][k] for k in sorted(datos[nombre])]
        filas_csv += [[f["fecha"], nombre, f["temperatura"], f["humedad"], f["viento"], f["lluvia"]] for f in filas]
        cascadas[nombre] = cascada(filas, inicial)
    filas_csv.sort(key=lambda r: (r[0], orden.index(r[1])))

    HISTORIAL.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORIAL, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(CAMPOS)
        w.writerows(filas_csv)

    ref = referencia(cascadas)
    clim = {}
    for (nombre, mes), v in ref.items():
        if len(v) >= 30:
            clim.setdefault(nombre, {})[str(mes)] = {
                "n": len(v), "p50": percentil_valor(v, 0.50), "p75": percentil_valor(v, 0.75),
                "p90": percentil_valor(v, 0.90), "p95": percentil_valor(v, 0.95),
                "p98": percentil_valor(v, 0.98)}

    estaciones = []
    for nombre, cod in ew.ESTACIONES.items():
        recientes = cascadas[nombre][-dias_json:]
        for d in recientes:
            v = ref.get((nombre, int(d["fecha"][5:7])))
            d["pct"] = rango_percentil(v, d["fwi"]) if (v and len(v) >= 30 and not d["calentando"]) else None
        estaciones.append({"nombre": nombre, "codigo": cod, "n_total": len(cascadas[nombre]),
                           "dias": recientes})

    SALIDA.parent.mkdir(parents=True, exist_ok=True)
    paquete = {
        "actualizado": ahora.isoformat(timespec="minutes"),
        "hora_dato": hora,
        "inicial": {"ffmc": inicial[0], "dmc": inicial[1], "dc": inicial[2]},
        "parametros": {"hueco_max": HUECO_MAX, "calentamiento": CALENTAMIENTO},
        "climatologia": clim,
        "estaciones": estaciones,
    }
    SALIDA.write_text(json.dumps(paquete, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser(description="Actualiza el histórico y el JSON del panel FWI")
    p.add_argument("--clave", required=True, help="Ruta al fichero con la clave PRIVADA (PEM)")
    p.add_argument("--email", default=os.environ.get("EUSKALMET_EMAIL"), help="Email de la solicitud de clave")
    p.add_argument("--emisor", default="panel-fwi")
    p.add_argument("--hasta", default=None)
    p.add_argument("--desde", default=None)
    p.add_argument("--hora", type=int, default=12)
    p.add_argument("--max-dias", type=int, default=45, help="Tope de días a recuperar hacia atrás")
    p.add_argument("--dias-json", type=int, default=DIAS_JSON, help="Días recientes que se publican en la web")
    p.add_argument("--ffmc", type=float, default=85.0)
    p.add_argument("--dmc", type=float, default=6.0)
    p.add_argument("--dc", type=float, default=15.0)
    a = p.parse_args()

    if not a.email:
        print("Falta el email (--email o variable EUSKALMET_EMAIL).")
        return 1
    ahora = datetime.now(ZONA)
    if a.hasta:
        dia_fin = date.fromisoformat(a.hasta)
    else:
        dia_fin = ahora.date() if ahora.hour >= a.hora + 1 else ahora.date() - timedelta(days=1)
    primera = date.fromisoformat(a.desde) if a.desde else dia_fin

    try:
        ew.crear_token(a.clave, a.email, a.emisor)  # solo para validar la clave pronto
    except FileNotFoundError:
        print(f"No encuentro el fichero de la clave: {a.clave}")
        return 1
    except Exception as e:
        print("No he podido firmar el token con esa clave:", type(e).__name__, str(e)[:200])
        return 1

    cli = ew.Cliente(lambda: ew.crear_token(a.clave, a.email, a.emisor))
    ew.FICHERO_SENSORES = SENSORES
    SENSORES.parent.mkdir(parents=True, exist_ok=True)
    try:
        sensores = ew.cargar_sensores(cli, dia_fin - timedelta(days=1))
    except RuntimeError as e:
        print(e)
        return 1
    if not sensores:
        print("No se ha podido detectar ningún sensor. Revisa la clave y el email.")
        return 1

    sensores_inicio = json.loads(json.dumps(sensores))
    datos = leer_historial()
    print(f"Histórico: {sum(len(v) for v in datos.values())} filas. Descargando hasta {dia_fin}...")
    nuevas = descargar(cli, sensores, datos, dia_fin, primera, a.hora, a.max_dias)
    if sensores != sensores_inicio:
        SENSORES.write_text(json.dumps(sensores, ensure_ascii=False, indent=2), encoding="utf-8")
    escribir(datos, (a.ffmc, a.dmc, a.dc), a.hora, ahora, a.dias_json)
    print(f"\nListo: {nuevas} filas nuevas, {cli.llamadas} llamadas a la API. "
          f"Escrito {SALIDA} y {HISTORIAL}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
