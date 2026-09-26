#!/usr/bin/env python3
"""
Paso 2 del panel FWI: genera el CSV con los datos que necesita el FWI.

Para cada estación (Arrasate, Miramon, Bidania, Berastegi, Zegama) y cada día:
  - temperatura (°C), humedad relativa (%) y velocidad media del viento (km/h)
    a la hora indicada (12:00 por defecto)
  - lluvia acumulada (mm) en las 24 h anteriores a esa hora
    (de las 12:00 del día anterior a las 12:00 del día)

La salida es un CSV con las columnas
    fecha,estacion,temperatura,humedad,viento,lluvia,direccion,origen
listo para importar en el panel.

Regla para datos que faltan (igual en todas las estaciones):
  * lluvia: se toma de la estación vecina más cercana que la tenga completa (VECINAS_LLUVIA);
    si ninguna la tiene (fallo general), de Open-Meteo.
  * temperatura, humedad y viento (con su dirección): primero, el valor más desfavorable de la propia
    estación en ±50 min de la hora del dato (humedad mínima, temperatura y viento máximos); si no hay
    ninguno, Open-Meteo en las coordenadas de la estación (COORDENADAS).
La columna 'origen' dice qué se ha rellenado y de dónde, p. ej. "humedad:Open-Meteo; lluvia:Zizurkil".
Vacía = todo medido por la propia estación.

Los sensores se detectan solos la primera vez (a partir del resumen diario de la
API) y se guardan en sensores.json para no repetir esa consulta. Si algún día
cambian los sensores de una estación, borra sensores.json.

Requisitos (los mismos que en el paso 1):
  py -m pip install pyjwt cryptography requests

Uso (PowerShell, en la carpeta del script):
  py euskalmet_fwi_datos.py --clave privateKey.pem --email TU_EMAIL --fecha 2026-09-19

Varios días seguidos (útil para arrancar la cascada de FFMC, DMC y DC):
  py euskalmet_fwi_datos.py --clave privateKey.pem --email TU_EMAIL --fecha 2026-09-01 --hasta 2026-09-19

Solo algunas estaciones (p. ej. para bajar el histórico de las que se añadieron después):
  py euskalmet_fwi_datos.py --clave privateKey.pem --email TU_EMAIL --fecha 2024-07-23 --hasta 2026-09-23 --estaciones Pasaia,Zizurkil,Ordizia

Opciones útiles:
  --hora 13        hora del dato (por defecto 12; ver nota sobre hora solar)
  --viento-kmh     si la API ya da el viento en km/h (por defecto se asume m/s y se convierte)
  --muestra        imprime la respuesta cruda de una lectura y termina (para depurar)
  --completar CSV  reutiliza un CSV anterior y solo descarga los días/estaciones que faltan
                   (guarda un fichero nuevo terminado en _completo.csv)

Límite de uso: la API responde 429 ("Please wait N seconds") si se hacen demasiadas
consultas por minuto. El script espera ese tiempo y sigue solo.

Supuestos que conviene verificar la primera vez:
  * Los valores llegan en tramos de 10 minutos y se toma el tramo que empieza a la hora
    indicada (12:00-12:09).
  * El viento medio (mean_speed) llega en m/s. Compara un día con la web de Euskalmet.
  * La hora de la API es hora local oficial. El FWI clásico usa las 12:00 hora solar,
    que en verano equivale a las 13:00 oficiales (usa --hora 13 si quieres ese criterio).

Seguridad: no imprime ni guarda el token ni la clave.
"""
import argparse
import csv
import json
import re
import sys
import time
from datetime import date, datetime, time as hora_t, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import jwt        # PyJWT
import requests

BASE = "https://api.euskadi.eus"

ESTACIONES = {
    "Arrasate": "C023",
    "Miramon": "C017",
    "Bidania": "C058",
    "Berastegi": "C026",
    "Zegama": "C028",
    "Ordizia": "C043",
    "Zizurkil": "C029",
    "Pasaia": "B096",   # plataforma océano-meteorológica, en la bocana de la bahía
}

# Variable -> "tipoDeMedida/medida" tal como aparece en el "key" de la API
MEDIDAS = {
    "temperatura": "measuresForAir/temperature",
    "humedad": "measuresForAir/humidity",
    "viento": "measuresForWind/mean_speed",
    "lluvia": "measuresForWater/precipitation",
}
# Opcional: si la estación no la tiene ese día, no impide calcular el resto (solo se pierde el aviso de viento)
MEDIDAS_OPCIONALES = {"direccion": "measuresForWind/mean_direction"}

FICHERO_SENSORES = Path("sensores.json")
RE_HORA = re.compile(r"(\d{1,2}):(\d{2})")
RE_ESPERA = re.compile(r"wait\s+(\d+)\s+seconds", re.I)
LECTURAS_POR_DIA = 144  # tramos de 10 minutos en 24 h
MIN_LLUVIA = 130        # con menos lecturas (≈90 %) la lluvia de 24 h no es fiable y se omite el día

# Nombre de cada código (para la columna origen). Altzola no está en ESTACIONES pero puede ser vecina.
NOMBRES = {cod: nombre for nombre, cod in ESTACIONES.items()}
NOMBRES["C078"] = "Altzola"
NOMBRES["C064"] = "Zarautz"
NOMBRES["C086"] = "Inurritza"

