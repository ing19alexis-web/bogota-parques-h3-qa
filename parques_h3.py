"""
Parques de Bogotá en hexágonos H3: cobertura de parques por hexágono (~0.74 km²).

Datos: Sistema Distrital de Parques y Escenarios Públicos Deportivos (Datos Abiertos Bogotá, CC BY 4.0)
https://datosabiertos.bogota.gov.co/dataset/sistema-distrital-de-parques-y-escenarios-publicos-deportivos

Uso:  python parques_h3.py
Salida: salida/parques_h3.gpkg, salida/mapa_parques_h3.html, salida/mapa_parques_h3.png,
        salida/resumen.json, salida/control_calidad.csv
"""
import json
from pathlib import Path

import folium
import geopandas as gpd
import h3
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from branca.colormap import LinearColormap
from shapely.geometry import Polygon
from shapely.validation import make_valid

BASE = Path(__file__).parent
DATOS = BASE / "datos" / "parques.geojson"
SALIDA = BASE / "salida"
SALIDA.mkdir(exist_ok=True)

RES = 8                # resolución H3 (arista ~460 m)
CRS_METRICO = 3116     # MAGNA-SIRGAS / Colombia Bogotá, para áreas en m²
FUENTE = "Datos Abiertos Bogotá (SDP/IDRD), CC BY 4.0"

# ---------------------------------------------------------------- 1. Lectura y control de calidad
g = gpd.read_file(DATOS)
qa = []


def registrar(regla, n, accion):
    qa.append({"regla": regla, "registros": int(n), "accion": accion})


registrar("Registros leídos", len(g), "-")

invalidas = ~g.geometry.is_valid
registrar("Geometrías inválidas", invalidas.sum(), "make_valid()")
g["geometry"] = g.geometry.apply(lambda geom: make_valid(geom) if not geom.is_valid else geom)

# make_valid puede devolver colecciones; nos quedamos con las partes poligonales
def solo_poligonos(geom):
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom
    partes = [p for p in getattr(geom, "geoms", []) if p.geom_type in ("Polygon", "MultiPolygon")]
    return gpd.GeoSeries(partes).union_all() if partes else geom

g["geometry"] = g.geometry.apply(solo_poligonos)

propuestos = g["Tipo_de_Parque"].str.contains("PROPUESTO", na=False)
registrar("Parques propuestos (no existentes)", propuestos.sum(), "excluidos")
g = g[~propuestos].copy()

g["Nombre_Localidad"] = g["Nombre_Localidad"].replace({"SANATFE": "SANTAFE"})
registrar("Localidad con error de digitación (SANATFE)", 1, "corregido a SANTAFE")

rural = g["Nombre_Localidad"] == "SUMAPAZ"
registrar("Parques en Sumapaz (localidad rural)", rural.sum(), "excluidos del análisis urbano")
g = g[~rural].copy()

dup = g.duplicated("Codigo_Parque", keep=False)
registrar("Códigos de parque duplicados", g["Codigo_Parque"].duplicated().sum(), "se conservan (polígonos distintos); el área se calcula sobre la unión")

vacias = g.geometry.is_empty
registrar("Geometrías vacías", vacias.sum(), "excluidas" if vacias.any() else "ninguna")
g = g[~vacias].copy()
registrar("Registros analizados", len(g), "-")
pd.DataFrame(qa).to_csv(SALIDA / "control_calidad.csv", index=False, encoding="utf-8-sig")

# ---------------------------------------------------------------- 2. Áreas en sistema métrico
gm = g.to_crs(CRS_METRICO)
gm["area_m2"] = gm.geometry.area
union = gm.geometry.union_all()  # evita doble conteo por solapes

# ---------------------------------------------------------------- 3. Malla H3 sobre el área con parques
cent = gm.geometry.centroid
cent_ll = gpd.GeoSeries(cent, crs=CRS_METRICO).to_crs(4326)
gm["h3"] = [h3.latlng_to_cell(p.y, p.x, RES) for p in cent_ll]

celdas = set(gm["h3"])
celdas |= {c for cell in list(celdas) for c in h3.grid_disk(cell, 1)}


def hex_poly(cell):
    lat_lng = h3.cell_to_boundary(cell)
    return Polygon([(lng, lat) for lat, lng in lat_lng])


hexes = gpd.GeoDataFrame(
    {"h3": sorted(celdas)},
    geometry=[hex_poly(c) for c in sorted(celdas)],
    crs=4326,
).to_crs(CRS_METRICO)
hexes["area_hex_m2"] = hexes.geometry.area

# ---------------------------------------------------------------- 4. Métricas por hexágono
hexes["area_parques_m2"] = hexes.geometry.apply(lambda hx: hx.intersection(union).area)
hexes["pct_parques"] = 100 * hexes["area_parques_m2"] / hexes["area_hex_m2"]
conteo = gm.groupby("h3").size().rename("n_parques")
hexes = hexes.merge(conteo, on="h3", how="left")
hexes["n_parques"] = hexes["n_parques"].fillna(0).astype(int)
hexes["n_bolsillo"] = hexes["h3"].map(
    gm[gm["Tipo_de_Parque"] == "PARQUE DE BOLSILLO"].groupby("h3").size()
).fillna(0).astype(int)

