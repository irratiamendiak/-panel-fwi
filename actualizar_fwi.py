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
try:
    import zarautz as zr
except ImportError:      # el punto híbrido Zarautz + Inurritza es opcional
    zr = None
try:
    import altzola as az
except ImportError:      # el punto híbrido Altzola + viento de Open-Meteo es opcional
    az = None

ZONA = ZoneInfo("Europe/Madrid")


def hora_solar(fecha):
    """Hora civil aproximada del mediodía solar en Gipuzkoa (~2° O): 13:00 en invierno, 14:00 en
    verano (el desfase real es de unos 8 minutos, despreciable a la resolución horaria de los datos)."""
    return 14 if datetime(fecha.year, fecha.month, fecha.day, 12, tzinfo=ZONA).dst() else 13
HISTORIAL = Path("datos/historial.csv")
SENSORES = Path("datos/sensores.json")
SALIDA = Path("docs/data/fwi.json")
CAMPOS = ["fecha", "estacion", "temperatura", "humedad", "viento", "lluvia", "direccion", "origen"]
# 'origen': qué dato se rellenó y de dónde (vacío = todo medido por la estación). Regla general, en
# euskalmet_fwi_datos.rellenar(): lluvia de la estación vecina más cercana y, si ninguna, Open-Meteo;
# temperatura, humedad y viento: el valor más desfavorable de la propia estación en ±50 min y, si no hay,
# Open-Meteo. Se aplica en cuanto pasa MARGEN_PUBLICACION tras la hora del dato (antes, lo que falta puede
# estar aún por publicarse y se reintenta).
# Factores de duración del día por mes (Van Wagner y Pickett, 1985; hemisferio norte)
DMC_L = [6.5, 7.5, 9.0, 12.8, 13.9, 13.9, 12.4, 10.9, 9.4, 8.0, 7.0, 6.0]
DC_L = [-1.6, -1.6, -1.6, 0.9, 3.8, 5.8, 6.4, 5.0, 2.4, 0.4, -1.6, -1.6]
# Tras la hora del dato (14:00 en verano, 13:00 en invierno) se espera hasta 60 minutos a que Euskalmet
# publique todo. A partir de ahí (15:00 / 14:00) el día se cierra SIEMPRE con el dato de la hora del dato:
# lo que falte se completa con la regla (ventana de ±50 min de la propia estación, estación vecina para
# la lluvia y, en último caso, Open-Meteo). Nunca se usan lecturas de horas posteriores.
MARGEN_PUBLICACION = timedelta(minutes=60)
REGISTRO_PREVISIONES = Path("datos/previsiones.csv")
HISTORICO_WEB = Path("docs/data/historico.json")   # historial completo para consultar cualquier día en la web
HUECO_MAX = 14       # días: con un hueco mayor entre dos lecturas se reinician los códigos
CALENTAMIENTO = 45   # días tras un reinicio (o el inicio del histórico) con valores aún poco fiables
# Coordenadas y altitudes oficiales de las fichas de Euskalmet (euskadi.eus), revisadas el 26/09/2026.
COORD = {   # nombre: (latitud, longitud, altitud)
    "Arrasate": (43.0695849, -2.493080, 318), "Miramon": (43.2868, -1.97121, 113),
    "Bidania": (43.146, -2.15502, 592), "Berastegi": (43.1248, -1.9817, 379),
    "Zegama": (42.9588, -2.29852, 520),
    "Mutriku": (43.3072, -2.3850, 20),   # sin estación física: estimación de Open-Meteo (ver MODELO_ESTACIONES)
    "Zarautz": (43.293, -2.14542, 80),   # C064 (temperatura/humedad/viento) + C086 Inurritza (lluvia)
    "Ordizia": (43.0484, -2.17755, 243),
    "Zizurkil": (43.1901, -2.06181, 149),
    "Pasaia": (43.3370283, -1.92752, 0),     # plataforma en la bocana de la bahía, no en tierra
    "Altzola": (43.2419, -2.39784, 17),   # C078 (temperatura/humedad/lluvia) + viento de Open-Meteo
}
# Puntos sin sensor real detrás: el dato "de hoy" también es una estimación de modelo (Open-Meteo),
# sin nada local con que corregirla ni contrastarla, a diferencia de las estaciones de Euskalmet.
MODELO_ESTACIONES = {"Mutriku"}
# Estaciones reales pero que combinan dos sensores distintos de Euskalmet (ver el módulo zarautz.py).
NOTAS_ESTACIONES = {"Zarautz": "Puntu honetako euria Inurritzako estaziotik (C086) hartzen da, udalerri "
                                "berean: Zarautzek (C064) ez du euri-neurgailurik.",
                     "Gipuzkoa": "Ez da estazio bat: puntu guztien batez besteko haztatua da, bakoitzaren "
                                 "eragin-eremuaren azaleraren arabera (mapako Thiessen poligonoak). "
                                 "Haizea ez da batez besteratzen (norabide bat batez besteratzeak ez du zentzurik)."}
if az is not None:
    NOTAS_ESTACIONES[az.NOMBRE] = az.NOTA
