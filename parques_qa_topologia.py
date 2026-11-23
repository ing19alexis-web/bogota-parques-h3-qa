"""
Control de calidad espacial y de atributos de la capa de parques de Bogotá.

Revisa: geometrías inválidas, solapes entre parques, polígonos diminutos, polígonos alargados (slivers),
duplicados exactos, coordenadas de atributo que no coinciden con la geometría, nulos y valores
inconsistentes. Genera un informe, capas de error para revisar en GIS y una figura resumen.

Datos: Sistema Distrital de Parques y Escenarios Públicos Deportivos (Datos Abiertos Bogotá, CC BY 4.0).
Uso:  python parques_qa_topologia.py
"""
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from shapely.validation import explain_validity

BASE = Path(__file__).parent
DATOS = BASE / "datos" / "parques.geojson"
SALIDA = BASE / "salida_qa"
SALIDA.mkdir(exist_ok=True)
CRS_M = 3116  # MAGNA-SIRGAS / Colombia Bogotá

UMBRAL_AREA_M2 = 50        # polígonos más pequeños se marcan para revisión
UMBRAL_SOLAPE_M2 = 5       # solapes menores se consideran ruido de digitalización
UMBRAL_COORD_M = 100       # distancia máxima entre Coordenada_X/Y y el centroide del polígono
UMBRAL_COMPACIDAD = 0.05   # 4*pi*A/P^2 por debajo de esto = polígono muy alargado (sliver)

g = gpd.read_file(DATOS)
gm = g.to_crs(CRS_M).copy()
gm["id_fila"] = gm.index
hallazgos = []


def anotar(regla, severidad, n, detalle):
    hallazgos.append({"regla": regla, "severidad": severidad, "registros": int(n), "detalle": detalle})


# 1. Geometrías inválidas ------------------------------------------------------
inval = gm[~gm.geometry.is_valid].copy()
inval["motivo"] = inval.geometry.apply(explain_validity)
anotar("Geometría inválida", "alta", len(inval), "; ".join(sorted(set(inval["motivo"].str.split("[").str[0]))) or "-")

# 2. Polígonos diminutos y alargados ------------------------------------------
gm["area_m2"] = gm.geometry.area
gm["perim_m"] = gm.geometry.length
gm["compacidad"] = 4 * np.pi * gm["area_m2"] / gm["perim_m"] ** 2
pequenos = gm[gm["area_m2"] < UMBRAL_AREA_M2].copy()
anotar("Polígono menor a %d m²" % UMBRAL_AREA_M2, "media", len(pequenos), "Posibles restos de digitalización o parques sin dimensión real")
slivers = gm[(gm["compacidad"] < UMBRAL_COMPACIDAD) & (gm["area_m2"] >= UMBRAL_AREA_M2)].copy()
anotar("Polígono muy alargado (compacidad < %.2f)" % UMBRAL_COMPACIDAD, "baja", len(slivers), "Posibles slivers; revisar si son corredores o errores")

# 3. Solapes entre parques ------------------------------------------------------
validas = gm[gm.geometry.is_valid]
idx = validas.sindex
filas = []
for i, geom in zip(validas.index, validas.geometry):
    for j in idx.query(geom, predicate="intersects"):
        j = validas.index[j]
        if j <= i:
            continue
        inter = geom.intersection(validas.geometry.loc[j])
        if inter.area >= UMBRAL_SOLAPE_M2:
            filas.append({"id_a": i, "id_b": j, "cod_a": validas.at[i, "Codigo_Parque"], "cod_b": validas.at[j, "Codigo_Parque"],
                          "area_solape_m2": round(inter.area, 1), "geometry": inter})
solapes = gpd.GeoDataFrame(filas, geometry="geometry", crs=CRS_M) if filas else gpd.GeoDataFrame(columns=["geometry"], crs=CRS_M)
anotar("Solape entre parques (>= %d m²)" % UMBRAL_SOLAPE_M2, "alta", len(solapes),
       f"Área total solapada: {solapes['area_solape_m2'].sum():,.0f} m²" if len(solapes) else "-")

# 4. Duplicados --------------------------------------------------------------
dup_cod = gm[gm["Codigo_Parque"].duplicated(keep=False)].sort_values("Codigo_Parque")
anotar("Código de parque duplicado", "media", len(dup_cod), ", ".join(dup_cod["Codigo_Parque"].unique()[:10]))
wkb = gm.geometry.to_wkb()
dup_geom = gm[wkb.duplicated(keep=False)]
anotar("Geometría exactamente duplicada", "alta", len(dup_geom), "-")

# 5. Coordenada de atributo vs. centroide del polígono -----------------------
pts = gpd.GeoSeries(gpd.points_from_xy(gm["Coordenada_X"], gm["Coordenada__Y"]), crs=4326).to_crs(CRS_M)
gm["dist_coord_m"] = gm.geometry.centroid.distance(pts)
lejos = gm[gm["dist_coord_m"] > UMBRAL_COORD_M].copy()
anotar("Coordenada lejos del polígono (> %d m)" % UMBRAL_COORD_M, "media", len(lejos),
       f"Mediana de distancia en toda la capa: {gm['dist_coord_m'].median():.0f} m")