# Si a una estación le falta lluvia un día, se toma la de la estación vecina más cercana, por orden:
# si la primera tampoco la tiene completa, se prueba la segunda. Revisa el orden si conoces mejor la zona.
VECINAS_LLUVIA = {
    "C078": ["C023", "C058"],   # Altzola   -> Arrasate, Bidania
    "C023": ["C028", "C078"],   # Arrasate  -> Zegama, Altzola
    "C028": ["C043", "C023"],   # Zegama    -> Ordizia, Arrasate
    "C043": ["C058", "C028"],   # Ordizia   -> Bidania, Zegama
    "C058": ["C029", "C043"],   # Bidania   -> Zizurkil, Ordizia
    "C029": ["C058", "C026"],   # Zizurkil  -> Bidania, Berastegi
    "C026": ["C029", "C058"],   # Berastegi -> Zizurkil, Bidania
    "C017": ["B096", "C029"],   # Miramon   -> Pasaia, Zizurkil
    "B096": ["C017"],           # Pasaia    -> Miramon
    "C086": ["C029", "C058"],   # Inurritza (lluvia de Zarautz) -> Zizurkil, Bidania
}
_SENSOR_LLUVIA = {}  # código de la vecina -> sensor de lluvia ya detectado

# Coordenadas (latitud, longitud, altitud en m) para pedir a Open-Meteo los datos que falten.
# Son las mismas que COORD de actualizar_fwi.py (si cambias unas, cambia las otras). Con altitud
# None, Open-Meteo usaría la del terreno en ese punto.
# Coordenadas y altitudes oficiales de las fichas de Euskalmet (euskadi.eus), revisadas el 26/09/2026.
COORDENADAS = {
    "C023": (43.0695849, -2.493080, 318),   # Arrasate
    "C017": (43.2868, -1.97121, 113),       # Miramon
    "C058": (43.146, -2.15502, 592),        # Bidania
    "C026": (43.1248, -1.9817, 379),        # Berastegi
    "C028": (42.9588, -2.29852, 520),       # Zegama
    "C043": (43.0484, -2.17755, 243),       # Ordizia
    "C029": (43.1901, -2.06181, 149),       # Zizurkil
    "B096": (43.3370283, -1.92752, 0),      # Pasaia (plataforma en la bocana)
    "C064": (43.293, -2.14542, 80),         # Zarautz
    "C078": (43.2419, -2.39784, 17),        # Altzola
    "C086": (43.2779811, -2.1693568, 5),    # Inurritza (lluvia de Zarautz)
}
DIAS_ESPERA = 3   # los días más recientes no se rellenan: puede que Euskalmet aún no haya publicado todo
VIENTO_FACTOR = 3.6   # m/s -> km/h de la API de Euskalmet (1.0 con --viento-kmh)


# ---------------------------------------------------------------- acceso a la API
def crear_token(ruta_clave: str, email: str, emisor: str) -> str:
    clave = Path(ruta_clave).read_text(encoding="utf-8")
    ahora = int(time.time())
    payload = {
        "aud": "met01.apikey",
        "iss": emisor,
        "iat": ahora,
        "exp": ahora + 3600,
        "version": "1.0.0",
        "email": email,
    }
    return jwt.encode(payload, clave, algorithm="RS256")


class Cliente:
    """Cliente HTTP. El token dura 1 hora, así que se renueva solo en las descargas largas."""

    def __init__(self, fabrica_token):
        self.fabrica = fabrica_token
        self.s = requests.Session()
        self.llamadas = 0
        self.max_esperas = 10      # avisos de límite (429) que se esperan por petición antes de rendirse
        self.limite_agotado = 0    # peticiones que se rindieron por el límite de uso
        self.max_fallos = 4        # errores de red o del servidor que se reintentan por petición
        self.errores_conexion = 0  # peticiones que se rindieron por falta de conexión
        self._renovar()

    def _renovar(self):
        self.s.headers.update({"Authorization": f"Bearer {self.fabrica()}", "Accept": "application/json"})
        self.t0 = time.time()

    def get(self, ruta: str) -> requests.Response:
        if time.time() - self.t0 > 2400:  # 40 minutos
            self._renovar()
        url = BASE + ruta
        r = None
        ultimo_error = None
        renovado = False
        fallos = 0   # errores de red o del servidor
        esperas = 0  # avisos de límite de uso (429)
        while fallos < self.max_fallos and esperas < self.max_esperas:
            try:
                r = self.s.get(url, timeout=(10, 30))   # 10 s para conectar, 30 s para recibir
                self.llamadas += 1
            except requests.RequestException as e:
                ultimo_error = e
                fallos += 1
                time.sleep(2 * fallos)
                continue
            if r.status_code == 401 and not renovado:
                self._renovar()
                renovado = True
                continue
            if r.status_code == 429:
                # La API indica cuánto esperar: "Please wait 53 seconds before retrying."
                esperas += 1
                m = RE_ESPERA.search(r.text or "")
                seg = min(int(m.group(1)) + 1, 120) if m else 30
                print(f"  Límite de la API alcanzado: espero {seg} s (aviso {esperas}/{self.max_esperas})...", flush=True)
                time.sleep(seg)
                continue
            if r.status_code in (500, 502, 503, 504):
                fallos += 1
                if fallos < self.max_fallos:
                    time.sleep(2 * fallos)
                    continue
            return r
        if r is None:
            self.errores_conexion += 1
            raise RuntimeError(f"Sin conexión con la API ({type(ultimo_error).__name__})")
        if r.status_code == 429:
            self.limite_agotado += 1
        return r