# "Gipuzkoa" tiene nota (arriba), pero no es una estación real: no cuenta para el histórico ni
# para la lista de estaciones que se recorre en cada ejecución (eso lo decide media_ponderada()).
NOMBRES_EXTRA = set(NOTAS_ESTACIONES) - {"Gipuzkoa"}
# nombre -> código Euskalmet de los puntos híbridos (para el JSON y para mezclar la lluvia ya medida)
CODIGO_HIBRIDO = {}
if zr is not None:
    CODIGO_HIBRIDO[zr.NOMBRE] = zr.ESTACION
if az is not None:
    CODIGO_HIBRIDO[az.NOMBRE] = az.ESTACION
IDENTIDAD = {"T": ("id", 0), "H": ("id", 0), "W": ("id", 0)}   # sin corrección: no hay estación con la que calibrarla
DIAS_JSON = 400      # días recientes que se publican en la web
# Multiplicador de la tasa de incendios con viento del cuadrante sur (SE-S-SO, 120-240°) de al menos
# VIENTO_SUR_MIN km/h a la hora del dato. Calibrado con el registro EGIF de Gipuzkoa 2010-2025 (497
# incendios, 63.831 días-estación, histórico al mediodía solar): a igualdad de FWI, con viento sur hubo
# 2,7 veces más incendios (intervalo 95 %: unos 2,3-3,3). El umbral de 3 km/h fue el que mejor ajustaba;
# los valores por rango de FWI (x2,8 con FWI < 11,2; x2,4 entre 11,2 y 38) no difieren significativamente,
# así que se usa un único multiplicador. Es una estimación orientativa, no un factor exacto.
MULT_VIENTO_SUR = 1.6   # una vez descontado el efecto de la época (ver IFG); sin descontarlo salía x2,7
VIENTO_SUR_MIN = 3.0   # km/h

# ---- Índice adaptado a Gipuzkoa (IFG): cuántas veces más probable es un incendio ese día que en un
# día medio, según el FWI, la época del año y el viento sur. Regresión de Poisson calibrada con el EGIF
# 2010-2025 (497 incendios, 63.837 días-estación, histórico al mediodía solar) y validada con 2020-2025:
#   tasa = exp(B0 + B1*ln(1+FWI) + B_EPOCA*[diciembre-abril] + B_SUR*[viento SE-S-SO >= 3 km/h])
#   IFG  = tasa / TASA_MEDIA
# Con el mismo FWI, de diciembre a abril hay 3,2 veces más incendios (x2,6-3,9) y con viento sur 1,6 veces
# más (x1,3-2,0). Los niveles son múltiplos de un día medio: <0,5 / 0,5-1 / 1-2 / 2-4 / 4-8 / >=8.
IFG_B0, IFG_B1, IFG_B_EPOCA, IFG_B_SUR = -7.335, 1.122, 1.156, 0.479
IFG_TASA_MEDIA = 497 / 63837
IFG_MESES_ALTOS = (12, 1, 2, 3, 4)
IFG_CLASES = [("Muy bajo", 0.5), ("Bajo", 1.0), ("Moderado", 2.0), ("Alto", 4.0), ("Muy alto", 8.0), ("Extremo", math.inf)]


def hay_viento_sur(sur, W):
    return sur is not None and sur >= 0.5 and (W is None or W >= VIENTO_SUR_MIN)


def ifg_calc(fwi, fecha, sur, W):
    """Índice adaptado a Gipuzkoa: múltiplo de la tasa de incendios de un día medio."""
    mes = int(str(fecha)[5:7])
    x = IFG_B0 + IFG_B1 * math.log1p(max(fwi, 0.0)) + (IFG_B_EPOCA if mes in IFG_MESES_ALTOS else 0.0) \
        + (IFG_B_SUR if hay_viento_sur(sur, W) else 0.0)
    return math.exp(x) / IFG_TASA_MEDIA


def clase_ifg(v):
    for nombre, tope in IFG_CLASES:
        if v < tope:
            return nombre
    return IFG_CLASES[-1][0]
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


def componente_sur(direccion_grados):
    if direccion_grados is None:
        return None
    return max(0.0, -math.cos(math.radians(direccion_grados)))


def mult_viento(fwi, sur, W=None):
    """Multiplicador orientativo de riesgo de ignición si el viento sopla del cuadrante sur
    (componente sur >= 0,5, es decir, entre 120 y 240°) con al menos VIENTO_SUR_MIN km/h."""
    if sur is None:
        return None
    if sur < 0.5 or (W is not None and W < VIENTO_SUR_MIN):
        return 1.0
    return MULT_VIENTO_SUR


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
        sur = componente_sur(f.get("direccion"))
        salida.append({
            "fecha": f["fecha"], "T": T, "H": H, "W": W, "R": R, "dir": f.get("direccion"), "sur": sur,
            "ffmc": round(F, 1), "dmc": round(M, 1), "dc": round(D, 1),
            "isi": round(isi, 1), "bui": round(bui, 1), "fwi": round(fwi, 1),
            "clase": clase(fwi), "hueco": hueco, "mult_viento": mult_viento(fwi, sur, W),
            "ifg": round(ifg_calc(fwi, f["fecha"], sur, W), 2),
            "clase_ifg": clase_ifg(ifg_calc(fwi, f["fecha"], sur, W)),
            "calentando": (d - inicio).days < CALENTAMIENTO,
            "origen": f.get("origen") or "",
        })
    if estado is not None and prev is not None:      # último estado real, sin redondear, para la previsión
        estado.update(fecha=prev, F=F, M=M, D=D, calentando=(prev - inicio).days < CALENTAMIENTO)
    return salida