# 6. Nulos y valores inconsistentes ---------------------------------------
nulos = g.drop(columns="geometry").isna().sum()
for col, n in nulos[nulos > 0].items():
    anotar(f"Nulos en {col}", "alta" if n / len(g) > 0.5 else "baja", n, f"{100 * n / len(g):.1f} % de los registros")

loc_validas = {"USAQUEN", "CHAPINERO", "SANTAFE", "SAN CRISTOBAL", "USME", "TUNJUELITO", "BOSA", "KENNEDY", "FONTIBON",
               "ENGATIVA", "SUBA", "BARRIOS UNIDOS", "TEUSAQUILLO", "MARTIRES", "ANTONIO NARIÑO", "PUENTE ARANDA",
               "CANDELARIA", "RAFAEL URIBE", "CIUDAD BOLIVAR", "SUMAPAZ"}
loc_mal = g[~g["Nombre_Localidad"].isin(loc_validas)]
anotar("Localidad fuera del catálogo oficial", "baja", len(loc_mal), ", ".join(sorted(loc_mal["Nombre_Localidad"].astype(str).unique())))

# SHAPE.AREA está en grados cuadrados, no en m²: no sirve como área real.
anotar("Campo SHAPE.AREA en grados² (no en m²)", "media", len(g),
       f"Mediana {g['SHAPE.AREA'].median():.2e}; el área real mediana es {gm['area_m2'].median():,.0f} m²")

# Informe y capas -------------------------------------------------------------
informe = pd.DataFrame(hallazgos)
informe.to_csv(SALIDA / "informe_qa.csv", index=False, encoding="utf-8-sig")
cols = ["id_fila", "Codigo_Parque", "Nombre_del_Parque", "Tipo_de_Parque", "Nombre_Localidad", "geometry"]
gpkg = SALIDA / "errores_parques.gpkg"
if gpkg.exists():
    gpkg.unlink()
for nombre, capa, extra in [
    ("geometria_invalida", inval, ["motivo"]),
    ("poligono_diminuto", pequenos, ["area_m2"]),
    ("poligono_alargado", slivers, ["compacidad"]),
    ("coordenada_lejana", lejos, ["dist_coord_m"]),
    ("codigo_duplicado", dup_cod, []),
]:
    if len(capa):
        capa[cols + extra].to_crs(4326).to_file(gpkg, layer=nombre, driver="GPKG")
if len(solapes):
    solapes.to_crs(4326).to_file(gpkg, layer="solapes", driver="GPKG")

# Figura resumen --------------------------------------------------------------
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6.5), dpi=170, gridspec_kw={"width_ratios": [1, 0.9]})
graf = informe[~informe["regla"].str.startswith(("Nulos", "Campo"))].sort_values("registros")
colores = {"alta": "#b2182b", "media": "#ef8a62", "baja": "#999999"}
ax1.barh(graf["regla"], graf["registros"], color=graf["severidad"].map(colores))
for y, v in enumerate(graf["registros"]):
    ax1.text(v, y, f" {v:,}", va="center", fontsize=8)
ax1.set_title("Hallazgos de geometría y atributos", loc="left", weight="bold")
ax1.set_xlabel("Registros afectados")
ax1.spines[["top", "right"]].set_visible(False)
ax1.tick_params(axis="y", labelsize=8)

gm.plot(ax=ax2, color="#d9d9d9", linewidth=0)
if len(solapes):
    solapes.buffer(220).plot(ax=ax2, color="#b2182b", linewidth=0, label=f"Solapes ({len(solapes)})")
if len(pequenos):
    pequenos.geometry.centroid.buffer(260).plot(ax=ax2, color="#ef8a62", linewidth=0, label=f"Polígonos < {UMBRAL_AREA_M2} m² ({len(pequenos)})")
if len(inval):
    inval.geometry.centroid.buffer(320).plot(ax=ax2, color="#2166ac", linewidth=0, label=f"Inválidos ({len(inval)})")
ax2.set_axis_off()
ax2.legend(loc="lower right", fontsize=8, frameon=False)
ax2.set_title("Dónde están los errores", loc="left", weight="bold")
fig.suptitle("Control de calidad: capa de parques de Bogotá", x=0.04, ha="left", fontsize=16, weight="bold")
fig.text(0.04, 0.01, f"{len(g):,} registros · Fuente: Datos Abiertos Bogotá, CC BY 4.0 · Análisis: David A. Solano",
         fontsize=8, color="#444")
fig.tight_layout(rect=(0, 0.03, 1, 0.94))
fig.savefig(SALIDA / "resumen_qa.png")

pd.set_option("display.width", 200, "display.max_colwidth", 90)
print(informe.to_string(index=False))