# ---------------------------------------------------------------- sensores
def intentar_direccion(cli: Cliente, alm: "Almacen", sensores: dict, cod: str, dia: date, hora: int) -> bool:
    """Prueba a encontrar el sensor de dirección de una estación y, si funciona, lo guarda en 'sensores'.

    La dirección no aparece en el catálogo de resúmenes diarios (no tiene sentido resumir un ángulo con
    una media o un máximo), así que no se puede detectar como el resto de sensores. Se prueba con el
    sensor de viento, que suele medir también la dirección (misma veleta). Solo se guarda si la prueba
    devuelve un dato real ese día; si no, no se guarda nada y se puede reintentar más adelante.
    Devuelve True si ha quedado guardada (ya sea ahora o en una llamada anterior).
    """
    if "direccion" in sensores.get(cod, {}):
        return True
    try:
        nuevos = detectar_sensores(cli, cod, dia)
    except RuntimeError:
        nuevos = None
    candidato = (nuevos or {}).get("direccion")
    if not candidato and "viento" in sensores.get(cod, {}):
        candidato = dict(sensores[cod]["viento"], medida="mean_direction")
    if not candidato:
        return False
    try:
        dv_prueba = valor_puntual(alm, cod, {"direccion": candidato}, "direccion", dia, hora)
    except RuntimeError:
        dv_prueba = None
    if dv_prueba is None:
        return False
    sensores[cod]["direccion"] = candidato
    return True


def detectar_sensores(cli: Cliente, cod: str, dia: date) -> dict:
    ruta = (f"/euskalmet/readings/aggregated/summarized/byDay/forStation/"
            f"{cod}/at/{dia:%Y}/{dia:%m}/{dia:%d}")
    r = cli.get(ruta)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} al listar los sensores de {cod}: {r.text[:200]}")
    claves = sorted({it.get("key") for it in r.json().get("items", []) if it.get("key")})
    res = {}
    for var, sufijo in MEDIDAS.items():
        cand = [k for k in claves if k.endswith("/" + sufijo)]
        if not cand:
            raise RuntimeError(f"{cod}: no hay ningún sensor '{sufijo}' ese día")
        if len(cand) > 1:
            print(f"  Aviso: {cod} tiene varios sensores '{sufijo}': {cand}. Uso {cand[0]}.")
        sensor, tipo, medida = cand[0].split("/")
        res[var] = {"sensor": sensor, "tipo": tipo, "medida": medida}
    for var, sufijo in MEDIDAS_OPCIONALES.items():
        cand = [k for k in claves if k.endswith("/" + sufijo)]
        if cand:
            sensor, tipo, medida = cand[0].split("/")
            res[var] = {"sensor": sensor, "tipo": tipo, "medida": medida}
    return res


def cargar_sensores(cli: Cliente, dia: date) -> dict:
    guardados = {}
    if FICHERO_SENSORES.exists():
        try:
            guardados = json.loads(FICHERO_SENSORES.read_text(encoding="utf-8"))
        except ValueError:
            guardados = {}
    cambios = False
    for nombre, cod in ESTACIONES.items():
        if cod in guardados:
            continue
        print(f"Detectando sensores de {nombre} ({cod})...")
        try:
            guardados[cod] = detectar_sensores(cli, cod, dia)
            cambios = True
        except RuntimeError as e:
            print("  ", e)
    if cambios:
        FICHERO_SENSORES.write_text(json.dumps(guardados, ensure_ascii=False, indent=2), encoding="utf-8")
    return guardados


# ---------------------------------------------------------------- lecturas
def ruta_lectura(cod: str, sen: dict, dia: date, hh: int) -> str:
    return (f"/euskalmet/readings/forStation/{cod}/{sen['sensor']}/measures/"
            f"{sen['tipo']}/{sen['medida']}/at/{dia:%Y}/{dia:%m}/{dia:%d}/{hh:02d}")


def leer_hora(cli: Cliente, cod: str, sen: dict, dia: date, hh: int):
    """Devuelve ({(hora, minuto): valor}, json_crudo) de una hora concreta."""
    ruta = ruta_lectura(cod, sen, dia, hh)
    r = cli.get(ruta)
    if r.status_code == 404:
        return {}, None
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} en {ruta}: {r.text[:200]}")
    datos = r.json()
    tramos = datos.get("slots") or datos.get("slices") or []
    valores = datos.get("values") or []
    salida = {}
    for i, tramo in enumerate(tramos):
        m = RE_HORA.search(tramo.get("range") or tramo.get("rangeDesc") or "")
        if m and i < len(valores):
            salida[(int(m.group(1)), int(m.group(2)))] = valores[i]
    return salida, datos


