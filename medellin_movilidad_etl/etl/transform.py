"""TRANSFORM — depuración de ruido + estandarización + enriquecimiento.

Principio: nada se borra en silencio.
  * Lo inservible (filas vacías, duplicados, fechas imposibles) va a data/quarantine con el motivo.
  * Lo corregible se corrige y queda marcado en `flags_calidad`.
  * Cada regla deja su conteo en el reporte de calidad.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from . import cleaning as C
from .geo import PolygonIndex

log = logging.getLogger("etl.transform")

CLASE_MAP = {
    "choque": "Choque", "atropello": "Atropello", "volcamiento": "Volcamiento", "incendio": "Incendio",
    "otro": "Otro", "caida ocupante": "Caída de ocupante", "caida de ocupante": "Caída de ocupante",
}
CONDICION_MAP = {
    "motociclista": "Motociclista", "peaton": "Peatón", "acompanante de motocicleta": "Acompañante de moto",
    "pasajero": "Pasajero", "conductor": "Conductor", "ciclista": "Ciclista",
}
ACTOR_VIAL = {
    "Motociclista": "Moto", "Acompañante de moto": "Moto", "Peatón": "Peatón", "Ciclista": "Bicicleta",
    "Conductor": "Vehículo", "Pasajero": "Vehículo",
}
GRAVEDAD_VICT = {"heridos": "Herido", "herido": "Herido", "muertos": "Muerto", "muerto": "Muerto"}
GRAVEDAD_INC = {"con heridos": "Con heridos", "con muertos": "Con muertos", "solo danos": "Solo daños"}
DISENO_MAP = {
    "tramo de via": "Tramo de vía", "interseccion": "Intersección", "lote o predio": "Lote o predio",
    "glorieta": "Glorieta", "paso elevado": "Paso elevado", "puente": "Puente", "ciclo ruta": "Ciclorruta",
    "paso inferior": "Paso inferior", "paso a nivel": "Paso a nivel", "tunel": "Túnel",
    "via peatonal": "Vía peatonal", "ponton": "Pontón",
}


class Report:
    def __init__(self):
        self.data: dict[str, dict] = {}

    def add(self, ds: str, key: str, val):
        self.data.setdefault(ds, {})[key] = int(val) if isinstance(val, (int, np.integer)) else val

    def save(self, path: Path):
        path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")


def _read_csv(path: Path) -> pd.DataFrame:
    for enc in ("utf-8-sig", "latin-1"):
        try:
            return pd.read_csv(path, dtype=str, encoding=enc, keep_default_na=False, na_values=[""],
                               on_bad_lines="warn", low_memory=False)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"No se pudo leer {path}")


def _quarantine(df: pd.DataFrame, mask: pd.Series, motivo: str, bucket: list) -> pd.DataFrame:
    mask = mask.fillna(False)
    if mask.any():
        q = df[mask].copy()
        q["motivo_rechazo"] = motivo
        bucket.append(q)
    return df[~mask]


def _calendar(df: pd.DataFrame, col: str, festivos: set) -> pd.DataFrame:
    dt = df[col]
    df["fecha"] = dt.dt.date
    df["anio"] = dt.dt.year.astype("Int64")
    df["mes"] = dt.dt.month.astype("Int64")
    df["hora"] = dt.dt.hour.astype("Int64")
    df["dia_semana"] = dt.dt.dayofweek.map(dict(enumerate(["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"])))
    df["es_fin_de_semana"] = dt.dt.dayofweek >= 5
    df["es_festivo"] = df["fecha"].isin(festivos)
    df["es_dia_habil"] = ~df["es_fin_de_semana"] & ~df["es_festivo"]
    df["franja_horaria"] = C.franja_horaria(df["hora"])
    df["hora_pico"] = df["es_dia_habil"] & (df["hora"].between(6, 8) | df["hora"].between(17, 19))
    return df


def _coords(df, lat, lon, cfg, flags: C.Flags, rep: Report, ds: str):
    bbox = cfg["bbox_medellin"]
    miss = lat.isna() | lon.isna()
    rep.add(ds, "sin_coordenada", flags.add(miss, "sin_coordenada"))
    # coordenadas invertidas (lat ↔ lon): se corrigen
    swapped = ~miss & ~C.in_bbox(lat, lon, bbox) & C.in_bbox(lon, lat, bbox)
    rep.add(ds, "coord_invertida_corregida", flags.add(swapped, "coord_invertida_corregida"))
    lat, lon = lat.where(~swapped, lon), lon.where(~swapped, lat)
    ph = C.is_placeholder(lat, lon, cfg.get("placeholder_coords", []))
    rep.add(ds, "coord_comodin_anulada", flags.add(ph, "coord_comodin"))
    out = ~miss & ~ph & ~C.in_bbox(lat, lon, bbox)
    rep.add(ds, "coord_fuera_medellin_anulada", flags.add(out, "coord_fuera_medellin"))
    bad = ph | out
    df["latitud"] = lat.mask(bad).round(7)
    df["longitud"] = lon.mask(bad).round(7)
    return df


def _comuna_geo(df, comunas: PolygonIndex | None, flags, rep, ds, declared_code_col="codigo_comuna",
                declared_name_col="comuna"):
    if comunas is None:
        return df
    code, name = comunas.lookup(df["latitud"], df["longitud"])
    df["codigo_comuna_geo"] = code
    df["comuna_geo"] = name
    has_pt = df["latitud"].notna()
    fuera_pol = has_pt & code.isna()
    rep.add(ds, "punto_fuera_poligono_medellin", flags.add(fuera_pol, "punto_fuera_poligono"))
    # dentro de la caja pero fuera del Distrito (Bello, Envigado, Itagüí…) → coordenada no confiable
    df.loc[fuera_pol, ["latitud", "longitud"]] = np.nan
    if declared_code_col in df:
        dec = df[declared_code_col]
        incons = dec.notna() & code.notna() & (dec != code)
        rep.add(ds, "comuna_declarada_vs_geo_distinta", flags.add(incons, "comuna_inconsistente_geo"))
        imput = dec.isna() & code.notna()
        rep.add(ds, "comuna_imputada_por_geo", flags.add(imput, "comuna_imputada_geo"))
        df[declared_code_col] = dec.fillna(code)
        df[declared_name_col] = df[declared_name_col].fillna(name)
    return df


def _camara_cercana(df, camaras: pd.DataFrame | None):
    if camaras is None or camaras.empty:
        return df
    for tipo in ("CCTV", "FDT", "ARS"):
        cams = camaras[camaras["tipo_camara"] == tipo].reset_index(drop=True)
        if cams.empty:
            continue
        d, j = C.haversine_min(df["latitud"], df["longitud"], cams["latitud"], cams["longitud"])
        t = tipo.lower()
        df[f"dist_m_camara_{t}"] = np.round(d, 1)
        df[f"id_camara_{t}_cercana"] = pd.Series(np.where(j >= 0, cams["id_camara"].to_numpy()[j.clip(0)], None),
                                                 index=df.index, dtype="string").mask(j < 0)
        df[f"cubierto_{t}_300m"] = pd.Series(d <= 300, index=df.index).where(~np.isnan(d))
    return df


def _clima_join(df, clima: pd.DataFrame | None, ts_col="fecha_hora"):
    if clima is None or clima.empty:
        return df
    key = df[ts_col].dt.floor("h")
    c = clima.set_index("fecha_hora")[["precipitacion_mm", "llovia", "temperatura_c", "humedad_rel", "visibilidad_m"]]
    joined = c.reindex(key.to_numpy())
    joined.index = df.index
    return pd.concat([df, joined], axis=1)


def _hash_id(*cols: pd.Series) -> pd.Series:
    s = cols[0].astype("string").fillna("")
    for c in cols[1:]:
        s = s + "|" + c.astype("string").fillna("")
    return s.map(lambda x: hashlib.md5(x.encode()).hexdigest()[:16])


# ============================================================ VÍCTIMAS
def transform_victimas(path: Path, cfg, ctx: dict, rep: Report):
    ds = "victimas"
    df = _read_csv(path)
    rep.add(ds, "filas_leidas", len(df))
    df.columns = [C.snake(c) for c in df.columns]
    q: list[pd.DataFrame] = []

    # 1) filas totalmente vacías (el CSV fuente viene rellenado hasta 1.048.576 filas, límite de Excel)
    empty = df.isna().all(axis=1)
    rep.add(ds, "filas_vacias_eliminadas", empty.sum())
    df = df[~empty].copy()

    # 2) texto: mojibake, escapes, espacios
    rep.add(ds, "variantes_mayus_minus_unificadas",
            int(df["sexo"].isin(["Sin inf"]).sum() + df["condicion"].isin(["Acompañante de motocicleta"]).sum()))
    for c in df.columns:
        df[c] = C.fix_text_series(df[c])

    # 3) duplicados exactos
    dup = df.duplicated(keep="first")
    rep.add(ds, "duplicados_exactos", dup.sum())
    df = _quarantine(df, dup, "duplicado_exacto", q)

    # 4) fecha-hora
    df["fecha_hora"] = C.parse_datetime(df["fecha_incidente"], df["hora_incidente"])
    bad_dt = df["fecha_hora"].isna() | (df["fecha_hora"] > pd.Timestamp.now()) | (df["fecha_hora"].dt.year < 2000)
    rep.add(ds, "fecha_invalida", bad_dt.sum())
    df = _quarantine(df, bad_dt, "fecha_invalida", q)
    flags = C.Flags(df.index)
    anio_src = C.only_digits_int(df.get("anio", pd.Series(index=df.index, dtype=str)))
    rep.add(ds, "anio_no_coincide_con_fecha", flags.add(anio_src.notna() & (anio_src != df["fecha_hora"].dt.year),
                                                        "anio_inconsistente"))
    df = _calendar(df, "fecha_hora", ctx["festivos"])

    # 5) categóricas
    df["gravedad_victima"] = C.canon(df["gravedad_victima"], GRAVEDAD_VICT)
    df["clase_incidente"] = C.canon(df["clase_incidente"], CLASE_MAP)
    df["condicion"] = C.canon(C.nullify(df["condicion"]), CONDICION_MAP)
    df["actor_vial"] = df["condicion"].map(ACTOR_VIAL).astype("string")
    sexo = C.nullify(df["sexo"]).str.upper().str[0]
    rep.add(ds, "sexo_sin_info", sexo.isna().sum())
    df["sexo"] = sexo.where(sexo.isin(["M", "F"]))

    # 6) edad: 0 y >99 se tratan como dato faltante (códigos de relleno)
    edad = pd.to_numeric(df["edad"], errors="coerce")
    bad_age = edad.notna() & ((edad <= 0) | (edad > 99))
    rep.add(ds, "edad_invalida_anulada", flags.add(bad_age, "edad_invalida"))
    rep.add(ds, "edad_sin_info", edad.isna().sum())
    df["edad"] = edad.mask(bad_age).astype("Int64")
    df["grupo_edad"] = C.grupo_edad(df["edad"])

    # 7) coordenadas (coma decimal, invertidas, comodín, fuera de Medellín)
    df = _coords(df, C.to_float_comma(df["latitud"]), C.to_float_comma(df["longitud"]), cfg, flags, rep, ds)

    # 8) comuna "04 - Aranjuez" → código + nombre ; validación espacial
    com = C.nullify(df["comuna"])
    parts = com.str.extract(r"^\s*(\d{1,2})\s*-\s*(.+)$")
    df["codigo_comuna"] = parts[0].str.zfill(2).astype("string")
    df["comuna"] = parts[1].str.replace(r"^Corregimiento de\s+", "", regex=True).astype("string")
    df["barrio"] = C.nullify(df["barrio"])
    df = _comuna_geo(df, ctx.get("comunas"), flags, rep, ds)

    # 9) enriquecimiento
    fuera = df["codigo_comuna"].isna() & flags.s.str.contains("punto_fuera_poligono")
    rep.add(ds, "fuera_de_medellin_a_cuarentena", int(fuera.sum()))
    df = _quarantine(df, fuera, "fuera_de_medellin", q)
    flags.s = flags.s[df.index]
    df = _camara_cercana(df, ctx.get("camaras"))
    df = _clima_join(df, ctx.get("clima"))
    df["radicado"] = df["radicado"].astype("string")
    df["id_victima"] = _hash_id(df["radicado"], df["fecha_hora"], df["sexo"], df["edad"], df["condicion"])
    df["flags_calidad"] = flags.series()

    keep = ["id_victima", "radicado", "fecha_hora", "fecha", "anio", "mes", "hora", "dia_semana",
            "es_fin_de_semana", "es_festivo", "es_dia_habil", "franja_horaria", "hora_pico",
            "gravedad_victima", "clase_incidente", "condicion", "actor_vial", "sexo", "edad", "grupo_edad",
            "direccion_incidente", "codigo_comuna", "comuna", "barrio", "latitud", "longitud",
            "codigo_comuna_geo", "comuna_geo"]
    extra = [c for c in df.columns if c.startswith(("dist_m_", "id_camara_", "cubierto_"))] + \
            [c for c in ("precipitacion_mm", "llovia", "temperatura_c", "humedad_rel", "visibilidad_m") if c in df]
    out = df[[c for c in keep if c in df] + extra + ["flags_calidad"]].reset_index(drop=True)
    rep.add(ds, "filas_limpias", len(out))
    rep.add(ds, "filas_con_alguna_bandera", int(out["flags_calidad"].notna().sum()))
    rep.add(ds, "rango_fechas", [str(out["fecha"].min()), str(out["fecha"].max())])
    return out, (pd.concat(q) if q else pd.DataFrame())


# ============================================================ INCIDENTES
def transform_incidentes(path: Path, cfg, ctx: dict, rep: Report):
    ds = "incidentes"
    df = _read_csv(path)
    rep.add(ds, "filas_leidas", len(df))
    df.columns = [C.snake(c) for c in df.columns]
    q: list[pd.DataFrame] = []
    empty = df.isna().all(axis=1)
    rep.add(ds, "filas_vacias_eliminadas", empty.sum())
    df = df[~empty].copy()
    rep.add(ds, "anio_con_basura_corregido", int(df["anio"].astype("string").str.contains(r"\D", na=False).sum()))
    rep.add(ds, "texto_con_escapes_hex_reparado",
            int(df.apply(lambda c: c.astype("string").str.contains(r"\\x[0-9A-Fa-f]{2}", na=False)).any(axis=1).sum()))
    for c in df.columns:
        df[c] = C.fix_text_series(df[c])

    dup = df.duplicated(keep="first")
    rep.add(ds, "duplicados_exactos", dup.sum())
    df = _quarantine(df, dup, "duplicado_exacto", q)
    dup_rad = df["nro_radicado"].notna() & df.duplicated("nro_radicado", keep="first")
    rep.add(ds, "radicado_repetido", dup_rad.sum())
    df = _quarantine(df, dup_rad, "radicado_repetido", q)

    df["fecha_hora"] = pd.to_datetime(df["fecha_accidente"], format="%d/%m/%Y %H:%M:%S", errors="coerce")
    # respaldo: la columna ISO en UTC → hora Colombia
    iso = pd.to_datetime(df.get("fecha_accidentes"), errors="coerce", utc=True)
    df["fecha_hora"] = df["fecha_hora"].fillna(iso.dt.tz_convert("America/Bogota").dt.tz_localize(None))
    bad_dt = df["fecha_hora"].isna() | (df["fecha_hora"] > pd.Timestamp.now())
    rep.add(ds, "fecha_invalida", bad_dt.sum())
    df = _quarantine(df, bad_dt, "fecha_invalida", q)
    flags = C.Flags(df.index)
    anio_src = C.only_digits_int(df["anio"])
    rep.add(ds, "anio_no_coincide_con_fecha", flags.add(anio_src != df["fecha_hora"].dt.year, "anio_inconsistente"))
    df = _calendar(df, "fecha_hora", ctx["festivos"])

    df["clase_incidente"] = C.canon(df["clase_accidente"], CLASE_MAP)
    df["gravedad_incidente"] = C.canon(df["gravedad_accidente"], GRAVEDAD_INC)
    df["diseno_via"] = C.canon(C.nullify(df["diseno"]), DISENO_MAP)

    loc = df["location"].astype("string").str.extract(r"\[\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\]")
    df = _coords(df, pd.to_numeric(loc[1], errors="coerce"), pd.to_numeric(loc[0], errors="coerce"),
                 cfg, flags, rep, ds)

    df["codigo_comuna"] = C.nullify(df["numcomuna"]).str.extract(r"(\d+)")[0].str.zfill(2).astype("string")
    df["comuna"] = C.nullify(df["comuna"]).str.replace(r"^Corregimiento de\s+", "", regex=True)
    df["comuna"] = df["comuna"].where(df["codigo_comuna"].notna())
    df["barrio"] = C.nullify(df["barrio"])
    df = _comuna_geo(df, ctx.get("comunas"), flags, rep, ds)
    fuera = df["codigo_comuna"].isna() & flags.s.str.contains("punto_fuera_poligono")
    rep.add(ds, "fuera_de_medellin_a_cuarentena", int(fuera.sum()))
    df = _quarantine(df, fuera, "fuera_de_medellin", q)
    flags.s = flags.s[df.index]
    df = _camara_cercana(df, ctx.get("camaras"))
    df = _clima_join(df, ctx.get("clima"))
    df["flags_calidad"] = flags.series()
    df = df.rename(columns={"nro_radicado": "radicado", "direccion_encasillada": "direccion_normalizada"})

    keep = ["radicado", "expediente", "cbml", "fecha_hora", "fecha", "anio", "mes", "hora", "dia_semana",
            "es_fin_de_semana", "es_festivo", "es_dia_habil", "franja_horaria", "hora_pico", "clase_incidente",
            "gravedad_incidente", "diseno_via", "direccion", "direccion_normalizada", "codigo_comuna", "comuna",
            "barrio", "latitud", "longitud", "codigo_comuna_geo", "comuna_geo"]
    extra = [c for c in df.columns if c.startswith(("dist_m_", "id_camara_", "cubierto_"))] + \
            [c for c in ("precipitacion_mm", "llovia", "temperatura_c", "humedad_rel", "visibilidad_m") if c in df]
    out = df[[c for c in keep if c in df] + extra + ["flags_calidad"]].reset_index(drop=True)
    rep.add(ds, "filas_limpias", len(out))
    rep.add(ds, "filas_con_alguna_bandera", int(out["flags_calidad"].notna().sum()))
    rep.add(ds, "rango_fechas", [str(out["fecha"].min()), str(out["fecha"].max())])
    return out, (pd.concat(q) if q else pd.DataFrame())


# ============================================================ GENÉRICO (aforos, velocidades, comparendos…)
def transform_generic(path: Path, cfg, ctx: dict, rep: Report, ds: str):
    df = _read_csv(path)
    rep.add(ds, "filas_leidas", len(df))
    df.columns = [C.snake(c) for c in df.columns]
    df = df.loc[:, ~df.columns.duplicated()]
    empty = df.isna().all(axis=1)
    rep.add(ds, "filas_vacias_eliminadas", empty.sum())
    df = df[~empty].copy()
    for c in df.columns:
        df[c] = C.fix_text_series(df[c])
    dup = df.duplicated()
    rep.add(ds, "duplicados_exactos", dup.sum())
    q = df[dup].assign(motivo_rechazo="duplicado_exacto")
    df = df[~dup]
    # números con coma decimal → float ; columnas de fecha → datetime
    for c in df.columns:
        s = df[c].dropna()
        if s.empty:
            continue
        sample = s.sample(min(len(s), 500), random_state=0).astype(str)
        if sample.str.fullmatch(r"-?\d+([.,]\d+)?").mean() > 0.95:
            df[c] = C.to_float_comma(df[c])
        elif "fecha" in c and sample.str.match(r"\d{1,4}[-/]\d{1,2}[-/]\d{1,4}").mean() > 0.9:
            df[c] = pd.to_datetime(df[c], errors="coerce", dayfirst=not sample.iloc[0][:4].isdigit())
    lat_c = next((c for c in df.columns if c in ("latitud", "lat", "coordenaday")), None)
    lon_c = next((c for c in df.columns if c in ("longitud", "lon", "coordenadax")), None)
    if lat_c and lon_c and pd.api.types.is_numeric_dtype(df[lat_c]):
        ok = C.in_bbox(df[lat_c], df[lon_c], cfg["bbox_medellin"])
        rep.add(ds, "coord_fuera_medellin_anulada", int((~ok & df[lat_c].notna()).sum()))
        df[lat_c] = df[lat_c].where(ok)
        df[lon_c] = df[lon_c].where(ok)
    rep.add(ds, "filas_limpias", len(df))
    return df.reset_index(drop=True), q


# ============================================================ DIMENSIONES
def build_dim_camaras(raw: Path, cfg, ctx, rep: Report) -> pd.DataFrame:
    def load(name):
        p = raw / "arcgis" / f"{name}.geojson"
        if not p.exists():
            return pd.DataFrame()
        gj = json.loads(p.read_text(encoding="utf-8"))
        rows = []
        for f in gj["features"]:
            pr = dict(f["properties"])
            g = f.get("geometry") or {}
            xy = g.get("coordinates") or []
            if g.get("type") == "Point" and len(xy) >= 2:
                pr["_lon"], pr["_lat"] = xy[:2]
            else:  # registros con geometría vacía (detectados 2 en ARS) → atributos lat/lon
                pr["_lon"], pr["_lat"] = pr.get("longitud"), pr.get("latitud")
            rows.append(pr)
        return pd.DataFrame(rows)

    frames = []
    cctv = load("camaras_cctv")
    if not cctv.empty:
        n = cctv["link_foto"].astype("string").str.extract(r"imagen(\d+)\.jpg")[0]
        frames.append(pd.DataFrame({
            # 11 cámaras traen link_foto vacío o roto ('imagen.jpg') → id por objectid y sin snapshot
            "id_camara": ("CCTV-" + n).fillna("CCTV-OBJ" + cctv["objectid"].astype(str)), "tipo_camara": "CCTV",
            "nombre": cctv["localizacion"], "direccion": cctv["direccion"],
            "url_snapshot": cctv["link_foto"].where(n.notna()).str.replace("http://", "https://", regex=False),
            "num_snapshot": pd.to_numeric(n, errors="coerce").astype("Int64"),
            "latitud": cctv["_lat"], "longitud": cctv["_lon"]}))
    fdt = load("camaras_fdt")
    if not fdt.empty:
        frames.append(pd.DataFrame({
            "id_camara": "FDT-" + fdt["id"].astype("Int64").astype(str), "tipo_camara": "FDT",
            "nombre": fdt["descripcion"], "direccion": fdt["direccion"], "sentido": fdt["sentido"],
            "estado": fdt["operando"], "vel_max_kmh": pd.to_numeric(fdt["vel_max_k"], errors="coerce"),
            # tipos de infracción que detecta (X = sí)
            "detecta_exceso_velocidad": fdt["ev"].astype("string").str.strip().str.upper().eq("X"),
            "detecta_pico_placa": fdt["pyp"].astype("string").str.strip().str.upper().eq("X"),
            "detecta_semaforo_rojo": fdt["sr"].astype("string").str.strip().str.upper().eq("X"),
            "detecta_soat": fdt["soat"].astype("string").str.strip().str.upper().eq("X"),
            "detecta_rtm": fdt["rtm"].astype("string").str.strip().str.upper().eq("X"),
            "latitud": fdt["_lat"], "longitud": fdt["_lon"]}))
    ars = load("camaras_ars")
    if not ars.empty:
        frames.append(pd.DataFrame({
            "id_camara": ars["name"].astype("string").str.replace("_", "-"), "tipo_camara": "ARS",
            "nombre": ars["direccion"], "direccion": ars["localizacion"],
            "latitud": ars["_lat"], "longitud": ars["_lon"]}))
    if not frames:
        return pd.DataFrame()
    cam = pd.concat(frames, ignore_index=True)
    for c in ("nombre", "direccion"):
        cam[c] = C.fix_text_series(cam[c])
    ok = C.in_bbox(cam["latitud"], cam["longitud"], cfg["bbox_medellin"])
    rep.add("camaras", "fuera_de_medellin_descartadas", int((~ok).sum()))
    cam = cam[ok]
    dup = cam.duplicated("id_camara")
    rep.add("camaras", "id_duplicado_descartado", int(dup.sum()))
    cam = cam[~dup].copy()
    if ctx.get("comunas") is not None:
        cam["codigo_comuna"], cam["comuna"] = ctx["comunas"].lookup(cam["latitud"], cam["longitud"])
    rep.add("camaras", "total_por_tipo", cam["tipo_camara"].value_counts().to_dict())
    return cam.reset_index(drop=True)


def build_dim_semaforos(raw: Path, cfg, ctx) -> pd.DataFrame:
    p = raw / "arcgis" / "cruces_semaforicos.geojson"
    if not p.exists():
        return pd.DataFrame()
    gj = json.loads(p.read_text(encoding="utf-8"))
    rows = [{**f["properties"], "longitud": f["geometry"]["coordinates"][0], "latitud": f["geometry"]["coordinates"][1]}
            for f in gj["features"] if f.get("geometry")]
    df = pd.DataFrame(rows)
    df = df[["id_cruce", "id_controlador", "direccion", "ubicacion", "cod_barrio", "latitud", "longitud"]]
    df["direccion"] = C.fix_text_series(df["direccion"])
    df = df[C.in_bbox(df["latitud"], df["longitud"], cfg["bbox_medellin"])].drop_duplicates("id_cruce")
    if ctx.get("comunas") is not None:
        df["codigo_comuna"], df["comuna"] = ctx["comunas"].lookup(df["latitud"], df["longitud"])
    return df.reset_index(drop=True)


def build_clima(raw: Path) -> pd.DataFrame:
    files = sorted((raw / "clima").glob("clima_*.csv"))
    if not files:
        return pd.DataFrame()
    df = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
    df = df.rename(columns={"time": "fecha_hora", "precipitation": "precipitacion_mm", "rain": "lluvia_mm",
                            "temperature_2m": "temperatura_c", "relative_humidity_2m": "humedad_rel",
                            "cloud_cover": "nubosidad_pct", "visibility": "visibilidad_m"})
    df["fecha_hora"] = pd.to_datetime(df["fecha_hora"])
    df = df.drop_duplicates("fecha_hora").sort_values("fecha_hora")
    for c in ("precipitacion_mm", "temperatura_c", "humedad_rel", "visibilidad_m"):
        if c not in df:
            df[c] = np.nan
    df["llovia"] = df["precipitacion_mm"] >= 0.2
    return df.reset_index(drop=True)


def build_festivos(years: range, country="CO") -> pd.DataFrame:
    import holidays
    h = holidays.country_holidays(country, years=list(years), language="es")
    return pd.DataFrame(sorted(h.items()), columns=["fecha", "festivo"])


def build_dim_fecha(start="2014-01-01", end=None, festivos: pd.DataFrame | None = None) -> pd.DataFrame:
    d = pd.DataFrame({"fecha": pd.date_range(start, end or pd.Timestamp.today().normalize(), freq="D")})
    d["anio"] = d.fecha.dt.year
    d["mes"] = d.fecha.dt.month
    d["trimestre"] = d.fecha.dt.quarter
    d["semana_iso"] = d.fecha.dt.isocalendar().week.astype(int)
    d["dia_semana"] = d.fecha.dt.dayofweek.map(dict(enumerate(["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"])))
    d["es_fin_de_semana"] = d.fecha.dt.dayofweek >= 5
    fest = festivos.set_index("fecha")["festivo"] if festivos is not None else pd.Series(dtype=str)
    d["festivo"] = d.fecha.dt.date.map(fest)
    d["es_festivo"] = d["festivo"].notna()
    # temporadas relevantes para movilidad en Medellín
    md = d.fecha.dt.month * 100 + d.fecha.dt.day
    d["temporada"] = np.select(
        [(md >= 1215) | (md <= 110), d.mes.isin([6, 7]) & (md >= 615), (d.mes == 8) & (d.fecha.dt.day <= 15)],
        ["Navidad/Fin de año", "Vacaciones mitad de año", "Feria de las Flores"], default="Normal")
    d["fecha"] = d.fecha.dt.date
    return d


# ============================================================ SINIESTRALIDAD 2019-2025 (GeoMedellín)
GRAVEDAD_SIN = {"heridos": "Con heridos", "con heridos": "Con heridos", "danos": "Solo daños",
                "solo danos": "Solo daños", "muertos": "Con muertos", "con muertos": "Con muertos"}


def transform_siniestralidad(path: Path, cfg, ctx: dict, rep: Report):
    """Capa ArcGIS transporte/VM_Accidentes/0: un punto por siniestro, 2019 → hoy."""
    ds = "siniestralidad"
    gj = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = []
    for f in gj["features"]:
        p = dict(f.get("properties") or {})
        xy = (f.get("geometry") or {}).get("coordinates") or []
        p["_lon"], p["_lat"] = (xy[0], xy[1]) if len(xy) >= 2 else (p.get("longitud"), p.get("latitud"))
        rows.append(p)
    df = pd.DataFrame(rows)
    rep.add(ds, "filas_leidas", len(df))
    q: list[pd.DataFrame] = []
    for c in ("clase_siniestro", "gravedad_siniestro", "direccion", "barrio", "nom_comuna", "num_comuna"):
        if c in df:
            df[c] = C.fix_text_series(df[c].astype("string"))
    df["radicado"] = pd.to_numeric(df["radicado"], errors="coerce").astype("Int64").astype("string")
    dup = df["radicado"].notna() & df.duplicated("radicado", keep="first")
    rep.add(ds, "radicado_repetido", dup.sum())
    df = _quarantine(df, dup, "radicado_repetido", q)
    df["fecha_hora"] = C.parse_datetime(df["fecha_siniestro"], df["hora_siniestro"], fmt_fecha="%Y-%m-%d")
    bad = df["fecha_hora"].isna() | (df["fecha_hora"] > pd.Timestamp.now())
    rep.add(ds, "fecha_invalida", bad.sum())
    df = _quarantine(df, bad, "fecha_invalida", q)
    flags = C.Flags(df.index)
    df = _calendar(df, "fecha_hora", ctx["festivos"])
    df["clase_incidente"] = C.canon(df["clase_siniestro"], CLASE_MAP)
    df["gravedad_incidente"] = C.canon(df["gravedad_siniestro"], GRAVEDAD_SIN)
    df = _coords(df, pd.to_numeric(df["_lat"], errors="coerce"), pd.to_numeric(df["_lon"], errors="coerce"),
                 cfg, flags, rep, ds)
    df["codigo_comuna"] = C.nullify(df["num_comuna"]).str.extract(r"(\d+)")[0].str.zfill(2).astype("string")
    df["comuna"] = C.nullify(df["nom_comuna"]).str.replace(r"^Corregimiento de\s+", "", regex=True)
    df["barrio"] = C.nullify(df["barrio"])
    df = _comuna_geo(df, ctx.get("comunas"), flags, rep, ds)
    fuera = df["codigo_comuna"].isna() & flags.s.str.contains("punto_fuera_poligono")
    rep.add(ds, "fuera_de_medellin_a_cuarentena", int(fuera.sum()))
    df = _quarantine(df, fuera, "fuera_de_medellin", q)
    flags.s = flags.s[df.index]
    df = _camara_cercana(df, ctx.get("camaras"))
    df = _clima_join(df, ctx.get("clima"))
    df["flags_calidad"] = flags.series()
    keep = ["radicado", "fecha_hora", "fecha", "anio", "mes", "hora", "dia_semana", "es_fin_de_semana",
            "es_festivo", "es_dia_habil", "franja_horaria", "hora_pico", "clase_incidente", "gravedad_incidente",
            "direccion", "codigo_comuna", "comuna", "barrio", "latitud", "longitud", "codigo_comuna_geo", "comuna_geo"]
    extra = [c for c in df.columns if c.startswith(("dist_m_", "id_camara_", "cubierto_"))] + \
            [c for c in ("precipitacion_mm", "llovia", "temperatura_c", "humedad_rel", "visibilidad_m") if c in df]
    out = df[[c for c in keep if c in df] + extra + ["flags_calidad"]].reset_index(drop=True)
    rep.add(ds, "filas_limpias", len(out))
    rep.add(ds, "rango_fechas", [str(out["fecha"].min()), str(out["fecha"].max())])
    return out, (pd.concat(q) if q else pd.DataFrame())


def build_incidentes_camaras(tables: dict) -> pd.DataFrame:
    """Puntos de incidentes de todas las fuentes en un CSV liviano para la consulta de cámaras de la página web."""
    parts = []
    for name, fuente in (("fact_incidente", "MEData 2014-2020"), ("fact_siniestro", "GeoMedellín 2019-2025")):
        d = tables.get(name)
        if d is None or d.empty:
            continue
        x = d.dropna(subset=["latitud", "longitud"])[["radicado", "fecha", "latitud", "longitud",
                                                      "clase_incidente", "gravedad_incidente"]].copy()
        x["fuente"] = fuente
        parts.append(x)
    if not parts:
        return pd.DataFrame()
    allp = pd.concat(parts, ignore_index=True)
    # 2019-2020 aparece en las dos fuentes: se conserva un solo registro por radicado
    return allp.drop_duplicates("radicado", keep="last").reset_index(drop=True)


# ============================================================ ORQUESTACIÓN
def run_transform(cfg: dict, only: list[str] | None = None):
    raw, clean, quar = cfg["paths"]["raw"], cfg["paths"]["clean"], cfg["paths"]["quarantine"]
    rep = Report()
    tables: dict[str, pd.DataFrame] = {}

    ctx: dict = {}
    com_path = raw / "arcgis" / "comunas.geojson"
    if com_path.exists():
        ctx["comunas"] = PolygonIndex(com_path)
        tables["dim_comuna"] = ctx["comunas"].frame()
    else:
        log.warning("No hay polígonos de comunas: se omite validación espacial")
    fest = build_festivos(range(2010, pd.Timestamp.today().year + 2), cfg["festivos"]["country"])
    ctx["festivos"] = set(fest["fecha"])
    tables["dim_fecha"] = build_dim_fecha(festivos=fest)
    cam = build_dim_camaras(raw, cfg, ctx, rep)
    if not cam.empty:
        ctx["camaras"] = cam
        tables["dim_camara"] = cam
    sem = build_dim_semaforos(raw, cfg, ctx)
    if not sem.empty:
        tables["dim_cruce_semaforico"] = sem
    clima = build_clima(raw)
    if not clima.empty:
        ctx["clima"] = clima
        tables["fact_clima_hora"] = clima

    for name, spec in cfg["medata"]["datasets"].items():
        if not spec.get("enabled", True) or (only and name not in only):
            continue
        p = raw / name / f"{name}.csv"
        if not p.exists():
            log.warning("[%s] no hay archivo crudo, ejecute primero 'extract'", name)
            continue
        log.info("[%s] transformando…", name)
        if spec["transform"] == "victimas":
            df, q = transform_victimas(p, cfg, ctx, rep)
            tables["fact_victima"] = df
        elif spec["transform"] == "incidentes":
            df, q = transform_incidentes(p, cfg, ctx, rep)
            tables["fact_incidente"] = df
        else:
            df, q = transform_generic(p, cfg, ctx, rep, name)
            tables[f"stg_{name}"] = df
        if len(q):
            q.astype(str).to_csv(quar / f"{name}_rechazados.csv", index=False, encoding="utf-8-sig")
        log.info("[%s] %s filas limpias, %s a cuarentena", name, f"{len(df):,}", f"{len(q):,}")

    sin_p = raw / "arcgis" / "siniestralidad.geojson"
    if sin_p.exists() and (not only or "siniestralidad" in only or "incidentes" in only):
        log.info("[siniestralidad] transformando…")
        df, q = transform_siniestralidad(sin_p, cfg, ctx, rep)
        tables["fact_siniestro"] = df
        if len(q):
            q.astype(str).to_csv(quar / "siniestralidad_rechazados.csv", index=False, encoding="utf-8-sig")
    inc_cam = build_incidentes_camaras(tables)
    if not inc_cam.empty:
        # archivo que se carga en la página web (Consulta de cámaras)
        inc_cam.to_csv(clean / "incidentes_para_camaras.csv", index=False, encoding="utf-8")
        if "dim_camara" in tables:
            tables["dim_camara"].to_csv(clean / "camaras.csv", index=False, encoding="utf-8")

    rep.save(cfg["paths"]["reports"] / "calidad_datos.json")
    return tables, rep
