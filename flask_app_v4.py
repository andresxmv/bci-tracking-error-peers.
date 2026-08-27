from __future__ import annotations

import math
from functools import lru_cache

import numpy as np
import pandas as pd
import requests

import flask_app_v2 as base
from quota_update import load_gross_returns, normalize_run

app = base.app
ORIGINAL_CATEGORY_LEVELS = base.category_levels
PROXY_URL = "https://nusycxhrfynrrbvdiiko.supabase.co/functions/v1/cmf-cartola-proxy"
PROXY_KEY = "bci-tracking-error-peers-v1"


@lru_cache(maxsize=8)
def _dollar_year(year: int) -> pd.Series:
    try:
        r = requests.post(
            PROXY_URL,
            json={"action": "dollar", "year": int(year)},
            headers={"x-proxy-key": PROXY_KEY},
            timeout=40,
        )
        data = r.json()
        if not r.ok or not data.get("ok"):
            return pd.Series(dtype=float)
        rows = data.get("serie") or []
        frame = pd.DataFrame(rows)
        if frame.empty or "fecha" not in frame.columns or "valor" not in frame.columns:
            return pd.Series(dtype=float)
        frame["fecha"] = pd.to_datetime(frame["fecha"], errors="coerce").dt.tz_localize(None).dt.normalize()
        frame["valor"] = pd.to_numeric(frame["valor"], errors="coerce")
        return frame.dropna().drop_duplicates("fecha", keep="last").set_index("fecha")["valor"].sort_index()
    except Exception:
        return pd.Series(dtype=float)


def _fx_returns_for(index: pd.DatetimeIndex) -> pd.Series:
    if len(index) == 0:
        return pd.Series(dtype=float)
    years = range(int(index.min().year), int(index.max().year) + 1)
    pieces = [_dollar_year(y) for y in years]
    pieces = [p for p in pieces if not p.empty]
    if not pieces:
        return pd.Series(index=index, dtype=float)
    observed = pd.concat(pieces).sort_index()
    observed = observed[~observed.index.duplicated(keep="last")]
    span_start = pd.Timestamp(index.min()) - pd.Timedelta(days=10)
    span_end = pd.Timestamp(index.max()) + pd.Timedelta(days=10)
    span = pd.date_range(span_start, span_end, freq="D")
    published = observed.reindex(span)
    # Convención del proyecto original: el dólar aplicable en T se publica el
    # siguiente día hábil. shift(-1), bfill y ffill de cola.
    rate = published.shift(-1).bfill().ffill()
    fx = rate.pct_change(fill_method=None)
    return fx.reindex(index)


def _adjust_prom_returns(frame: pd.DataFrame) -> pd.Series:
    ret = frame.set_index("fecha")["ret_bruta"].astype(float).sort_index()
    if frame["moneda"].astype(str).str.upper().eq("PROM").any():
        fx = _fx_returns_for(ret.index)
        aligned = pd.concat([ret.rename("fund"), fx.rename("fx")], axis=1).dropna()
        converted = (1.0 + aligned["fund"]) * (1.0 + aligned["fx"]) - 1.0
        ret.loc[converted.index] = converted
    return ret


def live_category_levels(name: str) -> pd.DataFrame:
    levels = ORIGINAL_CATEGORY_LEVELS(name).copy()
    if levels.empty:
        return levels
    gross = load_gross_returns()
    if gross.empty:
        return levels
    _, cfg = base.config_by_name(name)
    if cfg is None:
        return levels

    runs = [cfg.get("bci"), *cfg.get("peers", [])]
    for run in runs:
        col = base.column_for_run(levels.columns, run)
        if col is None:
            continue
        run_norm = normalize_run(run)
        sub = gross[gross["run"].astype(str).map(normalize_run) == run_norm].copy()
        if sub.empty:
            continue
        sub["fecha"] = pd.to_datetime(sub["fecha"], errors="coerce").dt.normalize()
        sub = sub.dropna(subset=["fecha", "ret_bruta"]).sort_values("fecha")
        existing = levels[col].dropna()
        if existing.empty:
            continue
        last_date = pd.Timestamp(existing.index.max()).normalize()
        last_level = float(existing.loc[existing.index.max()])
        sub = sub[sub["fecha"] > last_date]
        if sub.empty:
            continue
        returns = _adjust_prom_returns(sub)
        current = last_level
        for dt, ret in returns.items():
            if pd.isna(ret):
                continue
            current *= 1.0 + float(ret)
            levels.loc[pd.Timestamp(dt), col] = current
    return levels.sort_index()