class Almacen:
    """Guarda lo ya descargado. Si una respuesta trae más horas de las pedidas, las reutiliza."""

    def __init__(self, cli: Cliente):
        self.cli = cli
        self.datos = {}      # (cod, variable, dia) -> {(h, m): valor}
        self.pedidas = set()  # (cod, variable, dia, hh) ya consultadas

    def hora(self, cod, var, sen, dia, hh):
        clave = (cod, var, dia)
        d = self.datos.setdefault(clave, {})
        if any(k[0] == hh for k in d) or (clave, hh) in self.pedidas:
            return d
        self.pedidas.add((clave, hh))
        try:
            nuevos, _ = leer_hora(self.cli, cod, sen[var], dia, hh)
        except RuntimeError:
            self.pedidas.discard((clave, hh))  # que se pueda reintentar más adelante
            raise
        d.update(nuevos)
        return d

    def olvidar(self, cod, dias):
        """Borra lo guardado de una estación en esos días (por si cambian sus sensores)."""
        dias = set(dias)
        for clave in [k for k in self.datos if k[0] == cod and k[2] in dias]:
            del self.datos[clave]
        self.pedidas = {x for x in self.pedidas if not (x[0][0] == cod and x[0][2] in dias)}


# ---------------------------------------------------------------- horas: local frente a UTC
# IMPORTANTE: la API de Euskalmet (y los XML de Open Data) dan las horas en UTC. En todo el código la
# hora del dato se maneja en hora OFICIAL de Euskadi (13:00 en invierno, 14:00 en verano = mediodía
# solar = 12:00 UTC), y se convierte a UTC justo al pedir lecturas a Euskalmet. Open-Meteo se pide
# con timezone=Europe/Madrid, así que ahí se usa la hora local tal cual.
ZONA = ZoneInfo("Europe/Madrid")


def hora_solar(d):
    """Mediodía solar de Gipuzkoa (12:00 UTC) en hora oficial: 14 con horario de verano, 13 sin él."""
    return 14 if datetime(d.year, d.month, d.day, 12, tzinfo=ZONA).dst() else 13


def a_utc(dia, hh, mm=0):
    """Hora oficial de Euskadi -> (fecha, hora, minuto) en UTC, que es como las sirve Euskalmet."""
    t = datetime(dia.year, dia.month, dia.day, hh, mm, tzinfo=ZONA).astimezone(timezone.utc)
    return t.date(), t.hour, t.minute


def a_local(dia_utc, hh, mm=0):
    """(fecha, hora, minuto) UTC -> texto 'HH:MM' en hora oficial de Euskadi."""
    t = datetime(dia_utc.year, dia_utc.month, dia_utc.day, hh, mm, tzinfo=timezone.utc).astimezone(ZONA)
    return t.strftime("%H:%M")


def valor_puntual(alm, cod, sens, var, dia, hora):
    """Lectura de 'var' a la hora OFICIAL 'hora' de ese día (se pide a Euskalmet en UTC)."""
    du, hu, _ = a_utc(dia, hora)
    d = alm.hora(cod, var, sens, du, hu)
    v = d.get((hu, 0))
    return float(v) if v is not None else None


# Si falta la lectura exacta de la hora del dato, se busca en la propia estación dentro de la ventana
# HH-1:10 .. HH:50 (±50 min) el valor más desfavorable para el riesgo: la humedad más baja, la
# temperatura más alta y el viento más alto. Solo si en la ventana no hay nada, se va a Open-Meteo.
VENTANA = {"temperatura": max, "humedad": min, "viento": max}


def valor_ventana(alm, cod, sens, var, dia, hora):
    """(valor, 'HH:MM' en hora oficial) más desfavorable de 'var' entre HH-1:10 y HH:50 (hora oficial)
    de ese día, o (None, None)."""
    if var not in sens or var not in VENTANA:
        return None, None
    du, hu, _ = a_utc(dia, hora)
    cand = []
    for hh, minutos in ((hu - 1, range(10, 60, 10)), (hu, range(0, 60, 10))):
        try:
            d = alm.hora(cod, var, sens, du, hh)
        except RuntimeError:
            continue
        for m in minutos:
            v = d.get((hh, m))
            if v is not None:
                cand.append((float(v), a_local(du, hh, m)))
    if not cand:
        return None, None
    return VENTANA[var](cand, key=lambda x: x[0])


def valor_en(alm, cod, sens, var, dia, hhmm):
    """Lectura de 'var' a una hora oficial concreta 'HH:MM' (la dirección que acompaña al viento elegido)."""
    if var not in sens:
        return None
    du, hh, mm = a_utc(dia, *(int(x) for x in hhmm.split(":")))
    try:
        v = alm.hora(cod, var, sens, du, hh).get((hh, mm))
    except RuntimeError:
        return None
    return float(v) if v is not None else None


