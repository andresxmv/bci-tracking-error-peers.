from __future__ import annotations

import math
import os

import pandas as pd

import flask_app_v4 as v4
import flask_app_v2 as base
from quota_update import load_gross_returns, normalize_run

app = base.app


def _calendar_ytd_by_run(cfg: dict, reference_date: pd.Timestamp) -> dict[str, float]:
    """Compound daily gross returns from Jan 1 through the reference date.

    This deliberately does not use the 52-week level/cache window. The 52-week
    window remains only for TE / IR calculations.
    """
    gross = load_gross_returns()
    if gross.empty:
        return {}

    gross = gross.copy()
    gross["fecha"] = pd.to_datetime(gross["fecha"], errors="coerce").dt.normalize()
    gross["run_norm"] = gross["run"].astype(str).map(normalize_run)
    start = pd.Timestamp(reference_date.year, 1, 1)
    gross = gross[(gross["fecha"] >= start) & (gross["fecha"] <= reference_date)]

    result: dict[str, float] = {}
    for run in [cfg.get("bci"), *cfg.get("peers", [])]:
        run_norm = normalize_run(run)
        sub = gross[gross["run_norm"] == run_norm].dropna(subset=["fecha", "ret_bruta"]).sort_values("fecha")
        if sub.empty:
            result[run_norm] = float("nan")
            continue
        returns = v4._adjust_prom_returns(sub)
        returns = pd.to_numeric(returns, errors="coerce").dropna()
        result[run_norm] = float((1.0 + returns).prod() - 1.0) if not returns.empty else float("nan")
    return result


def compute_live_reference(name: str) -> dict | None:
    ref = v4.compute_live_reference(name)
    _, cfg = base.config_by_name(name)
    if ref is None or cfg is None or not cfg.get("peers"):
        return ref

    reference_date = pd.Timestamp(ref["Fecha"]).normalize()
    ytd = _calendar_ytd_by_run(cfg, reference_date)
    bci_run = normalize_run(cfg.get("bci"))
    peer_runs = [normalize_run(x) for x in cfg.get("peers", [])]

    portfolio_ytd = ytd.get(bci_run, float("nan"))
    peer_ytd = pd.Series([ytd.get(run, float("nan")) for run in peer_runs], dtype=float).dropna()
    if pd.isna(portfolio_ytd) or peer_ytd.empty:
        return ref

    percentile = float((peer_ytd > portfolio_ytd).sum() / len(peer_ytd))
    quartile = max(1, min(4, math.ceil(percentile * 4)))
    # Mantiene la convención del proyecto: benchmark YTD incluye BCI + peers.
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


# flask_app_v2 routes resolve these globals at request time.
base.compute_live_reference = compute_live_reference
base.fund_dashboard = live_fund_dashboard


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=False)
