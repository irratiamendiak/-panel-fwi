#!/usr/bin/env python3
"""
Paso 2 del panel FWI: genera el CSV con los datos que necesita el FWI.

Para cada estación (Arrasate, Miramon, Bidania, Berastegi, Zegama) y cada día:
  - temperatura (°C), humedad relativa (%) y velocidad media del viento (km/h)
    a la hora indicada (12:00 por defecto)
  - lluvia acumulada (mm) en las 24 h anteriores a esa hora
    (de las 12:00 del día anterior a las 12:00 del día)

La salida es un CSV con las columnas
    fecha,estacion,temperatura,humedad,viento,lluvia
listo para importar en el panel.

Los sensores se detectan solos la primera vez (a partir del resumen diario de la
API) y se guardan en sensores.json para no repetir esa consulta. Si algún día
cambian los sensores de una estación, borra sensores.json.

Requisitos (los mismos que en el paso 1):
  py -m pip install pyjwt cryptography requests

Uso (PowerShell, en la carpeta del script):
  py euskalmet_fwi_datos.py --clave privateKey.pem --email TU_EMAIL --fecha 2026-09-19

Varios días seguidos (útil para arrancar la cascada de FFMC, DMC y DC):
  py euskalmet_fwi_datos.py --clave privateKey.pem --email TU_EMAIL --fecha 2026-09-01 --hasta 2026-09-19

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
from datetime import date, datetime, time as hora_t, timedelta
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
}

# Variable -> "tipoDeMedida/medida" tal como aparece en el "key" de la API
MEDIDAS = {
    "temperatura": "measuresForAir/temperature",
    "humedad": "measuresForAir/humidity",
    "viento": "measuresForWind/mean_speed",
    "lluvia": "measuresForWater/precipitation",
}

FICHERO_SENSORES = Path("sensores.json")
RE_HORA = re.compile(r"(\d{1,2}):(\d{2})")
RE_ESPERA = re.compile(r"wait\s+(\d+)\s+seconds", re.I)
LECTURAS_POR_DIA = 144  # tramos de 10 minutos en 24 h
MIN_LLUVIA = 130        # con menos lecturas (≈90 %) la lluvia de 24 h no es fiable y se omite el día


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


def valor_puntual(alm, cod, sens, var, dia, hora):
    d = alm.hora(cod, var, sens, dia, hora)
    v = d.get((hora, 0))
    return float(v) if v is not None else None


def lluvia_24h(alm, cod, sens, dia, hora):
    """Suma los tramos de lluvia desde (dia-1) a la hora indicada hasta (dia) a esa hora."""
    ini = datetime.combine(dia - timedelta(days=1), hora_t(hora, 0))
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


# ---------------------------------------------------------------- principal
def obtener_dia(cli, alm, sensores, cod, dia, hora, minimo=MIN_LLUVIA):
    """Lee los cuatro datos de una estación y un día.

    Devuelve ((t, h, w_ms, lluvia), n_lluvia, "") o (None, n_lluvia, motivo).
    Si falta algún dato, vuelve a detectar los sensores de esa estación en ese día: las
    estaciones a veces cambian de sensor y el código antiguo deja de devolver datos.
    """
    def leer(sens):
        t = valor_puntual(alm, cod, sens, "temperatura", dia, hora)
        h = valor_puntual(alm, cod, sens, "humedad", dia, hora)
        w = valor_puntual(alm, cod, sens, "viento", dia, hora)
        faltan = [k for k, v in (("temperatura", t), ("humedad", h), ("viento", w)) if v is None]
        if faltan:
            return None, 0, f"sin lectura a las {hora:02d}:00 de " + ", ".join(faltan)
        ll, n = lluvia_24h(alm, cod, sens, dia, hora)
        if n == 0:
            return None, 0, "sin lecturas de lluvia"
        if n < minimo:
            return None, n, f"lluvia incompleta ({n}/{LECTURAS_POR_DIA})"
        return (t, h, w, ll), n, ""

    datos, n, motivo = leer(sensores[cod])
    if datos is None:
        try:
            nuevos = detectar_sensores(cli, cod, dia)
        except RuntimeError:
            nuevos = None
        if nuevos and nuevos != sensores[cod]:
            alm.olvidar(cod, [dia, dia - timedelta(days=1)])
            datos2, n2, motivo2 = leer(nuevos)
            if datos2 is not None:
                print(f"  ({cod}: la estación ha cambiado de sensores; se usan los de {dia})")
                sensores[cod] = nuevos
                return datos2, n2, ""
    return datos, n, motivo


def main() -> int:
    p = argparse.ArgumentParser(description="Genera el CSV de entrada del panel FWI desde Euskalmet")
    p.add_argument("--clave", required=True, help="Ruta al fichero con la clave PRIVADA (PEM)")
    p.add_argument("--email", required=True, help="Email con el que solicitaste la clave")
    p.add_argument("--emisor", default="panel-fwi")
    p.add_argument("--fecha", default=None, help="Primer día AAAA-MM-DD (por defecto, ayer)")
    p.add_argument("--hasta", default=None, help="Último día AAAA-MM-DD (por defecto, el mismo que --fecha)")
    p.add_argument("--hora", type=int, default=12, help="Hora del dato, 0-23 (por defecto 12)")
    p.add_argument("--viento-kmh", action="store_true", help="El viento ya viene en km/h (no convertir)")
    p.add_argument("--muestra", action="store_true", help="Imprime una respuesta cruda y termina")
    p.add_argument("--sensores", default="sensores.json", metavar="FICHERO",
                   help="Fichero donde se guardan los sensores detectados (por defecto sensores.json)")
    p.add_argument("--completar", default=None, metavar="CSV",
                   help="CSV de una ejecución anterior: solo descarga lo que le falte y guarda uno completo")
    a = p.parse_args()

    if not 0 <= a.hora <= 23:
        print("--hora debe estar entre 0 y 23")
        return 1
    ini = date.fromisoformat(a.fecha) if a.fecha else date.today() - timedelta(days=1)
    fin = date.fromisoformat(a.hasta) if a.hasta else ini
    if fin < ini:
        print("--hasta no puede ser anterior a --fecha")
        return 1

    try:
        token = crear_token(a.clave, a.email, a.emisor)
    except FileNotFoundError:
        print(f"No encuentro el fichero de la clave: {a.clave}")
        return 1
    except Exception as e:
        print("No he podido firmar el token con esa clave:", type(e).__name__, str(e)[:200])
        return 1

    global FICHERO_SENSORES
    FICHERO_SENSORES = Path(a.sensores)
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
        ruta = ruta_lectura(cod, sensores[cod]["temperatura"], ini, a.hora)
        r = cli.get(ruta)
        print(f"Muestra: temperatura, {nombre} ({cod}), {ini} {a.hora:02d}h")
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
                        fila["humedad"], fila["viento"], fila["lluvia"]]
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
            wr.writerow(["fecha", "estacion", "temperatura", "humedad", "viento", "lluvia"])
            wr.writerows(filas)

    alm = Almacen(cli)
    filas = []
    omitidos = []  # (día, estación, motivo)
    factor_viento = 1.0 if a.viento_kmh else 3.6

    dia = ini
    while dia <= fin:
        print(f"\n--- {dia} ({a.hora:02d}:00) ---")
        for nombre, cod in ESTACIONES.items():
            if (dia.isoformat(), nombre) in previas:
                filas.append(previas[(dia.isoformat(), nombre)])
                continue
            sens = sensores.get(cod)
            if not sens:
                print(f"{nombre}: sin sensores detectados, se omite.")
                omitidos.append((dia, nombre, "sin sensores detectados"))
                continue
            try:
                datos, n, motivo = obtener_dia(cli, alm, sensores, cod, dia, a.hora)
            except RuntimeError as e:
                print(f"{nombre}: {e}")
                omitidos.append((dia, nombre, str(e)[:120]))
                continue
            if datos is None:
                print(f"{nombre}: {motivo}; se omite este día.")
                omitidos.append((dia, nombre, motivo))
                continue
            t, h, w, ll = datos

            aviso = "" if n >= LECTURAS_POR_DIA else f"  (¡solo {n} de {LECTURAS_POR_DIA} lecturas de lluvia!)"
            w_kmh = w * factor_viento
            print(f"{nombre}: T={t:.1f} °C  HR={h:.0f} %  viento={w_kmh:.1f} km/h  "
                  f"lluvia 24 h={ll:.2f} mm [{n}/{LECTURAS_POR_DIA}]{aviso}")
            filas.append([dia.isoformat(), nombre, round(t, 1), round(h, 1), round(w_kmh, 1), ll])
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