def completar_ventana(alm, cod, sens, dia, hora, vals, dv):
    """Rellena en 'vals' (temperatura, humedad, viento en unidades de la API) lo que falte con la
    ventana de ±50 min. Devuelve (dv, notas) con la dirección del viento y las anotaciones para 'origen'."""
    notas = []
    for var in ("temperatura", "humedad", "viento"):
        if vals.get(var) is None:
            v, hhmm = valor_ventana(alm, cod, sens, var, dia, hora)
            if v is not None:
                vals[var] = v
                notas.append(f"{var}:{hhmm}")
                if var == "viento":
                    dv = valor_en(alm, cod, sens, "direccion", dia, hhmm)
    return dv, notas


def lluvia_24h(alm, cod, sens, dia, hora):
    """Suma los tramos de lluvia de las 24 h anteriores a la hora OFICIAL 'hora' de ese día."""
    du, hu, _ = a_utc(dia, hora)
    ini = datetime.combine(du, hora_t(hu, 0)) - timedelta(days=1)   # en UTC, como Euskalmet
    total, hay = 0.0, 0
    for h in range(24):
        t = ini + timedelta(hours=h)
        d = alm.hora(cod, "lluvia", sens, t.date(), t.hour)
        for m in range(0, 60, 10):
            v = d.get((t.hour, m))
            if v is not None:
                total += float(v)
                hay += 1
    return round(total, 2), hay


def sensor_lluvia(cli, cod, dia):
    """Sensor de lluvia de una estación en un día (del resumen diario), o None si no lo hay."""
    ruta = (f"/euskalmet/readings/aggregated/summarized/byDay/forStation/"
            f"{cod}/at/{dia:%Y}/{dia:%m}/{dia:%d}")
    try:
        r = cli.get(ruta)
    except RuntimeError:
        return None
    if r.status_code != 200:
        return None
    for it in r.json().get("items", []):
        k = it.get("key") or ""
        if k.endswith("/" + MEDIDAS["lluvia"]):
            sensor, tipo, medida = k.split("/")
            return {"sensor": sensor, "tipo": tipo, "medida": medida}
    return None


def lluvia_respaldo(cli, alm, cod, dia, hora, minimo=MIN_LLUVIA):
    """Lluvia de 24 h de la estación vecina más cercana que la tenga completa ese día.

    Devuelve (mm, nombre_de_la_vecina) o (None, None) si ninguna vecina sirve."""
    for vec in VECINAS_LLUVIA.get(cod, []):
        sen = _SENSOR_LLUVIA.get(vec) or sensor_lluvia(cli, vec, dia)
        if sen is None:
            continue
        _SENSOR_LLUVIA[vec] = sen
        try:
            ll, n = lluvia_24h(alm, vec, {"lluvia": sen}, dia, hora)
            if n < minimo:   # quizá la vecina cambió de sensor: se vuelve a detectar ese día
                nuevo = sensor_lluvia(cli, vec, dia)
                if nuevo and nuevo != sen:
                    _SENSOR_LLUVIA[vec] = nuevo
                    alm.olvidar(vec, [dia, dia - timedelta(days=1)])
                    ll, n = lluvia_24h(alm, vec, {"lluvia": nuevo}, dia, hora)
        except RuntimeError:
            continue
        if n >= minimo:
            return ll, NOMBRES.get(vec, vec)
    return None, None


# ---------------------------------------------------------------- Open-Meteo (respaldo)
URL_OM_ARCHIVO = "https://archive-api.open-meteo.com/v1/archive"
URL_OM_PREVISION = "https://api.open-meteo.com/v1/forecast"   # últimos ~90 días, que el archivo aún no tiene
VARS_OM = "temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m,precipitation"


class Modelo:
    """Datos horarios de Open-Meteo por estación, descargados por años y guardados en memoria."""

    def __init__(self):
        self.horas = {}       # cod -> {"AAAA-MM-DDTHH:00": (t, h, w_kmh, dir, lluvia_1h)}
        self.cargado = set()  # (cod, año) o (cod, "prevision")

    def _pedir(self, url, cod, ini, fin):
        if cod not in COORDENADAS:
            raise RuntimeError(f"no hay coordenadas de {cod} para Open-Meteo")
        if fin < ini:
            return
        lat, lon, alt = COORDENADAS[cod]
        params = {"latitude": lat, "longitude": lon, "timezone": "Europe/Madrid", "hourly": VARS_OM,
                  "wind_speed_unit": "kmh", "start_date": ini.isoformat(), "end_date": fin.isoformat()}
        if alt is not None:
            params["elevation"] = alt
        for _ in range(5):
            try:
                r = requests.get(url, params=params, timeout=90)
            except requests.RequestException:
                time.sleep(20)
                continue
            if r.status_code == 200:
                tb = r.json().get("hourly", {})
                d = self.horas.setdefault(cod, {})
                for i, ts in enumerate(tb.get("time", [])):
                    fila = tuple(tb[k][i] for k in VARS_OM.split(","))
                    if any(v is not None for v in fila):
                        d[ts] = fila
                return
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(20)
                continue
            raise RuntimeError(f"Open-Meteo respondió HTTP {r.status_code}: {r.text[:200]}")
        raise RuntimeError("Open-Meteo no responde")

    def _asegurar(self, cod, dias):
        ayer = date.today() - timedelta(days=1)
        for anio in sorted({d.year for d in dias}):
            if (cod, anio) not in self.cargado:
                self._pedir(URL_OM_ARCHIVO, cod, date(anio, 1, 1), min(date(anio, 12, 31), ayer))
                self.cargado.add((cod, anio))

    def dia(self, cod, dia, hora):
        """{'temperatura','humedad','viento'(km/h),'direccion','lluvia'(24 h)} a esa hora; None donde falte."""
        ahora = datetime.combine(dia, hora_t(hora, 0))
        claves = [(ahora - timedelta(hours=k)).strftime("%Y-%m-%dT%H:00") for k in range(24)]
        self._asegurar(cod, [dia, dia - timedelta(days=1)])
        d = self.horas.get(cod, {})
        if any(k not in d for k in claves) and (cod, "prevision") not in self.cargado \
                and dia >= date.today() - timedelta(days=90):
            self.cargado.add((cod, "prevision"))
            self._pedir(URL_OM_PREVISION, cod, date.today() - timedelta(days=91), date.today())
            d = self.horas.get(cod, {})
        t, h, w, dv, _ = d.get(claves[0], (None,) * 5)
        lluvias = [d.get(k, (None,) * 5)[4] for k in claves]   # cada valor = lluvia de la hora anterior
        ll = round(sum(lluvias), 2) if None not in lluvias else None
        return {"temperatura": t, "humedad": h, "viento": w, "direccion": dv, "lluvia": ll}


