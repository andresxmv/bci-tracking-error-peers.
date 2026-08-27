from __future__ import annotations

import math
import os

import pandas as pd

import flask_app_v4 as v4
import flask_app_v2 as base
from quota_update import load_gross_returns, normalize_run

app = base.app

# Métricas recalculadas después de cada actualización.
LIVE_REFERENCES: dict[str, dict] = {}
_ORIGINAL_PERSIST_QUOTA = base.persist_quota


def _post_baseline_returns(cfg: dict, baseline_date: pd.Timestamp, reference_date: pd.Timestamp) -> dict[str, pd.Series]:
    """Retornos brutos diarios posteriores al corte de referencia del ZIP."""
    gross = load_gross_returns()
    if gross.empty:
        return {}
    gross = gross.copy()
    gross["fecha"] = pd.to_datetime(gross["fecha"], errors="coerce").dt.normalize()
    gross["run_norm"] = gross["run"].astype(str).map(normalize_run)
    gross = gross[(gross["fecha"] > baseline_date) & (gross["fecha"] <= reference_date)]

    out: dict[str, pd.Series] = {}
    for run in [cfg.get("bci"), *cfg.get("peers", [])]:
        run_norm = normalize_run(run)
        sub = gross[gross["run_norm"] == run_norm].dropna(subset=["fecha", "ret_bruta"]).sort_values("fecha")
        if sub.empty:
            continue
        ret = v4._adjust_prom_returns(sub)
        ret = pd.to_numeric(ret, errors="coerce").dropna()
        if not ret.empty:
            out[run_norm] = ret
    return out


def _compound(s: pd.Series) -> float:
    s = pd.to_numeric(s, errors="coerce").dropna()
    return float((1.0 + s).prod() - 1.0) if not s.empty else 0.0


def _correct_calendar_ytd(name: str, cfg: dict, live_ref: dict) -> dict:
    """Actualiza YTD desde el YTD validado del ZIP, no desde la ventana de 52 semanas.

    panel_metrics_reference.json contiene el YTD correcto al corte original
    (21-08-2026 en CP Activa). Solo se compone el retorno de cartolas CMF
    posterior a ese corte. Así el cache de 52 semanas queda exclusivamente para
    TE/IR y nunca vuelve a ser la base del YTD.
    """
    baseline = base.REFERENCE.get(name)
    if not baseline:
        return live_ref

    baseline_date = pd.Timestamp(baseline.get("Fecha")).normalize()
    reference_date = pd.Timestamp(live_ref.get("Fecha", baseline_date)).normalize()
    if reference_date < baseline_date:
        reference_date = baseline_date

    bci_run = normalize_run(cfg.get("bci"))
    post = _post_baseline_returns(cfg, baseline_date, reference_date)

    base_port = baseline.get("Retorno YTD")
    if base_port is None or pd.isna(base_port):
        return live_ref
    post_port = _compound(post.get(bci_run, pd.Series(dtype=float)))
    portfolio_ytd = (1.0 + float(base_port)) * (1.0 + post_port) - 1.0

    # Benchmark del proyecto = BCI + peers equiponderados. Partimos del YTD
    # benchmark validado al corte original y componemos el retorno diario
    # equiponderado observado después del corte.
    base_bench = baseline.get("Retorno benchmark YTD")
    benchmark_ytd = float("nan")
    if base_bench is not None and not pd.isna(base_bench):
        pieces = []
        for run in [cfg.get("bci"), *cfg.get("peers", [])]:
            run_norm = normalize_run(run)
            s = post.get(run_norm)
            if s is not None and not s.empty:
                pieces.append(s.rename(run_norm))
        if pieces:
            daily = pd.concat(pieces, axis=1).sort_index()
            # Solo días con información de todos los fondos disponibles para no
            # sesgar el promedio por faltantes.
            daily = daily.dropna(how="any")
            bench_post = _compound(daily.mean(axis=1)) if not daily.empty else 0.0
        else:
            bench_post = 0.0
        benchmark_ytd = (1.0 + float(base_bench)) * (1.0 + bench_post) - 1.0

    corrected = dict(live_ref)
    corrected["Fecha"] = reference_date
    corrected["Retorno YTD"] = float(portfolio_ytd)
    if not pd.isna(benchmark_ytd):
        corrected["Retorno benchmark YTD"] = float(benchmark_ytd)
        corrected["Alpha YTD"] = float(portfolio_ytd - benchmark_ytd)

    # Percentil/cuarto requieren YTD individual de cada peer previo al corte.
    # Si no existe esa historia completa en runtime, conservar el valor validado
    # es preferible a inventarlo desde la ventana de 52 semanas.
    corrected["Percentil YTD"] = baseline.get("Percentil YTD")
    corrected["Cuartil YTD"] = baseline.get("Cuartil YTD")

    # TE e IR son semanales. Mientras no exista un nuevo viernes completo desde
    # el corte base, conservar el valor validado del último cierre semanal.
    next_friday = baseline_date + pd.offsets.Week(weekday=4)
    if reference_date < next_friday:
        corrected["TE EWMA anual"] = baseline.get("TE EWMA anual")
        corrected["Information Ratio"] = baseline.get("Information Ratio")
        corrected["Alpha anual"] = baseline.get("Alpha anual")

    return corrected