def _ytd_returns(levels: pd.DataFrame, reference_date: pd.Timestamp) -> pd.Series:
    available = levels.loc[:reference_date]
    start_of_year = pd.Timestamp(reference_date.year, 1, 1)
    result: dict[str, float] = {}
    for col in available.columns:
        s = available[col].dropna()
        if s.empty:
            result[str(col)] = float("nan")
            continue
        prior = s.loc[s.index < start_of_year]
        base_value = prior.iloc[-1] if not prior.empty else s.iloc[0]
        result[str(col)] = float(s.iloc[-1] / base_value - 1.0)
    return pd.Series(result, dtype=float)


def _cumulative_daily_peer(levels: pd.DataFrame, bci_col: str, peer_cols: list[str], reference_date: pd.Timestamp, target_date: pd.Timestamp) -> tuple[float, float]:
    common = levels.loc[:reference_date, [bci_col, *peer_cols]].dropna(how="any").sort_index()
    if len(common) < 2:
        return float("nan"), float("nan")
    bases = common.loc[:target_date]
    base_date = bases.index[-1] if not bases.empty else common.index[0]
    daily = common.loc[base_date:].pct_change(fill_method=None).dropna(how="all")
    if daily.empty or daily.isna().any(axis=None):
        return float("nan"), float("nan")
    fund = float((1.0 + daily[bci_col]).prod() - 1.0)
    bench = float((1.0 + daily[peer_cols].mean(axis=1)).prod() - 1.0)
    return fund, bench


def information_ratio_ytd(name: str, reference_date: pd.Timestamp | None = None) -> float:
    """IR anualizado usando retornos activos semanales del año calendario."""
    _, cfg = base.config_by_name(name)
    if cfg is None or not cfg.get("peers"):
        return float("nan")

    levels = live_category_levels(name)
    bci_col, peer_cols = base.configured_columns(name, levels)
    if levels.empty or bci_col is None or not peer_cols:
        return float("nan")

    required = [bci_col, *peer_cols]
    metric_levels = levels[required].dropna(how="any").sort_index()
    if metric_levels.empty:
        return float("nan")

    cutoff_value = reference_date if reference_date is not None else metric_levels.index.max()
    cutoff = pd.Timestamp(cutoff_value).normalize()
    weekly_levels = metric_levels.loc[:cutoff].resample("W-FRI").last().dropna(how="any")
    weekly_all = weekly_levels.pct_change(fill_method=None).dropna(how="any")
    start_of_year = pd.Timestamp(year=cutoff.year, month=1, day=1)
    weekly_ytd = weekly_all[(weekly_all.index >= start_of_year) & (weekly_all.index <= cutoff)]
    if len(weekly_ytd) < 2:
        return float("nan")

    active = weekly_ytd[bci_col] - weekly_ytd[peer_cols].mean(axis=1)
    te = base.ewma_te(active, annualize=True)
    return float(active.mean() * 52 / te) if pd.notna(te) and te > 0 else float("nan")


def compute_live_reference(name: str) -> dict | None:
    _, cfg = base.config_by_name(name)
    if cfg is None or not cfg.get("peers"):
        return None
    levels = live_category_levels(name)
    bci_col, peer_cols = base.configured_columns(name, levels)
    if levels.empty or bci_col is None or not peer_cols:
        return None
    required = [bci_col, *peer_cols]
    metric_levels = levels[required].dropna(how="any")
    if metric_levels.empty:
        return None
    reference_date = pd.Timestamp(metric_levels.index.max()).normalize()

    weekly_levels = metric_levels.resample("W-FRI").last().dropna(how="all")
    weekly_all = weekly_levels.pct_change(fill_method=None).dropna(how="all")
    weekly = weekly_all.tail(52)
    if len(weekly) < 2 or weekly.isna().any(axis=None):
        return None
    portfolio = weekly[bci_col]
    benchmark = weekly[peer_cols].mean(axis=1)
    active = portfolio - benchmark

    te_display = base.ewma_te(active, annualize=bool(cfg.get("te_anualizado", True)))
    te_ir = base.ewma_te(active, annualize=True)
    ir = float(active.mean() * 52 / te_ir) if pd.notna(te_ir) and te_ir > 0 else float("nan")
    ir_ytd = information_ratio_ytd(name, reference_date)

    ytd = _ytd_returns(levels[required], reference_date)
    portfolio_ytd = float(ytd[bci_col])
    peer_ytd = ytd[peer_cols]
    percentile = float((peer_ytd > portfolio_ytd).sum() / len(peer_ytd))
    quartile = max(1, min(4, math.ceil(percentile * 4)))
    # En el ZIP el benchmark YTD del peer group incluye al propio fondo BCI.
    benchmark_ytd = float(ytd[[bci_col, *peer_cols]].mean())
    alpha_ytd = portfolio_ytd - benchmark_ytd

    p1y, b1y = _cumulative_daily_peer(
        levels[required], bci_col, peer_cols, reference_date, reference_date - pd.DateOffset(years=1)
    )
    alpha_1y = p1y - b1y if pd.notna(p1y) and pd.notna(b1y) else float("nan")

    return {
        "Fondo": name,
        "Fecha": reference_date,
        "TE EWMA anual": te_display,
        "Alpha anual": alpha_1y,
        "Alpha YTD": alpha_ytd,
        "Information Ratio": ir,
        "Information Ratio YTD": ir_ytd,
        "Retorno YTD": portfolio_ytd,
        "Percentil YTD": percentile,
        "Cuartil YTD": quartile,
        "Retorno benchmark YTD": benchmark_ytd,
    }