ZONAS = Path("docs/data/zonas.geojson")


def cargar_pesos():
    """{nombre: área en km2 de su zona de influencia}, o {} si aún no se ha generado el mapa."""
    if not ZONAS.exists():
        return {}
    try:
        datos = json.loads(ZONAS.read_text(encoding="utf-8"))
        return {f["properties"]["estacion"]: f["properties"]["area_km2"] for f in datos["features"]}
    except (ValueError, KeyError, OSError):
        return {}


def media_ponderada(cascadas, pesos, cobertura_minima=0.30):
    """Serie diaria 'Gipuzkoa': cada variable, ponderada por el área de la zona de cada estación
    (docs/data/zonas.geojson), usando solo los días y estaciones con código fiable (sin calentar).
    No incluye viento (no tiene sentido promediar una dirección entre estaciones): las tarjetas y
    la tabla simplemente no lo muestran para este punto. Si un día no llega a la cobertura mínima
    del territorio (por ejemplo, en 2010-2023, antes de que existieran varias de estas estaciones),
    ese día no se calcula."""
    total = sum(pesos.get(n, 0) for n in cascadas if n in pesos)
    if not total:
        return []
    por_fecha = {}
    for nombre, filas in cascadas.items():
        peso = pesos.get(nombre)
        if not peso:
            continue
        for d in filas:
            if d["calentando"]:
                continue
            por_fecha.setdefault(d["fecha"], {})[nombre] = d
    VARS = ("ffmc", "dmc", "dc", "isi", "bui", "fwi", "ifg")   # solo índices: no se promedian T, HR ni lluvia
    salida = []
    for fecha in sorted(por_fecha):
        dias = por_fecha[fecha]
        peso_dia = sum(pesos[n] for n in dias)
        if peso_dia < total * cobertura_minima:
            continue
        prom = {v: sum(pesos[n] * dias[n][v] for n in dias) / peso_dia for v in VARS}
        salida.append({
            "fecha": fecha, "T": None, "H": None, "R": None,
            "W": None, "dir": None, "mult_viento": None,
            "ffmc": round(prom["ffmc"], 1), "dmc": round(prom["dmc"], 1), "dc": round(prom["dc"], 1),
            "isi": round(prom["isi"], 1), "bui": round(prom["bui"], 1), "fwi": round(prom["fwi"], 1),
            "clase": clase(prom["fwi"]), "hueco": False, "calentando": False,
            "ifg": round(prom["ifg"], 2), "clase_ifg": clase_ifg(prom["ifg"]),
            "cobertura": round(100 * peso_dia / total),
        })
    return salida


