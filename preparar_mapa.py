#!/usr/bin/env python3
"""
Prepara los datos geográficos del mapa del panel FWI (sin dependencias externas).

Entrada : shapefile de límites municipales de Gipuzkoa (B5m, ETRS89 / UTM 30N).
Salida  : docs/data/gipuzkoa.geojson   contorno de la provincia
          docs/data/municipios.geojson municipios (con su estación de referencia)
          docs/data/zonas.geojson      zonas de influencia (polígonos de Thiessen) de cada estación

Uso: py preparar_mapa.py GFA_DSET_MB_SHP.zip
"""
import json, math, os, sys, tempfile, zipfile
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shp_tools as st

# Estaciones: código, latitud, longitud, altitud (m)
ESTACIONES = {
    "Arrasate":  ("C023", 43.0695849, -2.493080, 318),
    "Miramon":   ("C017", 43.2868000, -1.971210, 113),
    "Bidania":   ("C058", 43.1460000, -2.155020, 592),
    "Berastegi": ("C026", 43.1248000, -1.981700, 379),
    "Zegama":    ("C028", 42.9588000, -2.298520, 520),
    "Mutriku":   (None, 43.3072000, -2.385000, 20),    # sin estación física: estimación de Open-Meteo
    "Zarautz":   ("C064", 43.2930000, -2.145400, 10),   # + C086 Inurritza (lluvia)
}
MODELO_ESTACIONES = {"Mutriku"}
TOL = 40.0      # tolerancia de simplificación (m)
PASO = 250.0    # paso de la rejilla para repartir cada municipio entre estaciones (m)


def a_lonlat(anillo):
    return [[round(x, 5), round(y, 5)] for x, y in (st.utm30n_a_wgs84(px, py) for px, py in anillo)]


def orientar(coords, antihorario=True):
    a = sum(coords[i][0]*coords[i+1][1] - coords[i+1][0]*coords[i][1] for i in range(len(coords)-1))
    if (a > 0) != antihorario:
        coords.reverse()
    return coords


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__); return 1
    tmp = tempfile.mkdtemp()
    with zipfile.ZipFile(sys.argv[1]) as z:
        z.extractall(tmp)
    base = next(os.path.join(tmp, f[:-4]) for f in os.listdir(tmp) if f.endswith(".shp"))
    regs, dbf = st.leer_shp(base + ".shp"), st.leer_dbf(base + ".dbf")
    print(f"{len(regs)} polígonos leídos")
    os.makedirs("docs/data", exist_ok=True)

    # ---- contorno de Gipuzkoa
    anillos, est = st.disolver(regs)
    anillos = [a for a in anillos if abs(st.area_anillo(a)) > 5000]      # descarta restos < 0,005 km²
    contorno = [st.simplificar(a, TOL) for a in anillos]
    print(f"contorno: {len(contorno)} anillos, {sum(len(a) for a in contorno)} puntos "
          f"(aristas totales {est['aristas_totales']}, restantes {est['aristas_restantes']})")
    multi = [[orientar(a_lonlat(a))] for a in contorno]
    json.dump({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"nombre": "Gipuzkoa"},
               "geometry": {"type": "MultiPolygon", "coordinates": multi}}]},
              open("docs/data/gipuzkoa.geojson", "w"), separators=(",", ":"))

    # ---- estaciones en UTM
    utm = {n: st.wgs84_a_utm30n(lon, lat) for n, (_, lat, lon, _) in ESTACIONES.items()}
    nombres = list(ESTACIONES)

    def mas_cercana(x, y):
        return min(nombres, key=lambda n: (x-utm[n][0])**2 + (y-utm[n][1])**2)

    # ---- zonas de influencia (Thiessen recortadas al contorno)
    feats = []
    for n in nombres:
        xi, yi = utm[n]
        partes = []
        for anillo in contorno:
            poly = anillo[:-1]
            for m in nombres:
                if m == n: continue
                xj, yj = utm[m]
                c = (xj**2 + yj**2 - xi**2 - yi**2)
                poly = st.recortar_semiplano(poly, lambda p, xj=xj, yj=yj, xi=xi, yi=yi, c=c:
                                             2*(p[0]*(xj-xi) + p[1]*(yj-yi)) - c)
                if not poly: break
            if len(poly) >= 3 and abs(st.area_anillo(poly + [poly[0]])) > 5000:
                partes.append([orientar(a_lonlat(poly + [poly[0]]))])
        cod, lat, lon, alt = ESTACIONES[n]
        feats.append({"type": "Feature", "properties": {"estacion": n, "codigo": cod, "lat": lat, "lon": lon, "alt": alt, "modelo": n in MODELO_ESTACIONES},
                      "geometry": {"type": "MultiPolygon", "coordinates": partes}})
    json.dump({"type": "FeatureCollection", "features": feats}, open("docs/data/zonas.geojson", "w"), separators=(",", ":"))

    # ---- municipios agrupados por código, con reparto entre estaciones
    grupos = defaultdict(list)
    for i, fila in enumerate(dbf):
        grupos[fila["CODMUNI"]].append((fila, regs[i]))
    mfeats = []
    for cod, items in sorted(grupos.items()):
        fila = items[0][0]
        anillos_m = [a for _, reg in items for a in reg]
        exteriores = [a for a in anillos_m if st.area_anillo(a) < 0]
        huecos = [a for a in anillos_m if st.area_anillo(a) > 0]
        reparto = defaultdict(int); total = 0
        for a in exteriores:
            xs = [p[0] for p in a]; ys = [p[1] for p in a]
            x = min(xs) + PASO/2
            while x < max(xs):
                y = min(ys) + PASO/2
                while y < max(ys):
                    if st.punto_en_anillo(x, y, a) and not any(st.punto_en_anillo(x, y, h) for h in huecos):
                        reparto[mas_cercana(x, y)] += 1; total += 1
                    y += PASO
                x += PASO
        if total == 0:                                    # municipios muy pequeños: se usa un punto interior
            x, y = exteriores[0][0]; reparto[mas_cercana(x, y)] = 1; total = 1
        shares = {n: round(100*v/total) for n, v in sorted(reparto.items(), key=lambda kv: -kv[1])}
        polys = []
        for a in exteriores:
            hs = [h for h in huecos if st.punto_en_anillo(h[0][0], h[0][1], a)]
            polys.append([orientar(a_lonlat(st.simplificar(a, TOL)))] +
                         [orientar(a_lonlat(st.simplificar(h, TOL)), antihorario=False) for h in hs])
        mfeats.append({"type": "Feature",
                       "properties": {"cod": cod, "nombre": fila["NAME_ES"] or fila["NAME"], "comarca": fila["REGION_ES"],
                                      "reparto": shares},
                       "geometry": {"type": "MultiPolygon", "coordinates": polys}})
    json.dump({"type": "FeatureCollection", "features": mfeats}, open("docs/data/municipios.geojson", "w"),
              ensure_ascii=False, separators=(",", ":"))
    print(f"{len(mfeats)} municipios")
    for f in ("gipuzkoa", "municipios", "zonas"):
        print(f"docs/data/{f}.geojson: {os.path.getsize(f'docs/data/{f}.geojson')//1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
