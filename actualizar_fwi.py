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
import time
from bisect import bisect_left, bisect_right
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import euskalmet_fwi_datos as ew

try:
    import prevision as pv
except ImportError:      # la previsión es opcional
    pv = None

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
COORD = {   # nombre: (latitud, longitud, altitud)
    "Arrasate": (43.0695849, -2.493080, 318), "Miramon": (43.2868, -1.97121, 113),
    "Bidania": (43.146, -2.15502, 592), "Berastegi": (43.1248, -1.9817, 379),
    "Zegama": (42.9588, -2.29852, 520),
}
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


def cascada(filas, inicial, estado=None):
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
    if estado is not None and prev is not None:      # último estado real, sin redondear, para la previsión
        estado.update(fecha=prev, F=F, M=M, D=D, calentando=(prev - inicio).days < CALENTAMIENTO)
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


def descargar(cli, sensores, datos, dia_fin, primera, hora, max_dias, alm=None, limite=None):
    alm = alm or ew.Almacen(cli)
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
            if (limite is not None and time.monotonic() > limite) or cli.limite_agotado >= 2 or cli.errores_conexion >= 3:
                motivo = ("tiempo máximo alcanzado" if (limite is not None and time.monotonic() > limite)
                          else "la API sigue limitando las consultas" if cli.limite_agotado >= 2 else "Euskalmet no responde")
                print(f"\nSe detiene la descarga ({motivo}). Se continúa con lo ya descargado; el resto se reintentará en la próxima ejecución.", flush=True)
                return nuevas
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
def lluvia_con_observada(alm, cod, sens, horas):
    """Lluvia de las 24 h previas a las 12:00 de hoy mezclando lo ya medido con la previsión.

    Cada hora completa (6 lecturas de 10 minutos) usa lo medido por la estación; las horas aún sin
    medir usan la previsión. Devuelve (mm, horas_medidas)."""
    total, medidas = 0.0, 0
    for ts, prev_mm in horas:
        b = datetime.strptime(ts, "%Y-%m-%dT%H:%M") - timedelta(hours=1)
        d = alm.hora(cod, "lluvia", sens, b.date(), b.hour)
        trozos = [d.get((b.hour, m)) for m in range(0, 60, 10)]
        if all(x is not None for x in trozos):
            total += sum(float(x) for x in trozos)
            medidas += 1
        else:
            total += prev_mm
    return round(total, 2), medidas


def crear_previsor(alm, sensores, hora):
    """Devuelve una función que calcula la previsión a 0-3 días de cada estación (o None si no está disponible)."""
    if pv is None:
        return None

    def previsor(estados, ref, ahora):
        corr = pv.cargar_json("datos/correcciones_prevision.json")
        inc = pv.cargar_json("datos/incertidumbre_prevision.json")
        if not corr or not inc or pv.MODELO not in corr or pv.MODELO not in inc:
            raise RuntimeError("faltan datos/correcciones_prevision.json o datos/incertidumbre_prevision.json")
        corr, inc = corr[pv.MODELO], inc[pv.MODELO]
        hoy = ahora.date()
        resultado = {}
        for nombre, (lat, lon, alt) in COORD.items():
            est = estados.get(nombre)
            if not est or "fecha" not in est or est["fecha"] < hoy - timedelta(days=3):
                print(f"Previsión {nombre}: sin estado reciente, se omite.")
                continue
            tabla = pv.descargar_horario(lat, lon, alt)
            F, M, D = est["F"], est["M"], est["D"]
            filas, dia = [], est["fecha"] + timedelta(days=1)
            while dia <= hoy + timedelta(days=3):
                j = (dia - hoy).days
                e = pv.entradas_dia(tabla, dia, hora)
                if e is None:
                    break
                T, H, W, P, horas = e
                T, H, W = pv.corregir(corr[f"{nombre}|{min(3, max(0, j))}"], T, H, W)
                medidas = 0
                if j == 0 and alm.cli.errores_conexion == 0 and alm.cli.limite_agotado < 2:   # hoy, antes de las 12:00: la lluvia ya caída se toma de la estación
                    try:
                        P, medidas = lluvia_con_observada(alm, ew.ESTACIONES[nombre], sensores[ew.ESTACIONES[nombre]], horas)
                    except (RuntimeError, KeyError):
                        pass
                F = ffmc_step(T, H, W, P, F)
                M = dmc_step(T, H, P, M, DMC_L[dia.month - 1])
                D = dc_step(T, P, D, DC_L[dia.month - 1])
                isi, bui = isi_calc(F, W), bui_calc(M, D)
                fwi = fwi_calc(isi, bui)
                if j >= 0:
                    lo, hi = pv.rango(inc, j, fwi)
                    v = ref.get((nombre, dia.month))
                    filas.append({
                        "fecha": dia.isoformat(), "k": j, "T": round(T, 1), "H": round(H, 1), "W": round(W, 1), "R": round(P, 1),
                        "ffmc": round(F, 1), "dmc": round(M, 1), "dc": round(D, 1), "isi": round(isi, 1), "bui": round(bui, 1),
                        "fwi": round(fwi, 1), "min": round(lo, 1), "max": round(hi, 1), "clase": clase(fwi),
                        "pct": rango_percentil(v, fwi) if (v and len(v) >= 30 and not est["calentando"]) else None,
                        "calentando": est["calentando"], "lluvia_medida_h": medidas})
                dia += timedelta(days=1)
            resultado[nombre] = filas
            print(f"Previsión {nombre}: " + ", ".join(f"{f['fecha'][5:]} {f['fwi']} ({f['min']}-{f['max']})" for f in filas))
        info = {"modelo": pv.MODELO, "generada": ahora.isoformat(timespec="minutes"), "estado": "ok", "atribucion": pv.ATRIBUCION}
        return resultado, info

    return previsor


