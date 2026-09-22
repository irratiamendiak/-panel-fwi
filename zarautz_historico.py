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
interrumpir y reanudar: guarda el CSV después de cada día.
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
    p.add_argument("--hora", type=int, default=12)
    p.add_argument("--salida", default="historial_zarautz.csv")
    p.add_argument("--tiempo-max", type=float, default=60.0, help="Minutos máximos antes de parar y guardar lo que haya")
    a = p.parse_args()

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
    if not zr.asegurar_sensores(cli, sens, ini, alm, a.hora):
        print("No se han podido detectar los sensores de Zarautz (C064) e Inurritza (C086) en", ini)
        return 1
    print("Sensores:", sens)

    import time
    limite = time.monotonic() + a.tiempo_max * 60
    filas = []

    def guardar():
        with open(a.salida, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["fecha", "estacion", "temperatura", "humedad", "viento", "lluvia", "direccion"])
            w.writerows(filas)

    print(f"Descargando Zarautz de {ini} a {fin}...")
    dia = ini
    while dia <= fin:
        if time.monotonic() > limite or cli.limite_agotado >= 2 or cli.errores_conexion >= 3:
            print("\nSe detiene por tiempo o por el límite de la API. Repite el comando para continuar desde aquí"
                  " (usa --desde con el día siguiente al último de la lista de abajo).")
            break
        fila, motivo = zr.leer_dia(cli, alm, sens, dia, a.hora)
        if fila:
            filas.append([fila["fecha"], zr.NOMBRE, fila["temperatura"], fila["humedad"], fila["viento"],
                         fila["lluvia"], fila["direccion"] if fila["direccion"] is not None else ""])
            if len(filas) % 60 == 0:
                guardar()
                print(f"  ...{fila['fecha']} ({len(filas)} días)")
        else:
            print(f"  {dia}: {motivo}")
        dia += timedelta(days=1)

    guardar()
    print(f"\nListo: {len(filas)} días en {a.salida} ({cli.llamadas} llamadas a la API).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
