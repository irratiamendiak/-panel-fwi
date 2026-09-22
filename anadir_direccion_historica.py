#!/usr/bin/env python3
"""
Añade la dirección del viento de 2010-2025 a un historial.csv ya existente.

El proceso diario ya descarga la dirección del viento desde hace poco, así que los días
nuevos ya la traen. Este script solo rellena el hueco de 2010 a 2025, con los datos del
histórico original (direccion_2010_2025.csv, incluido junto a este script). No toca ninguna
fila que ya tenga dirección (los días recientes, o si se ejecuta dos veces).

Uso (PowerShell, en la carpeta panel-fwi):
  py anadir_direccion_historica.py

Por defecto lee y escribe datos/historial.csv, y usa datos/direccion_2010_2025.csv como
fuente. Antes de escribir, guarda una copia del fichero original como historial.csv.bak.
"""
import argparse
import csv
import shutil
import sys
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--historial", default="datos/historial.csv")
    p.add_argument("--fuente", default="datos/direccion_2010_2025.csv")
    a = p.parse_args()

    historial, fuente = Path(a.historial), Path(a.fuente)
    if not historial.exists():
        print(f"No encuentro {historial}")
        return 1
    if not fuente.exists():
        print(f"No encuentro {fuente} (debe estar junto a este script, en datos/)")
        return 1

    direcciones = {}
    with open(fuente, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            direcciones[(r["fecha"], r["estacion"])] = r["direccion"]
    print(f"{len(direcciones)} direcciones históricas cargadas de {fuente}")

    with open(historial, newline="", encoding="utf-8") as f:
        filas = list(csv.DictReader(f))
    campos = list(filas[0].keys()) if filas else ["fecha", "estacion", "temperatura", "humedad", "viento", "lluvia", "direccion"]
    if "direccion" not in campos:
        campos.append("direccion")

    rellenadas = 0
    for fila in filas:
        if not (fila.get("direccion") or "").strip():
            d = direcciones.get((fila["fecha"], fila["estacion"]))
            if d is not None:
                fila["direccion"] = d
                rellenadas += 1
        else:
            fila.setdefault("direccion", "")

    shutil.copy(historial, historial.with_suffix(historial.suffix + ".bak"))
    with open(historial, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campos, lineterminator="\n")
        w.writeheader()
        w.writerows(filas)

    print(f"Rellenadas {rellenadas} filas de {len(filas)}. Copia de seguridad en {historial}.bak")
    return 0


if __name__ == "__main__":
    sys.exit(main())
