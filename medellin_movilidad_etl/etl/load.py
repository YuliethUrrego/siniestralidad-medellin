"""LOAD — guarda la información depurada.

  * data/clean/<tabla>.parquet           → formato columnar, liviano, listo para Power BI / pandas
  * data/warehouse/movilidad_medellin.duckdb → bodega SQL local con tablas + vistas analíticas
  * data/reports/calidad_datos.{json,md} → trazabilidad de cada regla de limpieza

Para llevarlo a SQL Server / PostgreSQL basta con exportar los parquet (ver README).
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import duckdb
import pandas as pd

log = logging.getLogger("etl.load")

VIEWS = {
    # Víctimas por comuna y año con tasa de mortalidad
    "v_victimas_comuna_anio": """
        SELECT anio, codigo_comuna, comuna,
               COUNT(*) AS victimas,
               SUM(CASE WHEN gravedad_victima = 'Muerto' THEN 1 ELSE 0 END) AS muertos,
               ROUND(100.0 * SUM(CASE WHEN gravedad_victima = 'Muerto' THEN 1 ELSE 0 END) / COUNT(*), 2) AS pct_letalidad
        FROM fact_victima GROUP BY ALL ORDER BY anio, victimas DESC""",
    # Actor vial × franja horaria
    "v_victimas_actor_franja": """
        SELECT actor_vial, franja_horaria, es_dia_habil, COUNT(*) AS victimas,
               SUM(CASE WHEN gravedad_victima = 'Muerto' THEN 1 ELSE 0 END) AS muertos
        FROM fact_victima GROUP BY ALL ORDER BY victimas DESC""",
    # Puntos críticos: celdas de ~110 m con más víctimas
    "v_puntos_criticos": """
        SELECT ROUND(latitud, 3) AS lat_celda, ROUND(longitud, 3) AS lon_celda,
               ANY_VALUE(direccion_incidente) AS direccion_ejemplo, ANY_VALUE(comuna) AS comuna,
               COUNT(*) AS victimas,
               SUM(CASE WHEN gravedad_victima = 'Muerto' THEN 1 ELSE 0 END) AS muertos,
               MIN(dist_m_camara_cctv) AS dist_m_cctv, MIN(dist_m_camara_fdt) AS dist_m_fotomulta
        FROM fact_victima WHERE latitud IS NOT NULL
        GROUP BY 1, 2 HAVING COUNT(*) >= 20 ORDER BY victimas DESC""",
    # ¿Influye la lluvia? tasa de víctimas por hora lluviosa vs seca
    "v_efecto_lluvia": """
        WITH h AS (SELECT llovia, COUNT(*) AS horas FROM fact_clima_hora
                   WHERE fecha_hora BETWEEN (SELECT MIN(fecha_hora) FROM fact_victima)
                                       AND (SELECT MAX(fecha_hora) FROM fact_victima) GROUP BY 1),
             v AS (SELECT llovia, COUNT(*) AS victimas FROM fact_victima WHERE llovia IS NOT NULL GROUP BY 1)
        SELECT h.llovia, h.horas, v.victimas, ROUND(v.victimas::DOUBLE / h.horas, 3) AS victimas_por_hora
        FROM h JOIN v USING (llovia)""",
    # Cobertura de cámaras sobre la siniestralidad
    "v_cobertura_camaras": """
        SELECT anio,
               ROUND(100.0 * AVG(CASE WHEN cubierto_cctv_300m THEN 1 ELSE 0 END), 1) AS pct_victimas_a_300m_cctv,
               ROUND(100.0 * AVG(CASE WHEN cubierto_fdt_300m  THEN 1 ELSE 0 END), 1) AS pct_victimas_a_300m_fotomulta,
               ROUND(MEDIAN(dist_m_camara_cctv)) AS mediana_dist_m_cctv
        FROM fact_victima WHERE latitud IS NOT NULL GROUP BY anio ORDER BY anio""",
    # Siniestros 2019-2025 alrededor de cada cámara CCTV (radio 300 m)
    "v_camaras_cctv_siniestros": """
        SELECT c.id_camara, c.nombre, c.direccion, c.comuna, c.url_snapshot IS NOT NULL AS tiene_foto,
               COUNT(s.radicado) AS siniestros_300m,
               SUM(CASE WHEN s.gravedad_incidente = 'Con heridos' THEN 1 ELSE 0 END) AS con_heridos,
               MAX(s.fecha) AS ultimo_siniestro
        FROM dim_camara c LEFT JOIN fact_siniestro s
          ON s.id_camara_cctv_cercana = c.id_camara AND s.cubierto_cctv_300m
        WHERE c.tipo_camara = 'CCTV' GROUP BY ALL ORDER BY siniestros_300m DESC""",
    # Víctimas por incidente (une víctimas e incidentes por radicado)
    "v_incidente_con_victimas": """
        SELECT i.*, v.n_victimas, v.n_muertos
        FROM fact_incidente i
        LEFT JOIN (SELECT radicado, COUNT(*) n_victimas,
                          SUM(CASE WHEN gravedad_victima='Muerto' THEN 1 ELSE 0 END) n_muertos
                   FROM fact_victima GROUP BY radicado) v USING (radicado)""",
}


def _prep(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in df.columns:
        if df[c].dtype == object:
            # columnas mixtas (p.ej. fecha como date) → tipos consistentes para parquet
            sample = df[c].dropna().head(1)
            if not sample.empty and hasattr(sample.iloc[0], "isoformat") and not isinstance(sample.iloc[0], str):
                df[c] = pd.to_datetime(df[c])
            else:
                df[c] = df[c].astype("string")
    return df


def run_load(cfg: dict, tables: dict[str, pd.DataFrame], report) -> None:
    clean: Path = cfg["paths"]["clean"]
    db_path: Path = cfg["paths"]["warehouse"]
    con = duckdb.connect(str(db_path))
    try:
        for name, df in tables.items():
            df = _prep(df)
            pq = clean / f"{name}.parquet"
            df.to_parquet(pq, index=False)
            con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM read_parquet('{pq.as_posix()}')")
            log.info("[load] %-28s %10s filas → %s", name, f"{len(df):,}", pq.name)
        existing = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        for v, sql in VIEWS.items():
            needed = set(re.findall(r"\b((?:fact|dim)_[a-z_]+)\b", sql))
            if needed <= existing:
                con.execute(f"CREATE OR REPLACE VIEW {v} AS {sql}")
        _write_md_report(cfg, report.data, con)
    finally:
        con.close()


def _write_md_report(cfg, data: dict, con) -> None:
    lines = ["# Reporte de calidad de datos — ETL Movilidad Medellín", ""]
    for ds, kv in data.items():
        lines += [f"## {ds}", "", "| Regla / métrica | Valor |", "|---|---|"]
        for k, v in kv.items():
            v = f"{v:,}" if isinstance(v, int) else json.dumps(v, ensure_ascii=False)
            lines.append(f"| {k.replace('_', ' ')} | {v} |")
        lines.append("")
    lines += ["## Tablas en la bodega", "", "| Tabla | Filas |", "|---|---|"]
    for (t,) in con.execute("SHOW TABLES").fetchall():
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        lines.append(f"| {t} | {n:,} |")
    (cfg["paths"]["reports"] / "calidad_datos.md").write_text("\n".join(lines), encoding="utf-8")
