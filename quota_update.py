from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from io import BytesIO
from functools import lru_cache
from pathlib import Path

import pandas as pd

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/runtime_data"))
LATEST_PATH = DATA_DIR / "latest_quota.csv"
HISTORY_PATH = DATA_DIR / "quota_history.csv"
GROSS_RETURNS_PATH = DATA_DIR / "gross_returns_history.csv"
STATUS_PATH = DATA_DIR / "quota_status.json"
MAX_RETURN_GAP_DAYS = 7

# Semilla versionada en el repo: retornos brutos diarios ya calculados con
# gross_fund_returns() sobre las cartolas CMF. Existe para que el dashboard
# tenga el año calendario completo aunque runtime_data se pierda al recrear el
# contenedor. Lo que el usuario carga en /actualizar siempre manda sobre ella.
SEED_RETURNS_PATH = Path(__file__).resolve().parent / "seed_gross_returns.csv.gz"

CARTOLA_NUMERIC = [
    "CUOTAS_APORTADAS",
    "CUOTAS_RESCATADAS",
    "CUOTAS_EN_CIRCULACION",
    "VALOR_CUOTA",
    "PATRIMONIO_NETO",
    "REM_FIJA",
    "REM_VARIABLE",
    "GASTOS_AFECTOS",
    "GASTOS_NO_AFECTOS",
    "FACTOR DE AJUSTE",
    "FACTOR DE REPARTO",
]


def _slug(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip().lower()).strip("_")
    return text


def normalize_run(value: object) -> str:
    """Normaliza RUN de fondo a la forma usada por la cartola CMF.

    La configuración histórica puede traer dígito verificador (ej. 8514-6),
    mientras RUN_FM en cartola viene como 8514.
    """
    text = str(value).strip().upper().replace(".", "").replace(" ", "")
    match = re.fullmatch(r"(\d+)-([0-9K])", text)
    return match.group(1) if match else text


def _looks_like_header(values: list[object]) -> bool:
    slugs = [_slug(v) for v in values]
    has_run = any(s in {"run", "run_fm", "run_fondo", "runfondo", "rut_fondo"} for s in slugs)
    has_quota = any("valor_cuota" in s or "valor_de_la_cuota" in s or s == "cuota" for s in slugs)
    has_date = any(s in {"fecha", "fecha_inf", "fecha_cuota", "fecha_valor_cuota"} for s in slugs)
    return has_run and has_quota and has_date


def _promote_header(raw: pd.DataFrame) -> pd.DataFrame:
    if raw.empty:
        return raw
    scan = min(len(raw), 30)
    for idx in range(scan):
        if _looks_like_header(raw.iloc[idx].tolist()):
            out = raw.iloc[idx + 1 :].copy()
            out.columns = [str(x).strip() for x in raw.iloc[idx].tolist()]
            return out.reset_index(drop=True)
    return raw


