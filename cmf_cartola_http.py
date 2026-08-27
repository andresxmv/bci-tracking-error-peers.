from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urljoin

import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup

CMF_BASE = "https://www.cmfchile.cl"
CARTOLA_URL = f"{CMF_BASE}/institucional/estadisticas/fondos_cartola_diaria.php"
CAPTCHA_VALIDATE_URL = f"{CMF_BASE}/sitio/biblioteca/captcha2/captcha.php"
DOWNLOAD_URL = f"{CMF_BASE}/institucional/estadisticas/cfm_download.php"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/151 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "es-CL,es;q=0.9,en;q=0.7",
}

NUMERIC_COLS = [
    "VALOR_CUOTA", "PATRIMONIO_NETO", "REM_FIJA", "REM_VARIABLE",
    "GASTOS_AFECTOS", "GASTOS_NO_AFECTOS", "FACTOR DE AJUSTE", "FACTOR DE REPARTO",
]


class CMFCartolaError(RuntimeError):
    pass


@dataclass
class PreparedCaptcha:
    image: bytes
    start: date
    end: date


class CMFCartolaSession:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.session.headers["Referer"] = CARTOLA_URL
        self.start: date | None = None
        self.end: date | None = None

    def close(self) -> None:
        self.session.close()

    def prepare(self, end_date: date, lookback_days: int = 10) -> PreparedCaptcha:
        start_date = end_date - timedelta(days=lookback_days)
        if (end_date - start_date).days > 30:
            start_date = end_date - timedelta(days=30)
        response = self.session.get(CARTOLA_URL, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        image = soup.find("img", id="captcha_img")
        src = str(image.get("src", "")) if image is not None else ""
        if not src:
            match = re.search(r'<img[^>]+id=["\']captcha_img["\'][^>]+src=["\']([^"\']+)', response.text, re.I)
            src = match.group(1) if match else ""
        if not src:
            raise CMFCartolaError("CMF no expuso la imagen captcha de la cartola diaria.")
        img = self.session.get(urljoin(CMF_BASE, src), timeout=30)
        img.raise_for_status()
        if len(img.content) < 100:
            raise CMFCartolaError("La imagen captcha recibida desde CMF no es válida.")
        self.start, self.end = start_date, end_date
        return PreparedCaptcha(img.content, start_date, end_date)

    def submit(self, code: str) -> tuple[bytes, str]:
        if self.start is None or self.end is None:
            raise CMFCartolaError("La sesión CMF no está preparada.")
        code = (code or "").strip()
        if not code:
            raise CMFCartolaError("Ingresa el código del captcha.")
        check = self.session.post(CAPTCHA_VALIDATE_URL, data={"accion": "valida", "valor": code}, timeout=30)
        check.raise_for_status()
        if check.text.strip() != "1":
            raise CMFCartolaError("Captcha CMF rechazado. Genera uno nuevo e inténtalo otra vez.")
        payload = {
            "ffmm": "%",
            "txt_inicio": f"{self.start:%d/%m/%Y}",
            "txt_termino": f"{self.end:%d/%m/%Y}",
            "enviar": "Buscar",
            "btnConsulta": "GENERAR ARCHIVO",
            "captcha": code,
        }
        response = self.session.post(DOWNLOAD_URL, data=payload, timeout=300)
        response.raise_for_status()
        if not response.content or b"RUN_FM" not in response.content[:500]:
            raise CMFCartolaError("CMF no devolvió una cartola válida.")
        return response.content, f"cartola_todos_{self.start:%Y%m%d}_{self.end:%Y%m%d}.txt"


def parse_cartola(payload: bytes) -> pd.DataFrame:
    text = payload.decode("latin-1", errors="replace")
    frame = pd.read_csv(io.StringIO(text), sep=";", dtype=str)
    frame.columns = frame.columns.str.strip()
    if "RUN_FM" not in frame.columns or "FECHA_INF" not in frame.columns:
        raise CMFCartolaError("La cartola CMF no tiene el formato esperado.")
    for col in NUMERIC_COLS:
        if col not in frame.columns:
            frame[col] = 0.0
        frame[col] = pd.to_numeric(frame[col].astype(str).str.replace(",", ".", regex=False), errors="coerce").fillna(0.0)
    frame["RUN_FM"] = frame["RUN_FM"].astype(str).str.strip()
    frame["SERIE"] = frame.get("SERIE", "").astype(str).str.strip()
    frame["FECHA"] = pd.to_datetime(frame["FECHA_INF"], format="%Y%m%d", errors="coerce")
    frame = frame.dropna(subset=["FECHA"])
    return frame.drop_duplicates(["RUN_FM", "FECHA", "SERIE"], keep="last").sort_values(["RUN_FM", "FECHA", "SERIE"])


def gross_returns_by_run(cartola: pd.DataFrame, run: str) -> pd.Series:
    """Copia literal de la metodología del panel original.

    r_serie,t = VC_t * factor_reparto_t * factor_ajuste_t / VC_{t-1} - 1
                + (rem_fija + rem_variable + gastos_afectos + gastos_no_afectos)_t / patrimonio_{t-1}
    Luego pondera series por patrimonio del día anterior.
    """
    sub = cartola.loc[cartola["RUN_FM"] == str(run)].copy()
    if sub.empty:
        return pd.Series(dtype=float)
    sub["FEE"] = sub["REM_FIJA"] + sub["REM_VARIABLE"] + sub["GASTOS_AFECTOS"] + sub["GASTOS_NO_AFECTOS"]
    quota = sub.pivot_table(index="FECHA", columns="SERIE", values="VALOR_CUOTA")
    equity = sub.pivot_table(index="FECHA", columns="SERIE", values="PATRIMONIO_NETO")
    fees = sub.pivot_table(index="FECHA", columns="SERIE", values="FEE")
    reparto = sub.pivot_table(index="FECHA", columns="SERIE", values="FACTOR DE REPARTO").fillna(1.0).replace(0.0, 1.0)
    ajuste = sub.pivot_table(index="FECHA", columns="SERIE", values="FACTOR DE AJUSTE").fillna(1.0).replace(0.0, 1.0)
    prev_equity = equity.shift(1)
    series_ret = (quota * reparto * ajuste) / quota.shift(1) - 1.0 + fees.div(prev_equity)
    weights = prev_equity.where(series_ret.notna() & (prev_equity > 0))
    weights = weights.div(weights.sum(axis=1), axis=0)
    out = (series_ret * weights).sum(axis=1, min_count=1).dropna()
    out.index = pd.DatetimeIndex(out.index)
    return out.sort_index()


def merge_cartola_history(existing: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    if existing is None or existing.empty:
        combined = new.copy()
    else:
        combined = pd.concat([existing, new], ignore_index=True)
    return combined.drop_duplicates(["RUN_FM", "FECHA", "SERIE"], keep="last").sort_values(["RUN_FM", "FECHA", "SERIE"])
