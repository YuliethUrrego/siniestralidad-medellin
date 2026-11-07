"""Utilidades geoespaciales: polígonos de comunas/barrios y asignación punto-en-polígono."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from shapely import STRtree, points
from shapely.geometry import shape


class PolygonIndex:
    def __init__(self, geojson_path: Path, code_field="codigo", name_field="nombre"):
        gj = json.loads(Path(geojson_path).read_text(encoding="utf-8"))
        self.geoms, self.codes, self.names, self.props = [], [], [], []
        for f in gj["features"]:
            if not f.get("geometry"):
                continue
            g = shape(f["geometry"])
            if not g.is_valid:
                g = g.buffer(0)
            self.geoms.append(g)
            p = f["properties"]
            code = str(p.get(code_field)).strip() if p.get(code_field) is not None else None
            name = p.get(name_field) or f"Zona sin asignar ({code})"  # polígonos SN01/SN02 sin nombre
            self.codes.append(code.zfill(2) if code and code.isdigit() else code)
            self.names.append(name.replace("Corregimiento de ", "").strip())
            self.props.append(p)
        self.tree = STRtree(self.geoms)

    def lookup(self, lat: pd.Series, lon: pd.Series) -> tuple[pd.Series, pd.Series]:
        """Devuelve (codigo, nombre) del polígono que contiene cada punto (o NA)."""
        code = np.full(len(lat), None, dtype=object)
        name = np.full(len(lat), None, dtype=object)
        ok = (~lat.isna() & ~lon.isna()).to_numpy()
        if ok.any():
            pts = points(lon.to_numpy()[ok].astype(float), lat.to_numpy()[ok].astype(float))
            pt_idx, poly_idx = self.tree.query(pts, predicate="within")
            sel = np.where(ok)[0]
            # si un punto cae en dos polígonos (borde), se queda con el primero
            first = pd.Series(poly_idx, index=pt_idx).groupby(level=0).first()
            code[sel[first.index]] = [self.codes[i] for i in first.values]
            name[sel[first.index]] = [self.names[i] for i in first.values]
        return (pd.Series(code, index=lat.index, dtype="string"),
                pd.Series(name, index=lat.index, dtype="string"))

    def frame(self) -> pd.DataFrame:
        rows = []
        for g, c, n, p in zip(self.geoms, self.codes, self.names, self.props):
            # área aproximada en km² (1° lat ≈ 110.57 km, 1° lon ≈ 110.57*cos(6.25°))
            area = g.area * 110.574 * 111.320 * np.cos(np.radians(6.25))
            rows.append({"codigo": c, "nombre": n, "tipo": "Corregimiento" if p.get("subtipo_comunacorregimiento") == 2 else "Comuna",
                         "area_km2": round(area, 3), "centroide_lat": g.centroid.y,
                         "centroide_lon": g.centroid.x, "geometry_wkt": g.wkt})
        return pd.DataFrame(rows)
