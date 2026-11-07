"""Pruebas de EXTRACT y del colector de cámaras sin red (respuestas simuladas)."""
import io
import json
from pathlib import Path

import pandas as pd
from PIL import Image

from etl.camaras import Collector
from etl.common import Manifest, load_config
from etl.extract import ArcGISClient, MEDataClient


class FakeResp:
    def __init__(self, status=200, content=b"", js=None, headers=None):
        self.status_code, self.content, self._js = status, content, js
        self.headers = headers or {}

    def json(self):
        return self._js

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def iter_content(self, n):
        yield self.content

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def _cfg(tmp_path):
    txt = (Path(__file__).parents[1] / "config.yaml").read_text(encoding="utf-8")
    (tmp_path / "config.yaml").write_text(txt.replace("data/", f"{tmp_path.as_posix()}/data/"), encoding="utf-8")
    return load_config(tmp_path / "config.yaml")


def test_medata_incremental(tmp_path):
    cfg = _cfg(tmp_path)
    meta = {"title": "Víctimas", "modified": "02-03-2025", "distribution": [
        {"identifier": "abc", "data": {"format": "csv",
                                       "downloadURL": "http://medata.app.medellin.gov.co/x/Mede_Victimas_inci.csv"}}]}
    calls = []

    class S:
        def get(self, url, **kw):
            calls.append(url)
            if "dataset-fast" in url:
                return FakeResp(js=meta)
            return FakeResp(content=b"Gravedad_victima\nHeridos\n")

        def head(self, url, **kw):
            return FakeResp(headers={"Content-Length": "25"})

    c = MEDataClient(cfg)
    c.s = S()
    man = Manifest(cfg["paths"]["raw"] / "_manifest.json")
    out = c.download("victimas", "1-023-25-000360", cfg["paths"]["raw"], man)
    assert out.read_text().startswith("Gravedad_victima")
    assert any(u.startswith("https://medata") and u.endswith(".csv") for u in calls)  # forzado a https
    n = len(calls)
    c.download("victimas", "1-023-25-000360", cfg["paths"]["raw"], man)  # sin cambios → no descarga
    assert len(calls) == n + 1  # solo la consulta de metadatos


def test_arcgis_paginacion(tmp_path):
    cfg = _cfg(tmp_path)
    pages = [{"features": [{"properties": {"i": i}} for i in range(1000)], "exceededTransferLimit": True},
             {"features": [{"properties": {"i": 1000}}]}]

    class S:
        def get(self, url, params=None, **kw):
            return FakeResp(js=pages.pop(0))

    g = ArcGISClient(cfg)
    g.s = S()
    assert len(g.layer_geojson("x/MapServer/2")["features"]) == 1001


def test_colector_camaras(tmp_path):
    cfg = _cfg(tmp_path)
    buf = io.BytesIO()
    Image.effect_noise((1280, 720), 60).convert("RGB").save(buf, "JPEG", quality=90)
    jpg = buf.getvalue()
    pd.DataFrame({"id_camara": ["CCTV-8", "CCTV-9", "CCTV-10"], "tipo_camara": "CCTV", "nombre": "x",
                  "url_snapshot": [f"https://www.medellin.gov.co/simm/camaras-cctv/imagen{n}.jpg" for n in (8, 9, 10)]}
                 ).to_parquet(cfg["paths"]["clean"] / "dim_camara.parquet")

    class S:
        headers = {}

        def get(self, url, **kw):
            if "imagen9" in url:
                return FakeResp(content=b"x" * 500, headers={"Content-Type": "image/jpeg"})  # sin señal
            if "imagen10" in url:
                return FakeResp(status=404, headers={"Content-Type": "text/html"})
            return FakeResp(content=jpg, headers={"Content-Type": "image/jpeg"})

    col = Collector(cfg)
    col.s = S()
    r1 = col.ronda().set_index("id_camara")["estado"]
    assert r1.to_dict() == {"CCTV-8": "ok", "CCTV-9": "sin_senal", "CCTV-10": "error"}
    r2 = col.ronda().set_index("id_camara")["estado"]
    assert r2["CCTV-8"] == "congelada"                       # misma imagen que la ronda anterior
    assert list((cfg["paths"]["snapshots"]).rglob("*.jpg"))
