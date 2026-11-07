"""CLI del ETL.

  python -m etl run                     # extract + transform + load (todo)
  python -m etl run --only victimas comunas camaras_cctv
  python -m etl extract [--force]
  python -m etl transform               # re-procesa desde data/raw sin descargar
  python -m etl camaras --rondas 1      # una captura de las 81 cámaras CCTV
  python -m etl camaras                 # captura continua cada `intervalo_s`
"""
from __future__ import annotations

import argparse
import logging

from .common import Timer, load_config, setup_logging

log = logging.getLogger("etl")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="etl", description="ETL Movilidad Medellín")
    ap.add_argument("comando", choices=["run", "extract", "transform", "camaras"])
    ap.add_argument("--only", nargs="*", help="nombres de fuentes (ver config.yaml)")
    ap.add_argument("--force", action="store_true", help="re-descargar aunque no haya cambios")
    ap.add_argument("--rondas", type=int, default=None, help="n.º de rondas de captura de cámaras")
    ap.add_argument("--config", default=None)
    ap.add_argument("--log", default="INFO")
    a = ap.parse_args(argv)
    setup_logging(a.log)
    cfg = load_config(a.config)

    if a.comando in ("run", "extract"):
        from .extract import run_extract
        with Timer("EXTRACT"):
            run_extract(cfg, a.only, a.force)
    if a.comando in ("run", "transform"):
        from .load import run_load
        from .transform import run_transform
        with Timer("TRANSFORM"):
            tables, rep = run_transform(cfg, a.only)
        with Timer("LOAD"):
            run_load(cfg, tables, rep)
        log.info("Reporte de calidad: %s", cfg["paths"]["reports"] / "calidad_datos.md")
    if a.comando == "camaras":
        from .camaras import Collector
        Collector(cfg).loop(a.rondas)


if __name__ == "__main__":
    main()
