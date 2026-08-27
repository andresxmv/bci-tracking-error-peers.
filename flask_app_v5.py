from __future__ import annotations

import math
import os

import pandas as pd

import flask_app_v4 as v4
import flask_app_v2 as base
from quota_update import load_gross_returns, normalize_run

app = base.app


def _baseline_and_live_ytd(name: str, cfg: dict, reference_date: pd.Timestamp) -> dict[str, float]:
    """YTD calendario = YTD histórico al último corte embebido + retorno CMF posterior.

    El histórico embebido contiene el YTD correcto hasta su fecha de corte
    (por ejemplo 2026-08-21). La cartola CMF persistida solo contiene el tramo
    nuevo; por eso no debe interpretarse ese tramo como si fuese todo el YTD.
    """
    levels = v4.ORIGINAL_CATEGORY_LEVELS(name).copy()
    if levels.empty:
        return {}

    bci_col, peer_cols = base.configured_columns(name, levels)
    if bci_col is None or not peer_cols:
        return {}

    required = [bci_col, *peer_cols]
    common = levels[required].dropna(how="any").sort_index()
    if common.empty:
        return {}

    baseline_date = min(pd.Timestamp(common.index.max()).normalize(), reference_date)
    baseline_ytd = v4._ytd_returns(common, baseline_date)

    col_by_run: dict[str, str] = {}
    for run in [cfg.get("bci"), *cfg.get("peers", [])]:
        col = base.column_for_run(required, run)
        if col is not None:
            col_by_run[normalize_run(run)] = col

    gross = load_gross_returns()
    if gross.empty:
        return {
            run: float(baseline_ytd[col])
            for run, col in col_by_run.items()
            if col in baseline_ytd.index and pd.notna(baseline_ytd[col])
        }

    gross = gross.copy()
    gross["fecha"] = pd.to_datetime(gross["fecha"], errors="coerce").dt.normalize()
    gross["run_norm"] = gross["run"].astype(str).map(normalize_run)
    gross = gross[(gross["fecha"] > baseline_date) & (gross["fecha"] <= reference_date)]

    result: dict[str, float] = {}
    for run_norm, col in col_by_run.items():
        base_ytd = float(baseline_ytd.get(col, float("nan")))
        if pd.isna(base_ytd):
            continue

        sub = gross[gross["run_norm"] == run_norm].dropna(subset=["fecha", "ret_bruta"]).sort_values("fecha")
        if sub.empty:
            result[run_norm] = base_ytd
            continue

        returns = v4._adjust_prom_returns(sub)
        returns = pd.to_numeric(returns, errors="coerce").dropna()
        live_growth = float((1.0 + returns).prod()) if not returns.empty else 1.0
        result[run_norm] = float((1.0 + base_ytd) * live_growth - 1.0)

    return result


def compute_live_reference(name: str) -> dict | None:
    ref = v4.compute_live_reference(name)
    _, cfg = base.config_by_name(name)
    if ref is None or cfg is None or not cfg.get("peers"):
        return ref

    reference_date = pd.Timestamp(ref["Fecha"]).normalize()
    ytd = _baseline_and_live_ytd(name, cfg, reference_date)
    bci_run = normalize_run(cfg.get("bci"))
    peer_runs = [normalize_run(x) for x in cfg.get("peers", [])]

    portfolio_ytd = ytd.get(bci_run, float("nan"))
    peer_ytd = pd.Series([ytd.get(run, float("nan")) for run in peer_runs], dtype=float).dropna()
    if pd.isna(portfolio_ytd) or peer_ytd.empty:
        return ref

    percentile = float((peer_ytd > portfolio_ytd).sum() / len(peer_ytd))
    quartile = max(1, min(4, math.ceil(percentile * 4)))
    benchmark_ytd = float(pd.concat([pd.Series([portfolio_ytd]), peer_ytd], ignore_index=True).mean())

    ref = dict(ref)
    ref["Retorno YTD"] = float(portfolio_ytd)
    ref["Retorno benchmark YTD"] = benchmark_ytd
    ref["Alpha YTD"] = float(portfolio_ytd - benchmark_ytd)
    ref["Percentil YTD"] = percentile
    ref["Cuartil YTD"] = quartile
    return ref


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
        "percentil_ytd": percentile_label,
        "cuartil_ytd": quartile,
        "peer_rows": base.peer_rows_for(name),
        "chart": v4.live_historical_te(name),
    }


base.compute_live_reference = compute_live_reference
base.fund_dashboard = live_fund_dashboard


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=False)