# Se descartan hexágonos sin ningún parque ni vecinos con parque (bordes vacíos de la malla)
hexes_con = hexes[(hexes["n_parques"] > 0) | (hexes["area_parques_m2"] > 0)].copy()
hexes_con.to_crs(4326).to_file(SALIDA / "parques_h3.gpkg", layer="hexagonos_h3_res8", driver="GPKG")

# ---------------------------------------------------------------- 5. Resumen
loc = (
    gm.groupby("Nombre_Localidad")
    .agg(n_parques=("area_m2", "size"), area_ha=("area_m2", lambda s: s.sum() / 1e4))
    .sort_values("area_ha", ascending=False)
)
tipo = gm.groupby("Tipo_de_Parque").agg(n=("area_m2", "size"), area_ha=("area_m2", lambda s: s.sum() / 1e4))
resumen = {
    "parques_analizados": int(len(gm)),
    "area_total_parques_ha": round(union.area / 1e4, 1),
    "hexagonos_h3_res8": int(len(hexes_con)),
    "area_hexagono_km2": round(float(hexes_con["area_hex_m2"].median() / 1e6), 3),
    "pct_parques_mediana_hex": round(float(hexes_con["pct_parques"].median()), 2),
    "pct_parques_p90_hex": round(float(hexes_con["pct_parques"].quantile(0.90)), 2),
    "hex_menos_1pct": int((hexes_con["pct_parques"] < 1).sum()),
    "hex_mas_10pct": int((hexes_con["pct_parques"] > 10).sum()),
    "parques_por_hex_max": int(hexes_con["n_parques"].max()),
    "bolsillo_total": int((gm["Tipo_de_Parque"] == "PARQUE DE BOLSILLO").sum()),
    "top_localidades_area_ha": loc["area_ha"].round(1).head(5).to_dict(),
    "top_localidades_n_parques": loc["n_parques"].head(5).to_dict(),
    "por_tipo": tipo.round(1).to_dict("index"),
}
(SALIDA / "resumen.json").write_text(json.dumps(resumen, ensure_ascii=False, indent=2), encoding="utf-8")

# ---------------------------------------------------------------- 6. Mapa interactivo (folium)
hx = hexes_con.to_crs(4326)
vmax = float(hx["pct_parques"].quantile(0.95))
cmap = LinearColormap(
    ["#f7fcf5", "#c7e9c0", "#74c476", "#238b45", "#00441b"], vmin=0, vmax=vmax,
    caption="% del hexágono ocupado por parques (tope = percentil 95)",
)
centro = [hx.geometry.union_all().centroid.y, hx.geometry.union_all().centroid.x]
m = folium.Map(location=centro, zoom_start=11, tiles="OpenStreetMap", control_scale=True)
folium.GeoJson(
    hx[["h3", "pct_parques", "n_parques", "n_bolsillo", "area_parques_m2", "geometry"]],
    name="Parques por hexágono H3 (res. 8)",
    style_function=lambda f: {
        "fillColor": cmap(min(f["properties"]["pct_parques"], vmax)),
        "color": "#ffffff", "weight": 0.4, "fillOpacity": 0.78,
    },
    tooltip=folium.GeoJsonTooltip(
        fields=["pct_parques", "n_parques", "n_bolsillo", "area_parques_m2"],
        aliases=["% de parques", "Parques (n)", "Parques de bolsillo (n)", "Área de parques (m²)"],
        localize=True, sticky=True,
    ),
).add_to(m)
cmap.add_to(m)
folium.LayerControl().add_to(m)
titulo = (
    '<div style="position:fixed;top:10px;left:50px;z-index:9999;background:white;padding:8px 12px;'
    'border-radius:6px;font:13px Arial;box-shadow:0 1px 4px #0004;max-width:360px">'
    "<b>Parques de Bogotá en hexágonos H3</b><br>Cobertura de parques por hexágono de ~0,74 km²<br>"
    f'<span style="color:#555">Fuente: {FUENTE}. Análisis: David A. Solano</span></div>'
)
m.get_root().html.add_child(folium.Element(titulo))
m.save(SALIDA / "mapa_parques_h3.html")

# ---------------------------------------------------------------- 7. Imagen estática para el portafolio
fig, ax = plt.subplots(figsize=(8, 10), dpi=200)
hexes_con.plot(
    column=np.minimum(hexes_con["pct_parques"], vmax), ax=ax, cmap="Greens", edgecolor="white",
    linewidth=0.2, legend=True, legend_kwds={"shrink": 0.5, "label": "% del hexágono con parques"},
)
ax.set_axis_off()
fig.suptitle("Parques de Bogotá en hexágonos H3 (res. 8)", fontsize=16, weight="bold", x=0.04, ha="left", y=0.97)
fig.text(0.04, 0.925, "Porcentaje de cada hexágono (~0,74 km²) ocupado por parques", fontsize=10, color="#444", ha="left")
fig.text(0.04, 0.03, f"{resumen['parques_analizados']:,} parques · {resumen['area_total_parques_ha']:,.0f} ha · "
         f"{resumen['hexagonos_h3_res8']} hexágonos · localidad rural de Sumapaz excluida\n"
         f"Fuente: {FUENTE}. Análisis: David A. Solano", fontsize=8, color="#444", ha="left")
fig.tight_layout(rect=(0, 0.06, 1, 0.91))
fig.savefig(SALIDA / "mapa_parques_h3.png")

print(json.dumps(resumen, ensure_ascii=False, indent=2))
print(pd.DataFrame(qa).to_string(index=False))