def _read_any(payload: bytes, filename: str) -> pd.DataFrame:
    # Cartola diaria oficial CMF: texto separado por ; con cabecera RUN_FM.
    if b"RUN_FM" in payload[:1000] and b"FECHA_INF" in payload[:1000]:
        for encoding in ("utf-8-sig", "latin-1"):
            try:
                return pd.read_csv(BytesIO(payload), sep=";", encoding=encoding, dtype=str)
            except Exception:
                pass

    name = filename.lower()
    if name.endswith((".xlsx", ".xlsm", ".xls")) or payload[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" or payload[:2] == b"PK":
        xls = pd.ExcelFile(BytesIO(payload))
        best = None
        for sheet in xls.sheet_names:
            candidate = pd.read_excel(xls, sheet_name=sheet, header=None)
            candidate = _promote_header(candidate)
            if best is None or candidate.shape[0] > best.shape[0]:
                best = candidate
        return best if best is not None else pd.DataFrame()

    prefix = payload[:500].lstrip().lower()
    if prefix.startswith(b"<html") or prefix.startswith(b"<!doctype") or b"<table" in prefix:
        tables = pd.read_html(BytesIO(payload))
        best = max(tables, key=lambda x: x.shape[0], default=pd.DataFrame())
        return _promote_header(best)

    for sep in (";", ",", "\t"):
        for encoding in ("utf-8-sig", "latin-1"):
            try:
                candidate = pd.read_csv(BytesIO(payload), sep=sep, encoding=encoding, header=None, dtype=str)
                candidate = _promote_header(candidate)
                if candidate.shape[1] >= 3:
                    return candidate
            except Exception:
                pass
    raise ValueError("No pude interpretar el archivo descargado desde CMF.")


def _find_column(columns: list[str], candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if candidate in columns:
            return candidate
    for col in columns:
        if any(candidate in col for candidate in candidates):
            return col
    return None


def _num(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series.astype(str).str.strip().str.replace(",", ".", regex=False), errors="coerce")


def parse_quota_file(payload: bytes, filename: str) -> pd.DataFrame:
    """Interpreta la cartola CMF manteniendo las columnas necesarias para retorno bruto."""
    raw = _read_any(payload, filename)
    if raw.empty:
        raise ValueError("El archivo está vacío.")

    # Ruta exacta para cartola oficial.
    upper_map = {str(c).strip().upper(): c for c in raw.columns}
    if {"RUN_FM", "FECHA_INF", "VALOR_CUOTA"}.issubset(upper_map):
        src = raw.rename(columns={v: k for k, v in upper_map.items()}).copy()
        for col in CARTOLA_NUMERIC:
            if col in src.columns:
                src[col] = _num(src[col]).fillna(0.0)
            else:
                src[col] = 0.0

        out = pd.DataFrame()
        out["fecha"] = pd.to_datetime(src["FECHA_INF"].astype(str).str.strip(), format="%Y%m%d", errors="coerce").dt.normalize()
        out["run"] = src["RUN_FM"].map(normalize_run)
        out["valor_cuota"] = src["VALOR_CUOTA"]
        out["fondo"] = src["RUN_FM"].astype(str).str.strip()
        out["serie"] = src["SERIE"].astype(str).str.strip() if "SERIE" in src.columns else ""
        out["moneda"] = src["MONEDA"].astype(str).str.strip() if "MONEDA" in src.columns else "$$"
        out["patrimonio_neto"] = src["PATRIMONIO_NETO"]
        out["rem_fija"] = src["REM_FIJA"]
        out["rem_variable"] = src["REM_VARIABLE"]
        out["gastos_afectos"] = src["GASTOS_AFECTOS"]
        out["gastos_no_afectos"] = src["GASTOS_NO_AFECTOS"]
        out["factor_ajuste"] = src["FACTOR DE AJUSTE"]
        out["factor_reparto"] = src["FACTOR DE REPARTO"]
        out = out.dropna(subset=["fecha"])
        out = out[(out["run"] != "") & (out["valor_cuota"] > 0)]
        out = out.sort_values(["fecha", "run", "serie"]).drop_duplicates(["fecha", "run", "serie"], keep="last")
        if out.empty:
            raise ValueError("La cartola CMF no contiene registros válidos.")
        return out.reset_index(drop=True)

    # Compatibilidad con archivos de valor cuota simples.
    raw = raw.rename(columns={c: _slug(c) for c in raw.columns})
    cols = list(raw.columns)
    date_col = _find_column(cols, ("fecha_inf", "fecha", "fecha_cuota", "fechacuota", "fecha_valor_cuota"))
    run_col = _find_column(cols, ("run_fm", "run", "run_fondo", "runfondo", "rut_fondo"))
    quota_col = _find_column(cols, ("valor_cuota", "valorcuota", "val_cuota", "cuota", "valor_de_la_cuota"))
    fund_col = _find_column(cols, ("fondo", "nombre_fondo", "nombre", "denominacion"))
    series_col = _find_column(cols, ("serie", "nombre_serie"))
    missing = [label for label, col in (("fecha", date_col), ("RUN", run_col), ("valor cuota", quota_col)) if col is None]
    if missing:
        raise ValueError("No pude identificar las columnas: " + ", ".join(missing) + ".")

    out = pd.DataFrame()
    raw_date = raw[date_col].astype(str).str.strip()
    parsed_yyyymmdd = pd.to_datetime(raw_date, format="%Y%m%d", errors="coerce")
    parsed_dayfirst = pd.to_datetime(raw_date, errors="coerce", dayfirst=True)
    out["fecha"] = parsed_yyyymmdd.fillna(parsed_dayfirst).dt.normalize()
    out["run"] = raw[run_col].map(normalize_run)
    quota_text = raw[quota_col].astype(str).str.strip().str.replace(r"\.(?=\d{3}(?:\D|$))", "", regex=True).str.replace(",", ".", regex=False)
    out["valor_cuota"] = pd.to_numeric(quota_text, errors="coerce")
    out["fondo"] = raw[fund_col].astype(str).str.strip() if fund_col else ""
    out["serie"] = raw[series_col].astype(str).str.strip() if series_col else ""
    out["moneda"] = "$$"
    for col in ["patrimonio_neto", "rem_fija", "rem_variable", "gastos_afectos", "gastos_no_afectos", "factor_ajuste", "factor_reparto"]:
        out[col] = 0.0
    out = out.dropna(subset=["fecha", "valor_cuota"])
    out = out[(out["run"] != "") & (out["valor_cuota"] > 0)]
    out = out.sort_values(["fecha", "run", "serie"]).drop_duplicates(["fecha", "run", "serie"], keep="last")
    if out.empty:
        raise ValueError("No quedaron registros válidos después de limpiar fecha, RUN y valor cuota.")
    return out.reset_index(drop=True)


def gross_fund_returns(cartola: pd.DataFrame) -> pd.DataFrame:
    """Replica bruta_returns_by_run del proyecto original.

    Por serie: VC ajustado por reparto/ajuste + remuneraciones y gastos sobre
    patrimonio previo. Después agrega series por patrimonio del día anterior.
    """
    required = {"fecha", "run", "serie", "valor_cuota", "patrimonio_neto", "rem_fija", "rem_variable", "gastos_afectos", "gastos_no_afectos", "factor_ajuste", "factor_reparto"}
    if not required.issubset(cartola.columns):
        return pd.DataFrame(columns=["fecha", "run", "ret_bruta", "moneda"])

    rows: list[pd.DataFrame] = []
    for run, sub in cartola.groupby("run", sort=False):
        sub = sub.copy().sort_values(["fecha", "serie"])
        sub["fee"] = sub["rem_fija"] + sub["rem_variable"] + sub["gastos_afectos"] + sub["gastos_no_afectos"]
        quota = sub.pivot_table(index="fecha", columns="serie", values="valor_cuota", aggfunc="last")
        equity = sub.pivot_table(index="fecha", columns="serie", values="patrimonio_neto", aggfunc="last")
        fees = sub.pivot_table(index="fecha", columns="serie", values="fee", aggfunc="last")
        reparto = sub.pivot_table(index="fecha", columns="serie", values="factor_reparto", aggfunc="last").fillna(1.0).replace(0.0, 1.0)
        ajuste = sub.pivot_table(index="fecha", columns="serie", values="factor_ajuste", aggfunc="last").fillna(1.0).replace(0.0, 1.0)
        prev_equity = equity.shift(1)
        series_returns = (quota * reparto * ajuste) / quota.shift(1) - 1.0 + fees.div(prev_equity)
        # Una cartola histórica y otra reciente pueden dejar semanas sin datos.
        # Nunca interpretamos ese salto como retorno de un solo día: al
        # aplicarlo sobre la serie embebida se duplicaba el tramo intermedio.
        gaps = quota.index.to_series().diff().dt.days
        valid_gap = gaps.le(MAX_RETURN_GAP_DAYS)
        series_returns = series_returns.where(valid_gap, axis=0)
        weights = prev_equity.where(series_returns.notna() & (prev_equity > 0))
        weights = weights.div(weights.sum(axis=1), axis=0)
        returns = (series_returns * weights).sum(axis=1, min_count=1).dropna()
        if returns.empty:
            continue
        currency = "$$"
        if "moneda" in sub.columns and not sub["moneda"].dropna().empty:
            modes = sub["moneda"].astype(str).str.strip().mode()
            if not modes.empty:
                currency = str(modes.iloc[0])
        rows.append(pd.DataFrame({"fecha": returns.index, "run": str(run), "ret_bruta": returns.values, "moneda": currency}))
    if not rows:
        return pd.DataFrame(columns=["fecha", "run", "ret_bruta", "moneda"])
    return pd.concat(rows, ignore_index=True).sort_values(["fecha", "run"]).reset_index(drop=True)


def validate_quota_file(frame: pd.DataFrame, expected_runs: set[str]) -> dict:
    latest_date = pd.Timestamp(frame["fecha"].max())
    latest = frame[frame["fecha"] == latest_date].copy()
    expected = {normalize_run(x) for x in expected_runs if normalize_run(x)}
    present = {normalize_run(x) for x in latest["run"].astype(str)}
    matched = sorted(expected & present)
    missing = sorted(expected - present)
    coverage = len(matched) / max(len(expected), 1)
    duplicate_rows = int(frame.duplicated(["fecha", "run", "serie"]).sum())
    invalid_quota = int((frame["valor_cuota"] <= 0).sum())
    return {
        "latest_date": latest_date.strftime("%Y-%m-%d"),
        "rows": int(len(frame)),
        "latest_rows": int(len(latest)),
        "matched_runs": len(matched),
        "expected_runs": len(expected),
        "coverage": coverage,
        "missing_runs": missing,
        "duplicate_rows": duplicate_rows,
        "invalid_quota": invalid_quota,
        "ok": coverage >= 0.80 and duplicate_rows == 0 and invalid_quota == 0,
    }


def persist_quota(frame: pd.DataFrame, source_filename: str, validation: dict) -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    latest_date = pd.Timestamp(frame["fecha"].max())
    latest = frame[frame["fecha"] == latest_date].copy()
    latest_tmp = LATEST_PATH.with_suffix(".tmp")
    latest.to_csv(latest_tmp, index=False)
    latest_tmp.replace(LATEST_PATH)

    if HISTORY_PATH.exists():
        prior = pd.read_csv(HISTORY_PATH, parse_dates=["fecha"])
        combined = pd.concat([prior, frame], ignore_index=True)
    else:
        combined = frame.copy()
    combined["fecha"] = pd.to_datetime(combined["fecha"], errors="coerce")
    combined = combined.dropna(subset=["fecha", "run", "valor_cuota"])
    combined["run"] = combined["run"].map(normalize_run)
    combined = combined.sort_values(["fecha", "run", "serie"]).drop_duplicates(["fecha", "run", "serie"], keep="last")
    history_tmp = HISTORY_PATH.with_suffix(".tmp")
    combined.to_csv(history_tmp, index=False)
    history_tmp.replace(HISTORY_PATH)

    gross = gross_fund_returns(combined)
    gross_tmp = GROSS_RETURNS_PATH.with_suffix(".tmp")
    gross.to_csv(gross_tmp, index=False)
    gross_tmp.replace(GROSS_RETURNS_PATH)

    status = {
        **validation,
        "source_filename": source_filename,
        "saved_at_utc": datetime.now(timezone.utc).isoformat(),
        "history_rows": int(len(combined)),
        "gross_return_rows": int(len(gross)),
        "methodology": "cartola_bruta_v1",
    }
    status_tmp = STATUS_PATH.with_suffix(".tmp")
    status_tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    status_tmp.replace(STATUS_PATH)
    return status


def load_status() -> dict | None:
    if not STATUS_PATH.exists():
        return None
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def quota_date_source(value: object) -> str | None:
    """Dónde está ya disponible esa fecha: "cartola", "semilla" o None.

    Sirve para no volver a pedir un captcha por un día que el sistema ya tiene.
    Se revisan el histórico y latest_quota.csv porque el primero guarda todas
    las fechas y el segundo sólo el último corte; después la semilla del repo.
    """
    target = pd.to_datetime(value, errors="coerce")
    if pd.isna(target):
        return None
    target = pd.Timestamp(target).normalize()

    for path in (HISTORY_PATH, LATEST_PATH):
        if not path.exists():
            continue
        try:
            dates = pd.read_csv(path, usecols=["fecha"], parse_dates=["fecha"])["fecha"]
        except (OSError, ValueError, KeyError):
            continue
        if dates.dt.normalize().eq(target).any():
            return "cartola"

    seed = load_seed_returns()
    if not seed.empty and "fecha" in seed.columns:
        fechas = pd.to_datetime(seed["fecha"], errors="coerce").dropna()
        if not fechas.empty and fechas.dt.normalize().eq(target).any():
            return "semilla"
    return None


def has_quota_date(value: object) -> bool:
    """True si la fecha ya está cubierta y no hace falta bajarla de nuevo."""
    return quota_date_source(value) is not None


def new_quota_rows(frame: pd.DataFrame) -> int:
    """Filas de la cartola que todavía no están en el histórico persistido.

    Si da 0, volver a guardar sólo reescribiría lo mismo. Se usa para cortar el
    flujo antes de tocar los archivos y de recomputar métricas.
    """
    if frame is None or frame.empty:
        return 0
    incoming = frame.copy()
    incoming["fecha"] = pd.to_datetime(incoming["fecha"], errors="coerce").dt.normalize()
    incoming["run"] = incoming["run"].map(normalize_run)
    incoming = incoming.dropna(subset=["fecha"]).drop_duplicates(["fecha", "run", "serie"])
    if not HISTORY_PATH.exists():
        return int(len(incoming))
    try:
        prior = pd.read_csv(HISTORY_PATH, usecols=["fecha", "run", "serie"], parse_dates=["fecha"])
    except (OSError, ValueError, KeyError):
        return int(len(incoming))
    prior["fecha"] = pd.to_datetime(prior["fecha"], errors="coerce").dt.normalize()
    prior["run"] = prior["run"].map(normalize_run)
    known = set(zip(prior["fecha"], prior["run"], prior["serie"].astype(str)))
    pairs = zip(incoming["fecha"], incoming["run"], incoming["serie"].astype(str))
    return int(sum(1 for item in pairs if item not in known))


def load_latest_quota() -> pd.DataFrame:
    if not LATEST_PATH.exists():
        return pd.DataFrame(columns=["fecha", "run", "valor_cuota", "fondo", "serie"])
    return pd.read_csv(LATEST_PATH, parse_dates=["fecha"])


def _file_signature(path: Path) -> tuple:
    try:
        stat = path.stat()
        return (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return (str(path), 0, 0)


@lru_cache(maxsize=4)
def _load_returns_cached(signature: tuple) -> pd.DataFrame:
    return _read_returns_file(Path(signature[0]))


def _read_returns_file(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=["fecha", "run", "ret_bruta", "moneda"])
    try:
        return pd.read_csv(path, parse_dates=["fecha"])
    except (OSError, ValueError):
        return pd.DataFrame(columns=["fecha", "run", "ret_bruta", "moneda"])


def load_seed_returns() -> pd.DataFrame:
    """Retornos brutos diarios de la semilla versionada."""
    return _load_returns_cached(_file_signature(SEED_RETURNS_PATH))


def seed_date_range() -> tuple[pd.Timestamp, pd.Timestamp] | None:
    seed = load_seed_returns()
    if seed.empty or "fecha" not in seed.columns:
        return None
    fechas = pd.to_datetime(seed["fecha"], errors="coerce").dropna()
    if fechas.empty:
        return None
    return pd.Timestamp(fechas.min()).normalize(), pd.Timestamp(fechas.max()).normalize()


@lru_cache(maxsize=2)
def _combined_gross_returns(seed_sig: tuple, runtime_sig: tuple) -> pd.DataFrame:
    return _build_gross_returns()


def load_gross_returns() -> pd.DataFrame:
    """Retornos brutos diarios: semilla del repo + lo cargado en runtime.

    El resultado se cachea por firma de archivo (mtime y tamaño) porque cada
    request del dashboard lo pide varias veces y releer el .csv.gz completo en
    cada llamada hacía la página inutilizable.
    """
    frame = _combined_gross_returns(
        _file_signature(SEED_RETURNS_PATH), _file_signature(GROSS_RETURNS_PATH)
    )
    return frame.copy()


def _build_gross_returns() -> pd.DataFrame:
    runtime = _read_returns_file(GROSS_RETURNS_PATH)
    seed = load_seed_returns()
    if runtime.empty and seed.empty:
        return pd.DataFrame(columns=["fecha", "run", "ret_bruta", "moneda"])
    # La semilla va primero y el runtime después: al deduplicar con keep="last"
    # gana siempre la cartola que cargó el usuario.
    frame = pd.concat([seed, runtime], ignore_index=True) if not seed.empty else runtime
    if "fecha" in frame.columns:
        frame["fecha"] = pd.to_datetime(frame["fecha"], errors="coerce").dt.normalize()
    if "run" in frame.columns:
        frame["run"] = frame["run"].map(normalize_run)
    if {"fecha", "run", "ret_bruta"}.issubset(frame.columns):
        frame["ret_bruta"] = pd.to_numeric(frame["ret_bruta"], errors="coerce")
        frame = frame.dropna(subset=["fecha", "run", "ret_bruta"])
        # El agregado bruto es único por RUN y fecha. Esto limpia históricos
        # creados antes de que persist_quota recompusiera todo el archivo y
        # evita que el mismo 31-07 se capitalice más de una vez.
        frame = (
            frame.sort_values(["fecha", "run"])
            .drop_duplicates(["fecha", "run"], keep="last")
        )
    return frame.reset_index(drop=True)