def live_historical_te(name: str):
    levels = live_category_levels(name)
    bci_col, peer_cols = base.configured_columns(name, levels)
    if levels.empty or bci_col is None or not peer_cols:
        return {"labels": [], "bci": [], "p75": []}
    use = [bci_col, *peer_cols]
    weekly = levels[use].dropna(how="any").resample("W-FRI").last()
    returns = weekly.pct_change(fill_method=None).dropna(how="any")
    if len(returns) < 8:
        return {"labels": [], "bci": [], "p75": []}
    _, cfg = base.config_by_name(name)
    annualize = bool(cfg.get("te_anualizado", True))
    out = pd.DataFrame(index=returns.index, columns=use, dtype=float)
    for i in range(7, len(returns)):
        window = returns.iloc[: i + 1].tail(52)
        for fund in use:
            others = [c for c in use if c != fund]
            out.loc[returns.index[i], fund] = base.ewma_te(window[fund] - window[others].mean(axis=1), annualize=annualize)
    out = out.dropna(how="all")
    if out.empty:
        return {"labels": [], "bci": [], "p75": []}
    p75 = out.quantile(.75, axis=1)
    return {
        "labels": [d.strftime("%d-%m-%Y") for d in out.index],
        "bci": [round(float(v) * 100, 4) if pd.notna(v) else None for v in out[bci_col]],
        "p75": [round(float(v) * 100, 4) if pd.notna(v) else None for v in p75],
    }


def live_fund_dashboard(selected_run: str):
    catalog = base.bci_catalog()
    choice = next((x for x in catalog if normalize_run(x["run"]) == normalize_run(selected_run)), catalog[0])
    name = choice["fondo"]
    _, cfg = base.config_by_name(name)
    ref = compute_live_reference(name) or base.REFERENCE.get(name, {})
    percentile = ref.get("Percentil YTD")
    percentile_label = "—" if percentile is None or pd.isna(percentile) else f"{float(percentile) * 100:.0f}"
    quartile = ref.get("Cuartil YTD")
    quartile = None if quartile is None or pd.isna(quartile) else int(quartile)
    return {
        "run": str(cfg["bci"]),
        "fondo": name,
        "categoria": cfg.get("grupo", ""),
        "grupo": cfg.get("grupo", ""),
        "mer": base.pct(ref.get("TE EWMA anual"), 2),
        "ret_ytd": base.pct(ref.get("Retorno YTD"), 2, True),
        "alpha_1y": base.pct(ref.get("Alpha anual"), 2, True),
        "alpha_ytd": base.pct(ref.get("Alpha YTD"), 2, True),
        "ir_12m": base.number(ref.get("Information Ratio"), 2),
        "ir_ytd": base.number(ref.get("Information Ratio YTD"), 2),
        "percentil_ytd": percentile_label,
        "cuartil_ytd": quartile,
        "peer_rows": base.peer_rows_for(name),
        "chart": live_historical_te(name),
    }


# Las rutas definidas en flask_app_v2 resuelven estas funciones en runtime.
base.category_levels = live_category_levels
base.historical_te = live_historical_te
base.fund_dashboard = live_fund_dashboard


if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=False)
