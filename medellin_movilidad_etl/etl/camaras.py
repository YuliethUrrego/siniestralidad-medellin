"""Colector de cámaras de circuito cerrado (CCTV) del SIMM — Secretaría de Movilidad.

Cada cámara publica un JPG 1280x720 que se refresca periódicamente:
    https://www.medellin.gov.co/simm/camaras-cctv/imagen{N}.jpg
La ubicación de cada una sale de la capa ArcGIS `CCTV_Movilidad` (dim_camara).

Por ronda, para cada cámara:
  1. descarga la imagen (requests)
  2. control de calidad: HTTP, tamaño mínimo (imagen "sin señal"), imagen congelada (mismo hash
     que la ronda anterior), brillo medio (día/noche)
  3. guarda el JPG en data/camaras/snapshots/AAAA-MM-DD/<id>/HHMMSS.jpg (solo si cambió)
  4. opcional: cuenta vehículos/personas con YOLOv8 (`conteo_vehiculos: true` en config.yaml)
  5. agrega una fila a fact_snapshot_camara (parquet diario + DuckDB)
"""
from __future__ import annotations

import hashlib
import io
import logging
import time
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd
from PIL import Image, ImageStat

from .common import http_session

log = logging.getLogger("etl.camaras")
COCO_VEHICULOS = {2: "autos", 3: "motos", 5: "buses", 7: "camiones", 0: "personas", 1: "bicicletas"}


class Collector:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.c = cfg["camaras_cctv"]
        self.s = http_session(cfg)
        self.s.headers["Referer"] = "https://www.medellin.gov.co/SIMM/camaras-de-circuito-cerrado"
        self.root: Path = cfg["paths"]["snapshots"]
        self.last_hash: dict[str, str] = {}
        self.model = None
        if self.c.get("conteo_vehiculos"):
            from ultralytics import YOLO  # pip install ultralytics
            self.model = YOLO(self.c.get("yolo_model", "yolov8n.pt"))
        self.cams = self._camaras()

    def _camaras(self) -> pd.DataFrame:
        pq = self.cfg["paths"]["clean"] / "dim_camara.parquet"
        if pq.exists():
            d = pd.read_parquet(pq)
            d = d[d["tipo_camara"] == "CCTV"][["id_camara", "nombre", "url_snapshot"]].dropna(subset=["url_snapshot"])
            if not d.empty:
                d["url_snapshot"] = d["url_snapshot"].str.replace("http://", "https://", regex=False)
                return d.reset_index(drop=True)
        # respaldo si aún no se ha corrido el ETL de capas: numeración conocida 2..82 (sin la 36)
        log.warning("dim_camara no disponible: uso numeración por defecto")
        ns = [n for n in range(2, 83) if n != 36]
        return pd.DataFrame({"id_camara": [f"CCTV-{n}" for n in ns], "nombre": None,
                             "url_snapshot": [self.c["snapshot_url"].format(n=n) for n in ns]})

    def _count(self, img: Image.Image) -> dict:
        if self.model is None:
            return {}
        res = self.model.predict(img, verbose=False, conf=0.35)[0]
        out = {v: 0 for v in COCO_VEHICULOS.values()}
        for cls in res.boxes.cls.tolist():
            k = COCO_VEHICULOS.get(int(cls))
            if k:
                out[k] += 1
        out["vehiculos_total"] = out["autos"] + out["motos"] + out["buses"] + out["camiones"]
        return out

    def ronda(self) -> pd.DataFrame:
        ts = datetime.now().replace(microsecond=0)
        rows = []
        for cam in self.cams.itertuples():
            row = {"id_camara": cam.id_camara, "ts_captura": ts, "http_status": None, "bytes": 0,
                   "estado": "error", "brillo": None, "es_noche": None, "ruta_imagen": None,
                   "last_modified": None}
            try:
                r = self.s.get(cam.url_snapshot, timeout=20, params={"t": int(ts.timestamp())})
                row["http_status"] = r.status_code
                row["bytes"] = len(r.content)
                row["last_modified"] = r.headers.get("Last-Modified")
                if r.status_code != 200 or not r.headers.get("Content-Type", "").startswith("image"):
                    rows.append(row)
                    continue
                if len(r.content) < self.c.get("min_bytes", 8000):
                    row["estado"] = "sin_senal"
                    rows.append(row)
                    continue
                h = hashlib.sha1(r.content).hexdigest()
                img = Image.open(io.BytesIO(r.content)).convert("RGB")
                row["brillo"] = round(ImageStat.Stat(img.convert("L")).mean[0], 1)
                row["es_noche"] = row["brillo"] < 60
                if self.last_hash.get(cam.id_camara) == h:
                    row["estado"] = "congelada"
                else:
                    row["estado"] = "ok"
                    d = self.root / ts.strftime("%Y-%m-%d") / cam.id_camara
                    d.mkdir(parents=True, exist_ok=True)
                    p = d / f"{ts:%H%M%S}.jpg"
                    p.write_bytes(r.content)
                    row["ruta_imagen"] = str(p.relative_to(self.root))
                    row |= self._count(img)
                self.last_hash[cam.id_camara] = h
            except Exception as e:  # noqa: BLE001
                log.debug("%s: %s", cam.id_camara, e)
            rows.append(row)
        df = pd.DataFrame(rows)
        resumen = df["estado"].value_counts().to_dict()
        log.info("Ronda %s → %s", ts, resumen)
        self._persist(df)
        return df

    def _persist(self, df: pd.DataFrame) -> None:
        day = df["ts_captura"].iloc[0].strftime("%Y-%m-%d")
        out = self.cfg["paths"]["clean"] / "snapshots_camaras" / f"fecha={day}"
        out.mkdir(parents=True, exist_ok=True)
        df.to_parquet(out / f"ronda_{df['ts_captura'].iloc[0]:%H%M%S}.parquet", index=False)
        con = duckdb.connect(str(self.cfg["paths"]["warehouse"]))
        try:
            glob = (self.cfg["paths"]["clean"] / "snapshots_camaras" / "*" / "*.parquet").as_posix()
            con.execute(f"CREATE OR REPLACE VIEW fact_snapshot_camara AS "
                        f"SELECT * FROM read_parquet('{glob}', union_by_name=true, hive_partitioning=true)")
        finally:
            con.close()

    def loop(self, rondas: int | None = None) -> None:
        i = 0
        while rondas is None or i < rondas:
            t0 = time.time()
            self.ronda()
            i += 1
            if rondas is not None and i >= rondas:
                break
            time.sleep(max(0, self.c["intervalo_s"] - (time.time() - t0)))