def compute_reference(name: str) -> dict | None:
    _, cfg = base.config_by_name(name)
    if cfg is None:
        return None
    try:
        live = v4.compute_live_reference(name)
    except Exception:
        live = None
    if not live:
        live = dict(base.REFERENCE.get(name, {}))
    if not live:
        return None
    return _correct_calendar_ytd(name, cfg, dict(live))


def recompute_all_metrics() -> dict[str, dict]:
    refreshed: dict[str, dict] = {}
    for item in base.bci_catalog():
        ref = compute_reference(item["fondo"])
        if ref:
            refreshed[item["fondo"]] = ref
    LIVE_REFERENCES.clear()
    LIVE_REFERENCES.update(refreshed)
    return LIVE_REFERENCES


def persist_quota_and_recompute(frame, source_filename: str, validation: dict):
    saved = _ORIGINAL_PERSIST_QUOTA(frame, source_filename, validation)
    recompute_all_metrics()
    return saved


def compute_live_reference(name: str) -> dict | None:
    ref = LIVE_REFERENCES.get(name)
    if ref is None:
        ref = compute_reference(name)
    return dict(ref) if ref else None


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


def verified_health():
    """Healthcheck también valida que CP Activa no vuelva al falso YTD ~3,79%."""
    ref = compute_live_reference("CP Activa") or {}
    ytd = ref.get("Retorno YTD")
    dt = ref.get("Fecha")
    valid_ytd = ytd is not None and not pd.isna(ytd) and 0.10 < float(ytd) < 0.25
    valid_date = dt is not None and pd.Timestamp(dt).normalize() >= pd.Timestamp("2026-08-25")
    payload = {
        "ok": bool(valid_ytd and valid_date),
        "cp_activa_fecha": pd.Timestamp(dt).strftime("%Y-%m-%d") if dt is not None else None,
        "cp_activa_ytd": float(ytd) if ytd is not None and not pd.isna(ytd) else None,
        "cp_activa_alpha_ytd": ref.get("Alpha YTD"),
        "cp_activa_ir": ref.get("Information Ratio"),
    }
    return (payload, 200 if payload["ok"] else 500)


base.persist_quota = persist_quota_and_recompute
base.compute_live_reference = compute_live_reference
base.fund_dashboard = live_fund_dashboard
recompute_all_metrics()

# Reemplaza el view existente de /health sin registrar una ruta duplicada.
if "health" in app.view_functions:
    app.view_functions["health"] = verified_health


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=False)