def escribir(datos, inicial, hora, ahora, dias_json, previsor=None):
    orden = list(ew.ESTACIONES)
    filas_csv = []
    cascadas, estados = {}, {}
    for nombre in ew.ESTACIONES:
        filas = [datos[nombre][k] for k in sorted(datos[nombre])]
        filas_csv += [[f["fecha"], nombre, f["temperatura"], f["humedad"], f["viento"], f["lluvia"]] for f in filas]
        estados[nombre] = {}
        cascadas[nombre] = cascada(filas, inicial, estados[nombre])
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

    # ---- previsión (opcional): si falla, se conserva la anterior marcada como obsoleta
    previsiones, info = None, None
    if previsor:
        try:
            previsiones, info = previsor(estados, ref, ahora)
        except Exception as e:     # un fallo de la previsión no debe impedir la actualización diaria
            print(f"Previsión no disponible: {type(e).__name__}: {str(e)[:200]}")
            info = {"estado": "error", "mensaje": str(e)[:200]}
    if previsiones is None and SALIDA.exists():
        try:
            viejo = json.loads(SALIDA.read_text(encoding="utf-8"))
            previsiones = {e["nombre"]: [x for x in e.get("prevision", []) if x["fecha"] >= ahora.date().isoformat()]
                           for e in viejo.get("estaciones", [])}
            vi = viejo.get("prevision_info") or {}
            info = dict(info or {}, estado="obsoleta", generada=vi.get("generada"), modelo=vi.get("modelo"), atribucion=vi.get("atribucion"))
        except (ValueError, KeyError, OSError):
            previsiones = None

    estaciones = []
    for nombre, cod in ew.ESTACIONES.items():
        recientes = cascadas[nombre][-dias_json:]
        for d in recientes:
            v = ref.get((nombre, int(d["fecha"][5:7])))
            d["pct"] = rango_percentil(v, d["fwi"]) if (v and len(v) >= 30 and not d["calentando"]) else None
        estaciones.append({"nombre": nombre, "codigo": cod, "n_total": len(cascadas[nombre]),
                           "dias": recientes, "prevision": (previsiones or {}).get(nombre, [])})

    SALIDA.parent.mkdir(parents=True, exist_ok=True)
    paquete = {
        "actualizado": ahora.isoformat(timespec="minutes"),
        "hora_dato": hora,
        "inicial": {"ffmc": inicial[0], "dmc": inicial[1], "dc": inicial[2]},
        "parametros": {"hueco_max": HUECO_MAX, "calentamiento": CALENTAMIENTO},
        "climatologia": clim,
        "prevision_info": info,
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
    p.add_argument("--sin-prevision", action="store_true", help="No calcula la previsión a 0-3 días")
    p.add_argument("--tiempo-max", type=float, default=12.0, help="Minutos máximos de descarga de Euskalmet antes de seguir con lo que haya")
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
        # el dato de las 12:00 (tramo 12:00-12:09) está disponible poco después de las 12:10
        limite = ahora.replace(hour=a.hora, minute=20, second=0, microsecond=0)
        dia_fin = ahora.date() if ahora >= limite else ahora.date() - timedelta(days=1)
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
    cli.max_esperas = 3          # en la ejecución diaria no se espera una eternidad: lo pendiente se reintenta después
    cli.max_fallos = 2
    limite = time.monotonic() + a.tiempo_max*60
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
    alm = ew.Almacen(cli)
    nuevas = descargar(cli, sensores, datos, dia_fin, primera, a.hora, a.max_dias, alm, limite)
    if sensores != sensores_inicio:
        SENSORES.write_text(json.dumps(sensores, ensure_ascii=False, indent=2), encoding="utf-8")
    previsor = None if a.sin_prevision else crear_previsor(alm, sensores, a.hora)
    escribir(datos, (a.ffmc, a.dmc, a.dc), a.hora, ahora, a.dias_json, previsor)
    print(f"\nListo: {nuevas} filas nuevas, {cli.llamadas} llamadas a la API. "
          f"Escrito {SALIDA} y {HISTORIAL}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
