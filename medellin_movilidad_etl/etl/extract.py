"""EXTRACT — descarga automática de todas las fuentes.

Estrategia (de más estable a menos):
  1. API JSON (DKAN de MEData / ArcGIS REST / Open-Meteo) con `requests`.
  2. Si la API de metadatos de MEData cambia, fallback a Selenium: renderiza la página
     del dataset (es una SPA en React) y lee el enlace de descarga del DOM.
  3. Si la descarga del CSV falla, fallback al datastore SQL paginado de DKAN.

Es incremental: solo vuelve a bajar un archivo si cambió su fecha `modified`,
su ETag/Content-Length o si se fuerza con --force.
"""
from __future__ import annotations

import csv
import json
import logging
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

import pandas as pd

from .common import Manifest, Timer, http_session, now_utc

log = logging.getLogger("etl.extract")


# --------------------------------------------------------------------------- MEData
class MEDataClient:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.base = cfg["medata"]["base_url"].rstrip("/")
        self.s = http_session(cfg)
        self.timeout = cfg["http"]["timeout_s"]

    def metadata(self, dataset_id: str) -> dict:
        try:
            r = self.s.get(f"{self.base}/api/dataset-fast/{dataset_id}", timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            log.warning("API de metadatos falló (%s); intento con Selenium", e)
            return self._metadata_selenium(dataset_id)

    def _metadata_selenium(self, dataset_id: str) -> dict:
        """Fallback: abre la página con Chrome headless y extrae el link .csv."""
        from selenium import webdriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        opts = webdriver.ChromeOptions()
        opts.add_argument("--headless=new")
        drv = webdriver.Chrome(options=opts)
        try:
            drv.get(f"{self.base}/dataset/{dataset_id}")
            WebDriverWait(drv, 40).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "a[href$='.csv']"))
            )
            links = [a.get_attribute("href") for a in drv.find_elements(By.CSS_SELECTOR, "a[href$='.csv']")]
            title = drv.title.replace("Dataset - ", "").replace(" - Medata", "")
        finally:
            drv.quit()
        return {
            "title": title,
            "modified": None,
            "distribution": [{"identifier": None, "data": {"format": "csv", "downloadURL": u}} for u in links],
        }

    @staticmethod
    def _csv_distribution(meta: dict) -> dict:
        for d in meta.get("distribution", []):
            data = d.get("data", {})
            if str(data.get("format", "")).lower() == "csv":
                return {"resource_id": d.get("identifier"), **data}
        raise ValueError(f"El dataset '{meta.get('title')}' no tiene distribución CSV")

    def download(self, name: str, dataset_id: str, raw_dir: Path, manifest: Manifest, force=False) -> Path:
        meta = self.metadata(dataset_id)
        dist = self._csv_distribution(meta)
        url = dist["downloadURL"].replace("http://", "https://")
        out_dir = raw_dir / name
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{name}.csv"
        (out_dir / "metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

        prev = manifest.get(f"medata:{name}")
        head = {}
        try:
            h = self.s.head(url, timeout=self.timeout, allow_redirects=True)
            head = {"etag": h.headers.get("ETag"), "length": h.headers.get("Content-Length")}
        except Exception:  # noqa: BLE001
            pass
        unchanged = (
            out.exists()
            and prev.get("modified") == meta.get("modified")
            and prev.get("url") == url
            and (not head.get("length") or prev.get("length") == head.get("length"))
        )
        if unchanged and not force:
            log.info("[%s] sin cambios en la fuente (modified=%s) → se omite descarga", name, meta.get("modified"))
            return out

        with Timer(f"[{name}] descarga {url}"):
            try:
                self._stream(url, out)
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] descarga directa falló (%s) → datastore SQL paginado", name, e)
                self._datastore_dump(dist["resource_id"], out)

        manifest.update(
            f"medata:{name}", dataset_id=dataset_id, title=meta.get("title"), url=url,
            modified=meta.get("modified"), periodicity=meta.get("accrualPeriodicity"),
            bytes=out.stat().st_size, extracted_at=now_utc(), **head,
        )
        return out

    def _stream(self, url: str, out: Path) -> None:
        tmp = out.with_suffix(".part")
        with self.s.get(url, stream=True, timeout=self.timeout) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        tmp.replace(out)

    def _datastore_dump(self, resource_id: str, out: Path, page: int = 500) -> None:
        """Descarga vía /api/1/datastore/sql con paginación LIMIT/OFFSET."""
        offset, first = 0, True
        with open(out, "w", encoding="utf-8", newline="") as f:
            w = None
            while True:
                q = f"[SELECT * FROM {resource_id}][LIMIT {page} OFFSET {offset}];"
                r = self.s.get(f"{self.base}/api/1/datastore/sql/?query={quote(q)}&show-db-columns",
                               timeout=self.timeout)
                r.raise_for_status()
                rows = r.json()
                if not rows:
                    break
                if first:
                    w = csv.DictWriter(f, fieldnames=[c for c in rows[0] if c != "record_number"],
                                       extrasaction="ignore")
                    w.writeheader()
                    first = False
                w.writerows(rows)
                offset += page
                if offset % 50_000 == 0:
                    log.info("  datastore: %s filas", offset)


