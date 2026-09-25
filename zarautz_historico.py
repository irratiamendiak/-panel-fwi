#!/usr/bin/env python3
"""
Histórico de Zarautz (C064, temperatura/humedad/viento/dirección) + Inurritza (C086, lluvia),
descargado de la API real de Euskalmet con tu clave. A diferencia de mutriku_historico.py, aquí
todo son mediciones reales, no una estimación de modelo.

Uso (PowerShell, en la carpeta panel-fwi):
  py zarautz_historico.py --clave privateKey.pem --email TU_EMAIL

Por defecto descarga de 2010-01-01 a ayer y guarda historial_zarautz.csv, con las mismas columnas
que datos/historial.csv, listo para fusionar con importar_historico.py. Respeta el límite de
peticiones de la API (espera cuando hace falta, igual que euskalmet_fwi_datos.py) y se puede
interrumpir y reanudar repitiendo el mismo comando: lo ya guardado se reutiliza.
Si falta algún dato se rellena con la regla general (ver euskalmet_fwi_datos.py, columna origen), y con
--lluvia-manual AAAA-MM-DD=mm se puede poner a mano un día que se conoce de otra forma.
"""
import argparse
import csv
import sys
from datetime import date, timedelta
from pathlib import Path

import euskalmet_fwi_datos as ew
import zarautz as zr


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--clave", required=True)
    p.add_argument("--email", required=True)
    p.add_argument("--emisor", default="panel-fwi")
    p.add_argument("--desde", default="2010-01-01")
    p.add_argument("--hasta", default=None)
    p.add_argument("--hora", type=int, default=None,
                   help="Hora OFICIAL del dato (por defecto, el mediodía solar: 13 en invierno, 14 en verano)")
    p.add_argument("--salida", default="historial_zarautz.csv")
    p.add_argument("--tiempo-max", type=float, default=60.0, help="Minutos máximos antes de parar y guardar lo que haya")
    p.add_argument("--lluvia-manual", action="append", default=[], metavar="FECHA=MM",
                   help="Lluvia puesta a mano para un día sin datos, p. ej. 2026-03-17=0 (se puede repetir)")
    a = p.parse_args()

    manual = {}
    for txt in a.lluvia_manual:
        try:
            f_, mm = txt.split("=")
            manual[date.fromisoformat(f_.strip()).isoformat()] = float(mm.replace(",", "."))
        except ValueError:
            print(f"--lluvia-manual mal escrito: {txt} (debe ser AAAA-MM-DD=mm, p. ej. 2026-03-17=0)")
            return 1

    ini = date.fromisoformat(a.desde)
    fin = date.fromisoformat(a.hasta) if a.hasta else date.today() - timedelta(days=1)

    try:
        ew.crear_token(a.clave, a.email, a.emisor)
    except FileNotFoundError:
        print(f"No encuentro el fichero de la clave: {a.clave}")
        return 1

    cli = ew.Cliente(lambda: ew.crear_token(a.clave, a.email, a.emisor))
    alm = ew.Almacen(cli)
    sens = {}
    if not zr.asegurar_sensores(cli, sens, ini, alm, a.hora if a.hora is not None else ew.hora_solar(ini)):
        print("No se han podido detectar los sensores de Zarautz (C064) e Inurritza (C086) en", ini)
        return 1
    print("Sensores:", sens)

    import time
    limite = time.monotonic() + a.tiempo_max * 60

    # Si ya existe un resultado de una ejecución anterior, se reutiliza en vez de empezar de cero:
    # así, repetir el comando con --desde para continuar no borra lo que ya se había conseguido.
    filas = []
    existentes = set()
    salida_path = Path(a.salida)
    if salida_path.exists():
        with open(salida_path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                filas.append([r["fecha"], r["estacion"], r["temperatura"], r["humedad"], r["viento"],
                             r["lluvia"], r.get("direccion", ""), ew.origen_de_fila(r)])
                existentes.add(r["fecha"])
        if filas:
            print(f"Se reutilizan {len(filas)} días ya guardados en {a.salida}; solo se pedirá lo que falte.")

    def guardar():
        filas.sort(key=lambda r: r[0])   # siempre en orden de fecha: el FWI encadena cada día con el anterior
        with open(a.salida, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["fecha", "estacion", "temperatura", "humedad", "viento", "lluvia", "direccion",
                        "origen"])
            w.writerows(filas)

    print(f"Descargando Zarautz de {ini} a {fin}...")
    dia = ini
    while dia <= fin:
        if dia.isoformat() in existentes:
            dia += timedelta(days=1)
            continue
        if time.monotonic() > limite or cli.limite_agotado >= 2 or cli.errores_conexion >= 3:
            print(f"\nSe detiene por tiempo o por el límite de la API en {dia}. Repite el mismo comando:"
                  " lo ya guardado se reutiliza y solo se pedirá lo que falte.")
            break
        fila, motivo = zr.leer_dia(cli, alm, sens, dia, a.hora if a.hora is not None else ew.hora_solar(dia),
                                   manual.get(dia.isoformat()),
                                   rellenar_huecos=ew.se_puede_rellenar(dia))
        if fila:
            filas.append([fila["fecha"], zr.NOMBRE, fila["temperatura"], fila["humedad"], fila["viento"],
                         fila["lluvia"], fila["direccion"] if fila["direccion"] is not None else "",
                         fila.get("origen", "")])
            if motivo:
                print(f"  {dia}: {motivo}")
            if len(filas) % 60 == 0:
                guardar()
                print(f"  ...{fila['fecha']} ({len(filas)} días)")
        else:
            print(f"  {dia}: {motivo}")
        dia += timedelta(days=1)

    guardar()

    tengo = {r[0] for r in filas}
    faltan = [(ini + timedelta(days=i)).isoformat() for i in range((fin - ini).days + 1)
              if (ini + timedelta(days=i)).isoformat() not in tengo]
    if faltan:
        print(f"\nFaltan {len(faltan)} días: {', '.join(faltan)}")

    print(f"\nListo: {len(filas)} días en {a.salida} ({cli.llamadas} llamadas a la API).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