MODELO = Modelo()


def rellenar(cli, alm, cod, dia, hora, t, h, w_kmh, dv, ll, n, minimo=MIN_LLUVIA, cod_lluvia=None, origen=None):
    """Aplica la regla general a los datos que falten (None) de una estación y un día.

    Lluvia (si n < minimo): estación vecina más cercana con la lluvia completa; si ninguna, Open-Meteo.
    Temperatura, humedad y viento (con su dirección): Open-Meteo en las coordenadas de 'cod'.
    'cod_lluvia' es la estación de la que sale la lluvia, si es otra (Inurritza en Zarautz).
    Devuelve ((t, h, w_kmh, dv, ll, origen), "") o (None, motivo)."""
    origen = list(origen or [])
    faltan = [k for k, v in (("temperatura", t), ("humedad", h), ("viento", w_kmh)) if v is None]
    falta_lluvia = n < minimo
    motivo_ll = ("sin lecturas de lluvia" if n == 0 else f"lluvia incompleta ({n}/{LECTURAS_POR_DIA})")
    if falta_lluvia:
        ll_v, vec = lluvia_respaldo(cli, alm, cod_lluvia or cod, dia, hora, minimo)
        if ll_v is not None:
            ll, falta_lluvia = ll_v, False
            origen.append(f"lluvia:{vec}")
    if faltan or falta_lluvia:
        try:
            m = MODELO.dia(cod, dia, hora)
        except RuntimeError as e:
            m = {}
            print(f"  (Open-Meteo: {e})")
        for var in faltan:
            if m.get(var) is None:
                return None, f"sin {var} a las {hora:02d}:00 ni en la estación ni en Open-Meteo"
        if falta_lluvia and m.get("lluvia") is None:
            return None, motivo_ll + "; tampoco en las vecinas ni en Open-Meteo"
        if "temperatura" in faltan:
            t = m["temperatura"]
            origen.append("temperatura:Open-Meteo")
        if "humedad" in faltan:
            h = m["humedad"]
            origen.append("humedad:Open-Meteo")
        if "viento" in faltan:
            w_kmh, dv = m["viento"], m["direccion"]
            origen.append("viento:Open-Meteo")
        if falta_lluvia:
            ll = m["lluvia"]
            origen.append("lluvia:Open-Meteo")
    return (t, h, w_kmh, dv, ll, "; ".join(origen)), ""


def faltas(t, h, w, n, minimo):
    """Texto con lo que falta de un día ("" si está completo)."""
    f = [k for k, v in (("temperatura", t), ("humedad", h), ("viento", w)) if v is None]
    if n < minimo:
        f.append("lluvia" if n == 0 else f"lluvia ({n}/{LECTURAS_POR_DIA})")
    return ", ".join(f)


def se_puede_rellenar(dia, hasta=None):
    """True si el día es lo bastante antiguo para rellenar lo que falte (ver DIAS_ESPERA)."""
    hasta = hasta or date.today()
    return dia <= hasta - timedelta(days=DIAS_ESPERA)


def origen_de_fila(fila):
    """Lee la columna 'origen' de un CSV, convirtiendo la antigua 'lluvia_origen' si es lo que hay."""
    if fila.get("origen"):
        return fila["origen"]
    viejo = (fila.get("lluvia_origen") or "").strip()
    return f"lluvia:{viejo}" if viejo else ""