def prevision_media(previsiones, pesos, ref, cobertura_minima=0.30):
    """Previsión de GIPUZKOA: para cada día, media ponderada por área de la previsión de cada estación
    (FWI, sus componentes y el tramo min-max), sin las estaciones que aún están calentando. Igual que
    en media_ponderada(), solo índices: ni temperatura, ni humedad, ni lluvia, ni viento.
    El tramo min-max es la media de los tramos de cada estación: orientativo."""
    total = sum(p for n, p in pesos.items() if n != "Gipuzkoa")
    if not total:
        return []
    por_fecha = {}
    for nombre, filas in previsiones.items():
        peso = pesos.get(nombre)
        if not peso or nombre == "Gipuzkoa":
            continue
        for f in filas:
            if not f.get("calentando"):
                if f.get("ifg") is None and f.get("fwi") is not None:   # previsiones antiguas sin Basugix
                    f["ifg"] = round(ifg_calc(f["fwi"], f["fecha"], componente_sur(f.get("dir")), f.get("W")), 2)
                por_fecha.setdefault(f["fecha"], {})[nombre] = f
    VARS = ("ffmc", "dmc", "dc", "isi", "bui", "fwi", "min", "max", "ifg")
    salida = []
    for fecha in sorted(por_fecha):
        dias = por_fecha[fecha]
        peso_dia = sum(pesos[n] for n in dias)
        if peso_dia < total * cobertura_minima:
            continue
        prom = {v: sum(pesos[n] * dias[n][v] for n in dias) / peso_dia for v in VARS}
        fwi = prom["fwi"]
        v = ref.get(("Gipuzkoa", int(fecha[5:7])))
        salida.append({
            "fecha": fecha, "k": min(d["k"] for d in dias.values()),
            "T": None, "H": None, "W": None, "R": None, "dir": None, "sur": None, "mult_viento": None,
            **{k: round(prom[k], 1) for k in VARS},
            "clase": clase(fwi), "pct": rango_percentil(v, fwi) if (v and len(v) >= 30) else None,
            "clase_ifg": clase_ifg(prom["ifg"]),
            "calentando": False, "lluvia_medida_h": None, "cobertura": round(100 * peso_dia / total),
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
    datos = {n: {} for n in list(ew.ESTACIONES) + list(NOMBRES_EXTRA) + list(MODELO_ESTACIONES)}
    # (NOTAS_ESTACIONES ya incluye Zarautz y, si está disponible, Altzola)
    if HISTORIAL.exists():
        with open(HISTORIAL, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                n = r.get("estacion")
                if n not in datos:
                    continue
                try:
                    date.fromisoformat(r["fecha"])
                    dir_txt = (r.get("direccion") or "").strip()
                    datos[n][r["fecha"]] = {
                        "fecha": r["fecha"],
                        "temperatura": float(r["temperatura"]),
                        "humedad": float(r["humedad"]),
                        "viento": float(r["viento"]),
                        "lluvia": float(r["lluvia"]),
                        "direccion": float(dir_txt) if dir_txt else None,
                        "origen": ew.origen_de_fila(r),
                    }
                except (ValueError, KeyError):
                    continue
    return datos


def dato_publicado(dia, hora):
    """True si ya pasó MARGEN_PUBLICACION desde la hora del dato de ese día: lo que falte ya no va a
    llegar, así que se puede aplicar la regla de relleno (estación vecina / Open-Meteo) sin esperar."""
    return datetime.now(ZONA) >= datetime.combine(dia, datetime.min.time()).replace(hour=hora, tzinfo=ZONA) + MARGEN_PUBLICACION


def obtener_dia(cli, alm, sensores, cod, dia, dia_fin, hora):
    """Devuelve (fila, mensaje). fila es None si no se puede guardar todavía."""
    # Lluvia de 24 h: en los últimos días se exige completa (144 lecturas) mientras Euskalmet puede
    # estar publicando todavía, es decir, durante MARGEN_PUBLICACION tras la hora del dato. Pasado ese
    # margen, las lecturas que faltan ya no van a llegar: se acepta con hasta 6 perdidas (una hora), como
    # en los históricos. En días más antiguos se admite hasta un 10 % de huecos (MIN_LLUVIA).
    publicado = dato_publicado(dia, hora)
    if dia >= dia_fin - timedelta(days=2):
        minimo = ew.LECTURAS_POR_DIA - 6 if publicado else ew.LECTURAS_POR_DIA
    else:
        minimo = ew.MIN_LLUVIA
    # Pasado el margen de publicación se aplica ya la regla de relleno (antes solo con 3 días de antigüedad)
    datos, n, motivo = ew.obtener_dia(cli, alm, sensores, cod, dia, hora, minimo,
                                      rellenar_huecos=publicado)
    if datos is None:
        return None, motivo + ("; se reintentará" if dia >= dia_fin - timedelta(days=6) else "")
    t, h, w, ll, dv, origen = datos
    if origen:
        aviso = f" (rellenado: {origen})"
    else:
        aviso = "" if n >= ew.LECTURAS_POR_DIA else f" (lluvia: {n}/{ew.LECTURAS_POR_DIA} lecturas)"
    fila = {"fecha": dia.isoformat(), "temperatura": round(t, 1), "humedad": round(h, 1),
            "viento": round(w * 3.6, 1), "lluvia": ll, "direccion": round(dv, 0) if dv is not None else None,
            "origen": origen}
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
        b = datetime.strptime(ts, "%Y-%m-%dT%H:%M") - timedelta(hours=1)   # hora oficial (Open-Meteo)
        du, hu, _ = ew.a_utc(b.date(), b.hour)                              # Euskalmet va en UTC
        d = alm.hora(cod, "lluvia", sens, du, hu)
        trozos = [d.get((hu, m)) for m in range(0, 60, 10)]
        if all(x is not None for x in trozos):
            total += sum(float(x) for x in trozos)
            medidas += 1
        else:
            total += prev_mm
    return round(total, 2), medidas


def descargar_mutriku(datos, dia_fin, hora, dias_atras=3):
    """Rellena los últimos días de Mutriku con la estimación de Open-Meteo (sin estación física).

    Solo cubre un margen corto (por defecto 3 días): para el histórico largo se usa
    mutriku_historico.py, ejecutado aparte, igual que con el resto de estaciones y Euskalmet.
    """
    if pv is None:
        return 0
    nombre = "Mutriku"
    existentes = datos.setdefault(nombre, {})
    faltan = [dia_fin - timedelta(days=k) for k in range(dias_atras)
              if (dia_fin - timedelta(days=k)).isoformat() not in existentes]
    if not faltan:
        return 0
    try:
        tabla = pv.descargar_horario(*COORD[nombre])
    except RuntimeError as e:
        print(f"{nombre}: no se ha podido descargar de Open-Meteo ({type(e).__name__}: {str(e)[:150]})")
        return 0
    nuevas = 0
    for dia in sorted(faltan):
        e = pv.entradas_dia(tabla, dia, hora)
        if not e:
            continue
        T, H, W, DV, P, _ = e
        existentes[dia.isoformat()] = {"fecha": dia.isoformat(), "temperatura": round(T, 1), "humedad": round(H, 1),
                                       "viento": round(W, 1), "lluvia": round(P, 1),
                                       "direccion": round(DV) if DV is not None else None}
        nuevas += 1
        print(f"{dia} {nombre} (Open-Meteo, sin estación): T={T:.1f} HR={H:.0f} viento={W:.1f} km/h lluvia={P:.1f} mm")
    return nuevas


def descargar_zarautz(cli, alm, sensores, datos, dia_fin, hora, max_dias):
    """Rellena los días que falten de Zarautz (C064 + C086 Inurritza, ver zarautz.py)."""
    if zr is None:
        return 0
    nombre = zr.NOMBRE
    existentes = datos.setdefault(nombre, {})
    sens = sensores.setdefault(nombre, {})
    if not zr.asegurar_sensores(cli, sens, dia_fin, alm, hora):
        print(f"{nombre}: sensores incompletos (Zarautz C064 / Inurritza C086), se omite esta ejecución.")
        return 0
    desde = date.fromisoformat(max(existentes)) + timedelta(days=1) if existentes else dia_fin - timedelta(days=max_dias)
    desde = min(desde, dia_fin - timedelta(days=6))        # reintenta huecos de la última semana
    desde = max(desde, dia_fin - timedelta(days=max_dias))
    nuevas = 0
    dia = desde
    while dia <= dia_fin:
        if dia.isoformat() not in existentes:
            fila, motivo = zr.leer_dia(cli, alm, sens, dia, hora,
                                       rellenar_huecos=dato_publicado(dia, hora))
            if fila:
                existentes[fila["fecha"]] = fila
                nuevas += 1
                print(f"{dia} {nombre}: T={fila['temperatura']} HR={fila['humedad']} "
                      f"viento={fila['viento']} km/h lluvia={fila['lluvia']} mm dir={fila['direccion']}"
                      + (f" ({motivo})" if motivo else ""))
            else:
                print(f"{dia} {nombre}: {motivo}")
        dia += timedelta(days=1)
    return nuevas


def descargar_altzola(cli, alm, sensores, datos, dia_fin, hora, max_dias):
    """Rellena los días que falten de Altzola: C078 (temperatura/humedad/lluvia) + viento de
    Open-Meteo en sus coordenadas (ver altzola.py)."""
    if az is None or pv is None:
        return 0
    nombre = az.NOMBRE
    existentes = datos.setdefault(nombre, {})
    sens = sensores.setdefault(nombre, {})
    if not az.asegurar_sensores(cli, sens, dia_fin):
        print(f"{nombre}: sensores incompletos (Altzola C078), se omite esta ejecución.")
        return 0
    desde = date.fromisoformat(max(existentes)) + timedelta(days=1) if existentes else dia_fin - timedelta(days=max_dias)
    desde = min(desde, dia_fin - timedelta(days=6))        # reintenta huecos de la última semana
    desde = max(desde, dia_fin - timedelta(days=max_dias))
    faltan = [desde + timedelta(days=k) for k in range((dia_fin - desde).days + 1)
              if (desde + timedelta(days=k)).isoformat() not in existentes]
    if not faltan:
        return 0
    try:
        tabla = pv.descargar_horario(*az.COORD)
    except RuntimeError as e:
        print(f"{nombre}: no se ha podido descargar el viento de Open-Meteo ({type(e).__name__}: {str(e)[:150]})")
        return 0
    nuevas = 0
    for dia in faltan:
        e = pv.entradas_dia(tabla, dia, hora)
        viento = (e[2], e[3]) if e else None
        fila, motivo = az.leer_dia(cli, alm, sens, dia, hora, viento,
                                   rellenar_huecos=dato_publicado(dia, hora))
        if fila:
            existentes[fila["fecha"]] = fila
            nuevas += 1
            print(f"{dia} {nombre}: T={fila['temperatura']} HR={fila['humedad']} "
                  f"viento={fila['viento']} km/h (Open-Meteo) lluvia={fila['lluvia']} mm dir={fila['direccion']}"
                  + (f" ({motivo})" if motivo else ""))
        else:
            print(f"{dia} {nombre}: {motivo}")
    return nuevas


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
                T, H, W, DV, P, horas = e
                T, H, W = pv.corregir(corr.get(f"{nombre}|{min(3, max(0, j))}", IDENTIDAD), T, H, W)
                medidas = 0
                cod_est = ew.ESTACIONES.get(nombre)
                if cod_est:
                    cod_lluvia, sens_lluvia = cod_est, sensores.get(cod_est)
                elif zr and nombre == zr.NOMBRE:      # Zarautz: la lluvia es de Inurritza (otra estación)
                    cod_lluvia, sens_lluvia = zr.ESTACION_LLUVIA, sensores.get(nombre)
                elif nombre in CODIGO_HIBRIDO:         # Altzola: la lluvia es de la misma estación que T/HR
                    cod_lluvia, sens_lluvia = CODIGO_HIBRIDO[nombre], sensores.get(nombre)
                else:                                  # Mutriku u otro punto sin estación real: no hay nada que mezclar
                    cod_lluvia, sens_lluvia = None, None
                if j == 0 and cod_lluvia and sens_lluvia and alm.cli.errores_conexion == 0 and alm.cli.limite_agotado < 2:   # hoy, antes de las 12:00: la lluvia ya caída se toma de la estación
                    try:
                        P, medidas = lluvia_con_observada(alm, cod_lluvia, sens_lluvia, horas)
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
                    sur = componente_sur(DV)
                    filas.append({
                        "fecha": dia.isoformat(), "k": j, "T": round(T, 1), "H": round(H, 1), "W": round(W, 1), "R": round(P, 1),
                        "dir": round(DV, 0) if DV is not None else None, "sur": sur,
                        "ffmc": round(F, 1), "dmc": round(M, 1), "dc": round(D, 1), "isi": round(isi, 1), "bui": round(bui, 1),
                        "fwi": round(fwi, 1), "min": round(lo, 1), "max": round(hi, 1), "clase": clase(fwi),
                        "mult_viento": mult_viento(fwi, sur, W),
                        "ifg": round(ifg_calc(fwi, dia.isoformat(), sur, W), 2),
                        "clase_ifg": clase_ifg(ifg_calc(fwi, dia.isoformat(), sur, W)),
                        "pct": rango_percentil(v, fwi) if (v and len(v) >= 30 and not est["calentando"]) else None,
                        "calentando": est["calentando"], "lluvia_medida_h": medidas})
                dia += timedelta(days=1)
            resultado[nombre] = filas
            print(f"Previsión {nombre}: " + ", ".join(f"{f['fecha'][5:]} {f['fwi']} ({f['min']}-{f['max']})" for f in filas))
        info = {"modelo": pv.MODELO, "generada": ahora.isoformat(timespec="minutes"), "estado": "ok", "atribucion": pv.ATRIBUCION}
        return resultado, info

    return previsor


def registrar_previsiones(previsiones, ahora):
    """Guarda en datos/previsiones.csv la previsión emitida hoy, para poder compararla después con el
    FWI medido (fiabilidad a 1, 2 y 3 días). Una fila por (día de emisión, estación, día previsto):
    si el proceso corre varias veces el mismo día, se queda la última previsión de ese día."""
    campos = ["emitida", "estacion", "fecha", "adelanto", "fwi", "min", "max", "clase", "calculado", "ifg", "clase_ifg"]
    hoy = ahora.date()
    filas = {}
    if REGISTRO_PREVISIONES.exists():
        with open(REGISTRO_PREVISIONES, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                filas[(r["emitida"], r["estacion"], r["fecha"])] = r
    for nombre, lista in previsiones.items():
        for pr in lista:
            try:
                adelanto = (date.fromisoformat(pr["fecha"]) - hoy).days
            except (KeyError, ValueError):
                continue
            if adelanto < 0:
                continue
            filas[(hoy.isoformat(), nombre, pr["fecha"])] = {
                "emitida": hoy.isoformat(), "estacion": nombre, "fecha": pr["fecha"], "adelanto": adelanto,
                "fwi": pr.get("fwi"), "min": pr.get("min"), "max": pr.get("max"), "clase": pr.get("clase"),
                "calculado": 1 if pr.get("k") == 0 else 0, "ifg": pr.get("ifg"), "clase_ifg": pr.get("clase_ifg")}
    REGISTRO_PREVISIONES.parent.mkdir(parents=True, exist_ok=True)
    with open(REGISTRO_PREVISIONES, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campos, lineterminator="\n")
        w.writeheader()
        for k in sorted(filas):
            w.writerow({c: filas[k].get(c, "") for c in campos})


def escribir_historico(cascadas, ref, orden_json):
    """docs/data/historico.json: todos los días de cada estación (y GIPUZKOA) desde el principio, en
    columnas compactas, para que la web pueda enseñar la situación de cualquier fecha (mapa, tarjetas
    e informe). La web solo lo descarga si se elige una fecha anterior a las que trae fwi.json."""
    salida = {"version": 1, "estaciones": {}}
    for nombre in orden_json:
        dias = cascadas.get(nombre) or []
        if not dias:
            continue
        ini = date.fromisoformat(dias[0]["fecha"])
        n = (date.fromisoformat(dias[-1]["fecha"]) - ini).days + 1
        cols = {k: [None] * n for k in ("fwi", "pct", "T", "H", "W", "R", "dir", "ffmc", "dmc", "dc", "isi", "bui",
                                         "cal", "cob", "ifg", "org")}
        for d in dias:
            i = (date.fromisoformat(d["fecha"]) - ini).days
            v = ref.get((nombre, int(d["fecha"][5:7])))
            cols["fwi"][i] = d["fwi"]
            cols["pct"][i] = rango_percentil(v, d["fwi"]) if (v and len(v) >= 30 and not d.get("calentando")) else None
            cols["ifg"][i] = d.get("ifg")
            cols["org"][i] = d.get("origen") or None   # datos rellenados y de dónde (vacío = todo medido)
            for k in ("T", "H", "W", "R", "ffmc", "dmc", "dc", "isi", "bui"):
                x = d.get(k)
                cols[k][i] = round(x, 1) if isinstance(x, (int, float)) else None
            cols["dir"][i] = round(d["dir"]) if isinstance(d.get("dir"), (int, float)) else None
            cols["cal"][i] = 1 if d.get("calentando") else 0
            cols["cob"][i] = d.get("cobertura")
        # columnas que no tienen ningún dato (p. ej. viento en GIPUZKOA) no se publican
        salida["estaciones"][nombre] = {"inicio": ini.isoformat(),
                                        **{k: v for k, v in cols.items() if any(x is not None for x in v)}}
    HISTORICO_WEB.parent.mkdir(parents=True, exist_ok=True)
    HISTORICO_WEB.write_text(json.dumps(salida, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def bgx_de_ifg(v):
    """Basugix a partir del multiplicador IFG: 20 = día medio; +10 puntos = probabilidad doble."""
    return max(0.0, 20 + 10 * math.log2(max(v, 1e-6)))


def clase_bgx(v):
    for nombre, tope in (("Muy bajo", 10), ("Bajo", 20), ("Moderado", 30), ("Alto", 40), ("Muy alto", 50)):
        if round(v, 1) < tope:
            return nombre
    return "Extremo"


def verificar_previsiones(cascadas, hoy, dias=365):
    """Compara las previsiones guardadas en datos/previsiones.csv con lo medido después.

    Para 1, 2 y 3 días de antelación, por separado para el conjunto de estaciones y para GIPUZKOA:
    número de casos, error medio absoluto, tendencia (media de previsto - medido), % de veces que el
    valor medido cayó dentro del tramo mín-máx y % de nivel acertado. Para el FWI y para Basugix."""
    if not REGISTRO_PREVISIONES.exists():
        return None
    medido = {(n, d["fecha"]): d for n, dd in cascadas.items() for d in dd if not d.get("calentando")}
    desde = (hoy - timedelta(days=dias)).isoformat()
    acum = {}
    emisiones = set()
    with open(REGISTRO_PREVISIONES, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                ad = int(r["adelanto"])
                pf = float(r["fwi"])
            except (KeyError, ValueError):
                continue
            if ad not in (1, 2, 3) or r["emitida"] < desde:
                continue
            o = medido.get((r["estacion"], r["fecha"]))
            if not o:
                continue
            emisiones.add(r["emitida"])
            grupo = "Gipuzkoa" if r["estacion"] == "Gipuzkoa" else "estaciones"
            a = acum.setdefault((grupo, ad), {"n": 0, "fwi": [0, 0, 0, 0, 0], "bgx": [0, 0, 0, 0, 0]})
            a["n"] += 1
            # FWI: |error|, error, dentro del tramo, casos con tramo, nivel acertado
            e = pf - o["fwi"]
            a["fwi"][0] += abs(e); a["fwi"][1] += e
            try:
                mn, mx = float(r["min"]), float(r["max"])
                a["fwi"][3] += 1
                a["fwi"][2] += 1 if mn <= o["fwi"] <= mx else 0
            except (KeyError, ValueError):
                mn = mx = None
            a["fwi"][4] += 1 if clase(pf) == o.get("clase", clase(o["fwi"])) else 0
            # Basugix (si la previsión guardó su IFG)
            try:
                pi = float(r["ifg"])
            except (KeyError, ValueError, TypeError):
                pi = None
            if pi and o.get("ifg"):
                pb, ob = bgx_de_ifg(pi), bgx_de_ifg(o["ifg"])
                eb = pb - ob
                a.setdefault("nb", 0); a["nb"] += 1
                a["bgx"][0] += abs(eb); a["bgx"][1] += eb
                if mn is not None:
                    # el tramo de FWI se traslada a Basugix con la misma época y viento que la previsión
                    bmn = pb + 10 * IFG_B1 * math.log2((1 + mn) / (1 + pf))
                    bmx = pb + 10 * IFG_B1 * math.log2((1 + mx) / (1 + pf))
                    a["bgx"][3] += 1
                    a["bgx"][2] += 1 if bmn - 0.05 <= ob <= bmx + 0.05 else 0
                a["bgx"][4] += 1 if clase_bgx(pb) == clase_bgx(ob) else 0
    if not acum:
        return {"emisiones": 0, "grupos": {}}
    grupos = {}
    for (g, ad), a in sorted(acum.items()):
        res = {"n": a["n"]}
        for k, n in (("fwi", a["n"]), ("bgx", a.get("nb", 0))):
            v = a[k]
            if n:
                res[k] = {"mae": round(v[0] / n, 1), "sesgo": round(v[1] / n, 1),
                          "en_tramo": round(100 * v[2] / v[3]) if v[3] else None,
                          "nivel": round(100 * v[4] / n), "n": n}
        grupos.setdefault(g, {})[str(ad)] = res
    return {"emisiones": len(emisiones), "desde": min(emisiones), "hasta": max(emisiones), "grupos": grupos}


def escribir(datos, inicial, hora, ahora, dias_json, previsor=None):
    orden = list(ew.ESTACIONES) + list(NOMBRES_EXTRA) + list(MODELO_ESTACIONES)
    filas_csv = []
    cascadas, estados = {}, {}
    for nombre in orden:
        filas = [datos[nombre][k] for k in sorted(datos[nombre])]
        filas_csv += [[f["fecha"], nombre, f["temperatura"], f["humedad"], f["viento"], f["lluvia"],
                       f.get("direccion") if f.get("direccion") is not None else "",
                       f.get("origen", "")] for f in filas]
        estados[nombre] = {}
        cascadas[nombre] = cascada(filas, inicial, estados[nombre])
    filas_csv.sort(key=lambda r: (r[0], orden.index(r[1])))

    HISTORIAL.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORIAL, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(CAMPOS)
        w.writerows(filas_csv)

    # Gipuzkoa: media ponderada por área de todas las estaciones (después de escribir el CSV:
    # no es un dato medido, no debe mezclarse con las filas reales del histórico).
    pesos = cargar_pesos()
    orden_json = sorted(orden)   # estaciones en orden alfabético en la web
    if pesos:
        cascadas["Gipuzkoa"] = media_ponderada(cascadas, pesos)
        orden_json = ["Gipuzkoa"] + orden_json   # Gipuzkoa siempre la primera, delante del alfabeto

    ref = referencia(cascadas)
    try:
        escribir_historico(cascadas, ref, orden_json)
    except (OSError, ValueError, KeyError) as e:   # nunca debe impedir publicar el panel
        print("No se ha podido escribir el histórico de la web:", e)
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
    if previsiones is not None and pesos:
        previsiones["Gipuzkoa"] = prevision_media(previsiones, pesos, ref)
    if previsiones is not None and info and info.get("estado") == "ok":
        try:
            registrar_previsiones(previsiones, ahora)
        except (OSError, ValueError) as e:   # el registro nunca debe impedir publicar el panel
            print("No se ha podido guardar el registro de previsiones:", e)
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
    for nombre in orden_json:
        recientes = cascadas[nombre][-dias_json:]
        for d in recientes:
            v = ref.get((nombre, int(d["fecha"][5:7])))
            d["pct"] = rango_percentil(v, d["fwi"]) if (v and len(v) >= 30 and not d["calentando"]) else None
        codigo = ew.ESTACIONES.get(nombre) or CODIGO_HIBRIDO.get(nombre)
        estaciones.append({"nombre": nombre, "codigo": codigo, "n_total": len(cascadas[nombre]),
                           "modelo": nombre in MODELO_ESTACIONES, "nota": NOTAS_ESTACIONES.get(nombre),
                           "resumen": nombre == "Gipuzkoa",
                           "dias": recientes, "prevision": (previsiones or {}).get(nombre, [])})

    SALIDA.parent.mkdir(parents=True, exist_ok=True)
    try:
        verificacion = verificar_previsiones(cascadas, ahora.date())
    except (OSError, ValueError, KeyError) as e:   # nunca debe impedir publicar el panel
        print("No se ha podido verificar la previsión:", e)
        verificacion = None
    paquete = {
        "actualizado": ahora.isoformat(timespec="minutes"),
        "hora_dato": hora,
        "inicial": {"ffmc": inicial[0], "dmc": inicial[1], "dc": inicial[2]},
        "parametros": {"hueco_max": HUECO_MAX, "calentamiento": CALENTAMIENTO},
        "climatologia": clim,
        "prevision_info": info,
        "verificacion": verificacion,
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
    p.add_argument("--hora", type=int, default=None,
                   help="Hora del dato (por defecto, la del mediodía solar: 13 en invierno, 14 en verano)")
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
    hora = a.hora if a.hora is not None else hora_solar(ahora.date())
    if a.hasta:
        dia_fin = date.fromisoformat(a.hasta)
    else:
        # el dato del mediodía solar (tramo HH:00-HH:09) está disponible poco después de HH:10
        limite = ahora.replace(hour=hora, minute=10, second=0, microsecond=0)   # desde HH:10 se intenta ya el dato de hoy
        dia_fin = ahora.date() if ahora >= limite else ahora.date() - timedelta(days=1)
    primera = date.fromisoformat(a.desde) if a.desde else dia_fin
    print(f"Hora del dato: {hora:02d}:00 ({'verano' if hora == 14 else 'invierno'}, mediodía solar aproximado).")

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
    # La dirección del viento no aparece en el catálogo de resúmenes diarios, así que si no se descarga
    # ningún día nuevo (caso habitual día a día) no habría ocasión de intentarlo. Se prueba aquí una vez
    # por ejecución para cada estación a la que todavía le falte, sin depender de si hay días pendientes.
    for nombre, cod in ew.ESTACIONES.items():
        if ew.intentar_direccion(cli, alm, sensores, cod, dia_fin - timedelta(days=1), hora):
            if "direccion" not in sensores_inicio.get(cod, {}):
                print(f"{nombre}: sensor de dirección del viento encontrado ({sensores[cod]['direccion']['sensor']}).")
    nuevas = descargar(cli, sensores, datos, dia_fin, primera, hora, a.max_dias, alm, limite)
    nuevas += descargar_mutriku(datos, dia_fin, hora)
    nuevas += descargar_zarautz(cli, alm, sensores, datos, dia_fin, hora, a.max_dias)
    nuevas += descargar_altzola(cli, alm, sensores, datos, dia_fin, hora, a.max_dias)
    if sensores != sensores_inicio:
        SENSORES.write_text(json.dumps(sensores, ensure_ascii=False, indent=2), encoding="utf-8")
    previsor = None if a.sin_prevision else crear_previsor(alm, sensores, hora)
    escribir(datos, (a.ffmc, a.dmc, a.dc), hora, ahora, a.dias_json, previsor)
    print(f"\nListo: {nuevas} filas nuevas, {cli.llamadas} llamadas a la API. "
          f"Escrito {SALIDA} y {HISTORIAL}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