# --------------------------------------------------------------------------- ArcGIS
class ArcGISClient:
    def __init__(self, cfg: dict):
        self.base = cfg["arcgis"]["base_url"].rstrip("/")
        self.s = http_session(cfg)
        self.timeout = cfg["http"]["timeout_s"]

    def layer_geojson(self, path: str, simplify: float | None = None, page: int = 1000) -> dict:
        feats, offset = [], 0
        while True:
            params = {
                "where": "1=1", "outFields": "*", "outSR": 4326, "f": "geojson",
                "resultOffset": offset, "resultRecordCount": page,
            }
            if simplify:
                params |= {"maxAllowableOffset": simplify, "geometryPrecision": 6}
            r = self.s.get(f"{self.base}/{path}/query", params=params, timeout=self.timeout)
            r.raise_for_status()
            gj = r.json()
            batch = gj.get("features", [])
            feats += batch
            if len(batch) < page and not gj.get("exceededTransferLimit"):
                break
            offset += page
        return {"type": "FeatureCollection", "features": feats}

    def download(self, name: str, spec: dict, raw_dir: Path, manifest: Manifest) -> Path:
        out = raw_dir / "arcgis" / f"{name}.geojson"
        out.parent.mkdir(parents=True, exist_ok=True)
        with Timer(f"[{name}] ArcGIS {spec['path']}"):
            gj = self.layer_geojson(spec["path"], spec.get("simplify"))
        out.write_text(json.dumps(gj, ensure_ascii=False), encoding="utf-8")
        manifest.update(f"arcgis:{name}", path=spec["path"], features=len(gj["features"]), extracted_at=now_utc())
        log.info("[%s] %d elementos", name, len(gj["features"]))
        return out


# --------------------------------------------------------------------------- Clima
def extract_clima(cfg: dict, raw_dir: Path, manifest: Manifest, force=False) -> Path:
    """Lluvia/temperatura horaria (Open-Meteo ERA5). Incremental por año."""
    c = cfg["clima"]
    s = http_session(cfg)
    out_dir = raw_dir / "clima"
    out_dir.mkdir(parents=True, exist_ok=True)
    start = date.fromisoformat(c["start"])
    end = date.today() - timedelta(days=6)  # el archivo ERA5 tiene ~5 días de rezago
    for year in range(start.year, end.year + 1):
        f = out_dir / f"clima_{year}.csv"
        closed_year = year < end.year
        if f.exists() and closed_year and not force:
            continue
        d0 = max(start, date(year, 1, 1))
        d1 = min(end, date(year, 12, 31))
        r = s.get(c["url"], params={
            "latitude": c["lat"], "longitude": c["lon"], "start_date": d0.isoformat(),
            "end_date": d1.isoformat(), "hourly": ",".join(c["hourly"]), "timezone": c["timezone"],
        }, timeout=cfg["http"]["timeout_s"])
        r.raise_for_status()
        pd.DataFrame(r.json()["hourly"]).to_csv(f, index=False)
        log.info("[clima] %s descargado (%s → %s)", year, d0, d1)
    manifest.update("clima", lat=c["lat"], lon=c["lon"], hasta=end.isoformat(), extracted_at=now_utc())
    return out_dir


# --------------------------------------------------------------------------- Orquestación
def run_extract(cfg: dict, only: list[str] | None = None, force=False) -> dict:
    raw = cfg["paths"]["raw"]
    manifest = Manifest(raw / "_manifest.json")
    results = {}

    md = MEDataClient(cfg)
    for name, spec in cfg["medata"]["datasets"].items():
        if not spec.get("enabled", True) or (only and name not in only):
            continue
        try:
            results[name] = md.download(name, spec["id"], raw, manifest, force=force)
        except Exception as e:  # noqa: BLE001
            log.error("[%s] ERROR en extracción: %s", name, e)

    gis = ArcGISClient(cfg)
    for name, spec in cfg["arcgis"]["layers"].items():
        if only and name not in only and "arcgis" not in only:
            continue
        try:
            results[name] = gis.download(name, spec, raw, manifest)
        except Exception as e:  # noqa: BLE001
            log.error("[%s] ERROR en extracción ArcGIS: %s", name, e)

    if not only or "clima" in only:
        try:
            results["clima"] = extract_clima(cfg, raw, manifest, force=force)
        except Exception as e:  # noqa: BLE001
            log.error("[clima] ERROR: %s", e)
    return results