# ---------------------------------------------------------------- principal
def obtener_dia(cli, alm, sensores, cod, dia, hora, minimo=MIN_LLUVIA, rellenar_huecos=True):
    """Lee los datos de una estación y un día.

    Devuelve ((t, h, w, lluvia, dir, origen), n_lluvia, "") o (None, n_lluvia, motivo); w en las
    unidades de la API (se multiplica por VIENTO_FACTOR para km/h). Si falta algún dato, primero
    vuelve a detectar los sensores (las estaciones a veces cambian de sensor) y, si sigue faltando,
    lo rellena con la regla general (ver rellenar()), salvo con rellenar_huecos=False: entonces
    devuelve None para reintentarlo más adelante (días recientes que quizá aún no están publicados).
    """
    def leer(sens):
        vals = {v: valor_puntual(alm, cod, sens, v, dia, hora) for v in ("temperatura", "humedad", "viento")}
        ll, n = lluvia_24h(alm, cod, sens, dia, hora)
        return vals, ll, n

    def incompleto(vals, n):
        return None in vals.values() or n < minimo

    vals, ll, n = leer(sensores[cod])
    if incompleto(vals, n):
        try:
            nuevos = detectar_sensores(cli, cod, dia)
        except RuntimeError:
            nuevos = None
        if nuevos and nuevos != sensores[cod]:
            alm.olvidar(cod, [dia, dia - timedelta(days=1)])
            v2, ll2, n2 = leer(nuevos)
            if not incompleto(v2, n2):
                print(f"  ({cod}: la estación ha cambiado de sensores; se usan los de {dia})")
                sensores[cod] = nuevos
                vals, ll, n = v2, ll2, n2
    elif "direccion" not in sensores[cod]:
        intentar_direccion(cli, alm, sensores, cod, dia, hora)

    dv = None
    if "direccion" in sensores[cod] and vals["viento"] is not None:
        dv = valor_puntual(alm, cod, sensores[cod], "direccion", dia, hora)
    if not rellenar_huecos and incompleto(vals, n):
        return None, n, "falta " + faltas(vals["temperatura"], vals["humedad"], vals["viento"], n, minimo)
    # 1) lo que falte a la hora exacta: valor más desfavorable de la propia estación en ±50 min
    dv, notas = completar_ventana(alm, cod, sensores[cod], dia, hora, vals, dv)
    w_kmh = vals["viento"] * VIENTO_FACTOR if vals["viento"] is not None else None
    # 2) lo que siga faltando: regla general (lluvia de la vecina; el resto, Open-Meteo)
    res, motivo = rellenar(cli, alm, cod, dia, hora, vals["temperatura"], vals["humedad"], w_kmh, dv,
                           ll, n, minimo, origen=notas)
    if res is None:
        return None, n, motivo
    t, h, w_kmh, dv, ll, origen = res
    return (t, h, w_kmh / VIENTO_FACTOR, ll, dv, origen), n, ""


