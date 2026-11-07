"""Funciones de limpieza reutilizables (texto, fechas, coordenadas, categorías).

Cada regla está pensada a partir del perfilado real de las fuentes (ver README §Ruido detectado).
"""
from __future__ import annotations

import re
import unicodedata

import numpy as np
import pandas as pd

try:
    from ftfy import fix_text as _ftfy
except ImportError:  # ftfy es opcional pero recomendado
    _ftfy = None

NULL_TOKENS = {
    "", "sin inf", "sin informacion", "sin información", "no georef", "nan", "none", "null",
    "n/a", "na", "nd", "-", "0", "in", "au", "sin dato",
}

_HEX_ESC = re.compile(r"\\x([0-9A-Fa-f]{2})")
_LIT_CR = re.compile(r"(\\r|\\n|\r|\n)+")
_SPACES = re.compile(r"\s+")


# ------------------------------------------------------------------ texto
def fix_text(s):
    """Repara mojibake (Ã© → é), escapes literales (\\xF1 → ñ), \\r pegados y espacios."""
    if s is None or (isinstance(s, float) and np.isnan(s)):
        return None
    s = str(s)
    if "\\x" in s:
        s = _HEX_ESC.sub(lambda m: bytes([int(m.group(1), 16)]).decode("latin-1"), s)
    if _ftfy and ("Ã" in s or "Â" in s or "â€" in s):
        s = _ftfy(s)
    s = _LIT_CR.sub(" ", s)
    s = _SPACES.sub(" ", s).strip()
    return s or None


def fix_text_series(sr: pd.Series) -> pd.Series:
    # se aplica sobre valores únicos → muy rápido en columnas categóricas
    uniq = sr.dropna().unique()
    mapping = {u: fix_text(u) for u in uniq}
    return sr.map(mapping)


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm_key(s) -> str | None:
    """Clave de comparación: sin tildes, minúsculas, sin dobles espacios."""
    if s is None or (isinstance(s, float) and np.isnan(s)):
        return None
    return _SPACES.sub(" ", strip_accents(str(s)).lower()).strip()


def nullify(sr: pd.Series, extra: set[str] | None = None) -> pd.Series:
    tokens = NULL_TOKENS | (extra or set())
    k = sr.map(norm_key)
    return sr.mask(k.isin(tokens) | k.isna())


def snake(col: str) -> str:
    c = strip_accents(fix_text(col) or col).lower()
    c = re.sub(r"[^a-z0-9]+", "_", c).strip("_")
    return {"ano": "anio", "a_o": "anio", "ao": "anio"}.get(c, c)


def canon(sr: pd.Series, mapping: dict[str, str], default_title=True) -> pd.Series:
    """Mapea variantes a un valor canónico usando la clave normalizada."""
    def f(v):
        k = norm_key(v)
        if k is None:
            return None
        if k in mapping:
            return mapping[k]
        return v.strip().capitalize() if default_title else v
    uniq = sr.dropna().unique()
    m = {u: f(u) for u in uniq}
    return sr.map(m)


# ------------------------------------------------------------------ números / fechas
def to_float_comma(sr: pd.Series) -> pd.Series:
    """'6,26691466' → 6.26691466 (coma decimal colombiana)."""
    s = sr.astype("string").str.strip().str.replace(",", ".", regex=False)
    return pd.to_numeric(s, errors="coerce")


def only_digits_int(sr: pd.Series) -> pd.Series:
    """'2019\\r' → 2019."""
    return pd.to_numeric(sr.astype("string").str.extract(r"(\d+)")[0], errors="coerce").astype("Int64")


def parse_datetime(fecha: pd.Series, hora: pd.Series | None = None, fmt_fecha="%d/%m/%Y") -> pd.Series:
    if hora is not None:
        h = hora.astype("string").str.strip().str.extract(r"(\d{1,2}:\d{2}(?::\d{2})?)")[0].fillna("00:00:00")
        h = h.where(h.str.count(":") == 2, h + ":00")
        return pd.to_datetime(fecha.astype("string").str.strip() + " " + h, format=f"{fmt_fecha} %H:%M:%S",
                              errors="coerce")
    return pd.to_datetime(fecha, format=fmt_fecha, errors="coerce")


def franja_horaria(h: pd.Series) -> pd.Series:
    bins = [-1, 5, 11, 17, 23]
    return pd.cut(h, bins=bins, labels=["Madrugada (0-5)", "Mañana (6-11)", "Tarde (12-17)", "Noche (18-23)"]).astype("string")


def grupo_edad(edad: pd.Series) -> pd.Series:
    bins = [-1, 9, 19, 29, 39, 49, 59, 69, 79, 200]
    labels = ["0-9", "10-19", "20-29", "30-39", "40-49", "50-59", "60-69", "70-79", "80+"]
    return pd.cut(edad, bins=bins, labels=labels).astype("string")


# ------------------------------------------------------------------ coordenadas
def in_bbox(lat: pd.Series, lon: pd.Series, bbox: dict) -> pd.Series:
    return lat.between(bbox["lat_min"], bbox["lat_max"]) & lon.between(bbox["lon_min"], bbox["lon_max"])


def is_placeholder(lat: pd.Series, lon: pd.Series, placeholders: list, tol=1e-6) -> pd.Series:
    m = pd.Series(False, index=lat.index)
    for lo, la in placeholders:
        m |= (lat.sub(la).abs() < tol) & (lon.sub(lo).abs() < tol)
    return m


def haversine_min(lat, lon, cam_lat, cam_lon, chunk=20000):
    """Distancia (m) y posición de la cámara más cercana para cada punto. Vectorizado."""
    lat = np.asarray(lat, dtype=float)
    lon = np.asarray(lon, dtype=float)
    cl = np.radians(np.asarray(cam_lat, float))[None, :]
    cg = np.radians(np.asarray(cam_lon, float))[None, :]
    dist = np.full(lat.shape, np.nan)
    idx = np.full(lat.shape, -1)
    ok = ~(np.isnan(lat) | np.isnan(lon))
    pos = np.where(ok)[0]
    for i in range(0, len(pos), chunk):
        p = pos[i:i + chunk]
        la = np.radians(lat[p])[:, None]
        lo = np.radians(lon[p])[:, None]
        a = np.sin((cl - la) / 2) ** 2 + np.cos(la) * np.cos(cl) * np.sin((cg - lo) / 2) ** 2
        d = 2 * 6_371_000 * np.arcsin(np.sqrt(a))
        j = d.argmin(axis=1)
        dist[p] = d[np.arange(len(p)), j]
        idx[p] = j
    return dist, idx


# ------------------------------------------------------------------ flags de calidad
class Flags:
    """Acumula banderas de calidad por fila → columna `flags_calidad` ('a;b;c')."""

    def __init__(self, index):
        self.s = pd.Series("", index=index, dtype="string")

    def add(self, mask: pd.Series, name: str) -> int:
        mask = mask.fillna(False).astype(bool)
        self.s = self.s.mask(mask, self.s + name + ";")
        return int(mask.sum())

    def series(self) -> pd.Series:
        return self.s.str.rstrip(";").replace("", pd.NA)
