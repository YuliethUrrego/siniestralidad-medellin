"""Utilidades compartidas: configuración, logging, sesión HTTP con reintentos, manifiesto."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("etl")


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def load_config(path: str | Path | None = None) -> dict:
    path = Path(path) if path else ROOT / "config.yaml"
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    for k, v in cfg["paths"].items():
        p = Path(v)
        cfg["paths"][k] = p if p.is_absolute() else ROOT / p
    for k in ("raw", "clean", "quarantine", "snapshots", "reports"):
        cfg["paths"][k].mkdir(parents=True, exist_ok=True)
    cfg["paths"]["warehouse"].parent.mkdir(parents=True, exist_ok=True)
    return cfg


def http_session(cfg: dict) -> requests.Session:
    h = cfg.get("http", {})
    s = requests.Session()
    retry = Retry(
        total=h.get("retries", 4),
        backoff_factor=h.get("backoff_s", 3),
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.headers["User-Agent"] = h.get("user_agent", "etl-movilidad-medellin/1.0")
    s.request_timeout = h.get("timeout_s", 120)  # type: ignore[attr-defined]
    return s


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Manifest:
    """Registro de qué se descargó, cuándo y con qué hash → permite extracción incremental."""

    def __init__(self, path: Path):
        self.path = path
        self.data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def get(self, key: str) -> dict:
        return self.data.get(key, {})

    def update(self, key: str, **kw) -> None:
        self.data.setdefault(key, {}).update(kw, updated_at=now_utc())
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")


class Timer:
    def __init__(self, name: str):
        self.name = name

    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        log.info("%s terminado en %.1fs", self.name, time.perf_counter() - self.t0)