def main() -> int:
    p = argparse.ArgumentParser(description="Genera el CSV de entrada del panel FWI desde Euskalmet")
    p.add_argument("--clave", required=True, help="Ruta al fichero con la clave PRIVADA (PEM)")
    p.add_argument("--email", required=True, help="Email con el que solicitaste la clave")
    p.add_argument("--emisor", default="panel-fwi")
    p.add_argument("--fecha", default=None, help="Primer día AAAA-MM-DD (por defecto, ayer)")
    p.add_argument("--hasta", default=None, help="Último día AAAA-MM-DD (por defecto, el mismo que --fecha)")
    p.add_argument("--hora", type=int, default=None,
                   help="Hora OFICIAL del dato, 0-23 (por defecto, el mediodía solar: 13 en invierno, 14 en verano)")
    p.add_argument("--viento-kmh", action="store_true", help="El viento ya viene en km/h (no convertir)")
    p.add_argument("--muestra", action="store_true", help="Imprime una respuesta cruda y termina")
    p.add_argument("--sensores", default="sensores.json", metavar="FICHERO",
                   help="Fichero donde se guardan los sensores detectados (por defecto sensores.json)")
    p.add_argument("--estaciones", default=None, metavar="LISTA",
                   help="Solo estas estaciones, separadas por comas (p. ej. Pasaia,Zizurkil,Ordizia)")
    p.add_argument("--completar", default=None, metavar="CSV",
                   help="CSV de una ejecución anterior: solo descarga lo que le falte y guarda uno completo")
    a = p.parse_args()

    if a.hora is not None and not 0 <= a.hora <= 23:
        print("--hora debe estar entre 0 y 23")
        return 1
    ini = date.fromisoformat(a.fecha) if a.fecha else date.today() - timedelta(days=1)
    fin = date.fromisoformat(a.hasta) if a.hasta else ini
    if fin < ini:
        print("--hasta no puede ser anterior a --fecha")
        return 1
    estaciones = dict(ESTACIONES)
    if a.estaciones:
        pedidas = [x.strip().lower() for x in a.estaciones.split(",") if x.strip()]
        estaciones = {n: c for n, c in ESTACIONES.items() if n.lower() in pedidas}
        desconocidas = set(pedidas) - {n.lower() for n in estaciones}
        if desconocidas:
            print("Estaciones desconocidas:", ", ".join(sorted(desconocidas)),
                  "| disponibles:", ", ".join(ESTACIONES))
            return 1

    try:
        token = crear_token(a.clave, a.email, a.emisor)
    except FileNotFoundError:
        print(f"No encuentro el fichero de la clave: {a.clave}")
        return 1
    except Exception as e:
        print("No he podido firmar el token con esa clave:", type(e).__name__, str(e)[:200])
        return 1

    global FICHERO_SENSORES, VIENTO_FACTOR
    FICHERO_SENSORES = Path(a.sensores)
    VIENTO_FACTOR = 1.0 if a.viento_kmh else 3.6
    cli = Cliente(lambda: crear_token(a.clave, a.email, a.emisor))
    try:
        sensores = cargar_sensores(cli, ini)
    except RuntimeError as e:
        print(e)
        return 1

    if a.muestra:
        nombre, cod = next(iter(ESTACIONES.items()))
        if cod not in sensores:
            print(f"No hay sensores detectados para {nombre}.")
            return 1
        du, hu, _ = a_utc(ini, a.hora if a.hora is not None else hora_solar(ini))
        ruta = ruta_lectura(cod, sensores[cod]["temperatura"], du, hu)
        r = cli.get(ruta)
        print(f"Muestra: temperatura, {nombre} ({cod}), {du} {hu:02d}h UTC")
        print("Ruta:", ruta)
        print("HTTP", r.status_code)
        print(r.text[:3000])
        return 0

    sensores_inicio = json.loads(json.dumps(sensores))
    previas = {}
    if a.completar:
        try:
            with open(a.completar, newline="", encoding="utf-8") as f:
                for fila in csv.DictReader(f):
                    previas[(fila["fecha"], fila["estacion"])] = [
                        fila["fecha"], fila["estacion"], fila["temperatura"],
                        fila["humedad"], fila["viento"], fila["lluvia"], fila.get("direccion", ""),
                        origen_de_fila(fila)]
        except (OSError, KeyError) as e:
            print(f"No puedo leer {a.completar}: {type(e).__name__} {e}")
            return 1
        print(f"Se reutilizan {len(previas)} filas de {a.completar}; solo se descargará lo que falte.")

    nombre_csv = f"fwi_{ini}.csv" if ini == fin else f"fwi_{ini}_{fin}.csv"
    if a.completar:
        nombre_csv = nombre_csv.replace(".csv", "_completo.csv")

    def guardar():
        with open(nombre_csv, "w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            wr.writerow(["fecha", "estacion", "temperatura", "humedad", "viento", "lluvia", "direccion",
                         "origen"])
            wr.writerows(filas)

    alm = Almacen(cli)
    filas = []
    omitidos = []  # (día, estación, motivo)
    factor_viento = 1.0 if a.viento_kmh else 3.6

    dia = ini
    while dia <= fin:
        hora_d = a.hora if a.hora is not None else hora_solar(dia)
        print(f"\n--- {dia} ({hora_d:02d}:00 hora oficial) ---")
        for nombre, cod in estaciones.items():
            if (dia.isoformat(), nombre) in previas:
                filas.append(previas[(dia.isoformat(), nombre)])
                continue
            sens = sensores.get(cod)
            if not sens:
                print(f"{nombre}: sin sensores detectados, se omite.")
                omitidos.append((dia, nombre, "sin sensores detectados"))
                continue
            try:
                datos, n, motivo = obtener_dia(cli, alm, sensores, cod, dia, hora_d,
                                               rellenar_huecos=se_puede_rellenar(dia))
            except RuntimeError as e:
                print(f"{nombre}: {e}")
                omitidos.append((dia, nombre, str(e)[:120]))
                continue
            if datos is None:
                print(f"{nombre}: {motivo}; se omite este día.")
                omitidos.append((dia, nombre, motivo))
                continue
            t, h, w, ll, dv, origen = datos

            if origen:
                aviso = f"  (rellenado: {origen})"
            else:
                aviso = "" if n >= LECTURAS_POR_DIA else f"  (¡solo {n} de {LECTURAS_POR_DIA} lecturas de lluvia!)"
            w_kmh = w * factor_viento
            dv_txt = f"  dir={dv:.0f}°" if dv is not None else "  (sin dirección de viento)"
            print(f"{nombre}: T={t:.1f} °C  HR={h:.0f} %  viento={w_kmh:.1f} km/h  "
                  f"lluvia 24 h={ll:.2f} mm [{n}/{LECTURAS_POR_DIA}]{aviso}{dv_txt}")
            filas.append([dia.isoformat(), nombre, round(t, 1), round(h, 1), round(w_kmh, 1), ll,
                          round(dv, 0) if dv is not None else "", origen])
        if filas:
            guardar()   # se guarda cada día: si se corta la ejecución, se puede reanudar con --completar
        dia += timedelta(days=1)

    if sensores != sensores_inicio:
        FICHERO_SENSORES.write_text(json.dumps(sensores, ensure_ascii=False, indent=2), encoding="utf-8")
    if omitidos:
        print(f"\nDías/estaciones omitidos ({len(omitidos)}):")
        for d_, n_, motivo in omitidos:
            print(f"  {d_} {n_}: {motivo}")

    if not filas:
        print("\nNo se ha generado ninguna fila. Revisa los mensajes de arriba.")
        return 1

    guardar()
    print(f"\nListo: {len(filas)} filas en {nombre_csv} ({cli.llamadas} llamadas a la API).")
    print("Impórtalo en el panel con 'Cargar fichero' o pegando su contenido.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
