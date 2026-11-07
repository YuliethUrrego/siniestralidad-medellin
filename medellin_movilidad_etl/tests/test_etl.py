"""Pruebas de las reglas de limpieza con muestras REALES de MEData / GeoMedellín (tests/fixtures)."""
import shutil
from pathlib import Path

import pandas as pd
import pytest

from etl import cleaning as C
from etl.common import load_config
from etl.load import run_load
from etl.transform import run_transform

FIX = Path(__file__).parent / "fixtures"


# ------------------------------------------------------------------ unitarias
@pytest.mark.parametrize("raw,clean", [
    ("MoscÃº No. 2", "Moscú No. 2"),          # mojibake UTF-8 leído como latin-1
    ("Solo da\\xF1os", "Solo daños"),         # escape literal
    ("Bel\\xE9n", "Belén"),
    ("  Choque  ", "Choque"),
    ("2019\\r", "2019"),                      # \r literal pegado al año
])
def test_fix_text(raw, clean):
    assert C.fix_text(raw) == clean


def test_coma_decimal():
    s = C.to_float_comma(pd.Series(["6,26691466", "-75,5590994", "", None]))
    assert s.iloc[0] == pytest.approx(6.26691466) and s.iloc[1] == pytest.approx(-75.5590994)
    assert s.iloc[2:].isna().all()


def test_nullify_y_canon():
    s = C.nullify(pd.Series(["Sin Inf", "Sin inf", "No Georef", "Castilla", "0"]))
    assert s.isna().sum() == 4
    c = C.canon(pd.Series(["Caida Ocupante", "Caída de Ocupante", "caida de ocupante"]),
                {"caida ocupante": "Caída de ocupante", "caida de ocupante": "Caída de ocupante"})
    assert c.nunique() == 1


def test_haversine():
    d, j = C.haversine_min([6.2442], [-75.5812], [6.2442, 6.2532], [-75.5812, -75.5812])
    assert d[0] == pytest.approx(0, abs=0.5) and j[0] == 0


# ------------------------------------------------------------------ extremo a extremo
@pytest.fixture(scope="module")
def pipeline(tmp_path_factory):
    root = tmp_path_factory.mktemp("etl")
    cfg_src = Path(__file__).parents[1] / "config.yaml"
    txt = cfg_src.read_text(encoding="utf-8").replace("data/", f"{root.as_posix()}/data/")
    (root / "config.yaml").write_text(txt, encoding="utf-8")
    cfg = load_config(root / "config.yaml")
    raw = cfg["paths"]["raw"]
    for n in ("victimas", "incidentes"):
        (raw / n).mkdir(parents=True, exist_ok=True)
        shutil.copy(FIX / f"{n}_muestra.csv", raw / n / f"{n}.csv")
    (raw / "arcgis").mkdir(exist_ok=True)
    for g in FIX.glob("*.geojson"):
        shutil.copy(g, raw / "arcgis" / g.name)
    tables, rep = run_transform(cfg, only=["victimas", "incidentes"])
    run_load(cfg, tables, rep)
    return cfg, tables, rep.data


def test_victimas_sin_ruido(pipeline):
    _, t, rep = pipeline
    v = t["fact_victima"]
    assert rep["victimas"]["filas_vacias_eliminadas"] > 0
    assert rep["victimas"]["duplicados_exactos"] > 0
    assert not v.duplicated().any()
    assert v["fecha_hora"].notna().all()
    assert set(v["sexo"].dropna()) <= {"M", "F"}
    assert v["edad"].dropna().between(1, 99).all()
    assert set(v["gravedad_victima"]) <= {"Herido", "Muerto"}
    assert v["condicion"].dropna().isin(["Motociclista", "Acompañante de moto", "Peatón", "Pasajero",
                                         "Conductor", "Ciclista"]).all()
    assert not v["comuna"].dropna().str.contains("Ã|\\\\x").any()
    # toda coordenada que sobrevive está dentro de Medellín
    ok = v.dropna(subset=["latitud"])
    assert ok["codigo_comuna_geo"].notna().all()


def test_incidentes_sin_ruido(pipeline):
    _, t, rep = pipeline
    i = t["fact_incidente"]
    assert rep["incidentes"]["coord_comodin_anulada"] > 0          # el punto -75.7037, 6.2214
    assert not ((i["longitud"].round(4) == -75.7038) & (i["latitud"].round(4) == 6.2214)).any()
    assert set(i["gravedad_incidente"]) <= {"Con heridos", "Con muertos", "Solo daños"}
    assert i["clase_incidente"].isin(["Choque", "Atropello", "Volcamiento", "Incendio", "Otro",
                                      "Caída de ocupante"]).all()
    assert i["anio"].between(2014, 2030).all()


def test_camaras(pipeline):
    _, t, _ = pipeline
    c = t["dim_camara"]
    assert set(c["tipo_camara"]) == {"CCTV", "FDT", "ARS"}
    assert c["id_camara"].is_unique
    assert c["url_snapshot"].dropna().str.match(r"https://www\.medellin\.gov\.co/simm/camaras-cctv/imagen\d+\.jpg").all()


def test_bodega(pipeline):
    import duckdb
    cfg, _, _ = pipeline
    con = duckdb.connect(str(cfg["paths"]["warehouse"]), read_only=True)
    tablas = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    assert {"fact_victima", "fact_incidente", "dim_camara", "dim_comuna", "dim_fecha",
            "v_victimas_comuna_anio", "v_cobertura_camaras"} <= tablas
    assert con.execute("SELECT COUNT(*) FROM v_incidente_con_victimas").fetchone()[0] > 0


def test_siniestralidad_2019_2025(pipeline):
    """Capa GeoMedellín VM_Accidentes: 2 registros reales + 4 casos de ruido sintéticos."""
    cfg, t, rep = pipeline
    s = t["fact_siniestro"]
    r = rep["siniestralidad"]
    assert r["radicado_repetido"] == 1 and r["fecha_invalida"] == 1
    assert len(s) == 4  # 2 reales + Envigado + caída de ocupante
    # punto en Envigado con comuna declarada: se conserva, pero sin coordenada y con bandera
    env = s[s["radicado"] == "9000001"].iloc[0]
    assert pd.isna(env["latitud"]) and "punto_fuera_poligono" in env["flags_calidad"]
    assert set(s["gravedad_incidente"]) <= {"Con heridos", "Solo daños"}
    assert "Caída de ocupante" in set(s["clase_incidente"])
    assert (cfg["paths"]["clean"] / "incidentes_para_camaras.csv").exists()
