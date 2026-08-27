from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import pandas as pd

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/runtime_data"))
LATEST_PATH = DATA_DIR / "latest_quota.csv"
HISTORY_PATH = DATA_DIR / "quota_history.csv"
STATUS_PATH = DATA_DIR / "quota_status.json"


def _slug(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip().lower()).strip("_")
    return text


def normalize_run(value: object) -> str:
    return str(value).strip().upper().replace(".", "").replace(" ", "")


def _looks_like_header(values: list[object]) -> bool:
    slugs = [_slug(v) for v in values]
    has_run = any(s == "run" or "run_fondo" in s or "rut_fondo" in s for s in slugs)
    has_quota = any("valor_cuota" in s or "valor_de_la_cuota" in s or s == "cuota" for s in slugs)
    has_date = any(s == "fecha" or "fecha_cuota" in s or "fecha_valor_cuota" in s for s in slugs)
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

    # Algunos endpoints históricos entregan HTML con extensión .xls.
    prefix = payload[:500].lstrip().lower()
    if prefix.startswith(b"<html") or prefix.startswith(b"<!doctype") or b"<table" in prefix:
        tables = pd.read_html(BytesIO(payload))
        best = max(tables, key=lambda x: x.shape[0], default=pd.DataFrame())
        return _promote_header(best)

    for sep in (";", ",", "\t"):
        for encoding in ("utf-8-sig", "latin-1"):
            try:
                candidate = pd.read_csv(BytesIO(payload), sep=sep, encoding=encoding, header=None)
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


def parse_quota_file(payload: bytes, filename: str) -> pd.DataFrame:
    raw = _read_any(payload, filename)
    if raw.empty:
        raise ValueError("El archivo está vacío.")
    raw = raw.rename(columns={c: _slug(c) for c in raw.columns})
    cols = list(raw.columns)
    date_col = _find_column(cols, ("fecha", "fecha_cuota", "fechacuota", "fecha_valor_cuota"))
    run_col = _find_column(cols, ("run", "run_fondo", "runfondo", "rut_fondo"))
    quota_col = _find_column(cols, ("valor_cuota", "valorcuota", "val_cuota", "cuota", "valor_de_la_cuota"))
    fund_col = _find_column(cols, ("fondo", "nombre_fondo", "nombre", "denominacion"))
    series_col = _find_column(cols, ("serie", "nombre_serie"))
    missing = [label for label, col in (("fecha", date_col), ("RUN", run_col), ("valor cuota", quota_col)) if col is None]
    if missing:
        raise ValueError("No pude identificar las columnas: " + ", ".join(missing) + ".")

    out = pd.DataFrame()
    out["fecha"] = pd.to_datetime(raw[date_col], errors="coerce", dayfirst=True).dt.normalize()
    out["run"] = raw[run_col].map(normalize_run)
    quota_text = raw[quota_col].astype(str).str.strip()
    quota_text = quota_text.str.replace(r"\.(?=\d{3}(?:\D|$))", "", regex=True).str.replace(",", ".", regex=False)
    out["valor_cuota"] = pd.to_numeric(quota_text, errors="coerce")
    out["fondo"] = raw[fund_col].astype(str).str.strip() if fund_col else ""
    out["serie"] = raw[series_col].astype(str).str.strip() if series_col else ""
    out = out.dropna(subset=["fecha", "run", "valor_cuota"])
    out = out[(out["run"] != "") & (out["valor_cuota"] > 0)]
    out = out.sort_values(["fecha", "run", "serie"]).drop_duplicates(["fecha", "run", "serie"], keep="last")
    if out.empty:
        raise ValueError("No quedaron registros válidos después de limpiar fecha, RUN y valor cuota.")
    return out.reset_index(drop=True)


def validate_quota_file(frame: pd.DataFrame, expected_runs: set[str]) -> dict:
    latest_date = pd.Timestamp(frame["fecha"].max())
    latest = frame[frame["fecha"] == latest_date].copy()
    present = set(latest["run"].astype(str))
    matched = sorted(expected_runs & present)
    missing = sorted(expected_runs - present)
    coverage = len(matched) / max(len(expected_runs), 1)
    duplicate_rows = int(frame.duplicated(["fecha", "run", "serie"]).sum())
    invalid_quota = int((frame["valor_cuota"] <= 0).sum())
    return {"latest_date": latest_date.strftime("%Y-%m-%d"), "rows": int(len(frame)), "latest_rows": int(len(latest)), "matched_runs": len(matched), "expected_runs": len(expected_runs), "coverage": coverage, "missing_runs": missing, "duplicate_rows": duplicate_rows, "invalid_quota": invalid_quota, "ok": coverage >= 0.80 and duplicate_rows == 0 and invalid_quota == 0}


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
    combined = combined.sort_values(["fecha", "run", "serie"]).drop_duplicates(["fecha", "run", "serie"], keep="last")
    history_tmp = HISTORY_PATH.with_suffix(".tmp")
    combined.to_csv(history_tmp, index=False)
    history_tmp.replace(HISTORY_PATH)
    status = {**validation, "source_filename": source_filename, "saved_at_utc": datetime.now(timezone.utc).isoformat(), "history_rows": int(len(combined))}
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


def load_latest_quota() -> pd.DataFrame:
    if not LATEST_PATH.exists():
        return pd.DataFrame(columns=["fecha", "run", "valor_cuota", "fondo", "serie"])
    return pd.read_csv(LATEST_PATH, parse_dates=["fecha"])
