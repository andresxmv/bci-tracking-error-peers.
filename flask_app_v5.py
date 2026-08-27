from __future__ import annotations

import os

import pandas as pd

import flask_app_v4 as v4
import flask_app_v2 as base
from quota_update import load_gross_returns, normalize_run

app = base.app

# Congelamos el panel original como baseline histórico. Nunca se vuelve a usar
# una referencia ya corregida como punto de partida, evitando doble composición.
BASELINE_REFERENCE = {k: dict(v) for k, v in base.REFERENCE.items()}
LIVE_REFERENCES: dict[str, dict] = {}
_ORIGINAL_PERSIST_QUOTA = base.persist_quota


def _post_baseline_returns(cfg: dict, baseline_date: pd.Timestamp, reference_date: pd.Timestamp) -> dict[str, pd.Series]:
    """Retornos brutos diarios posteriores al corte histórico validado."""
    gross = load_gross_returns()
    if gross.empty:
        return {}

    gross = gross.copy()
    gross["fecha"] = pd.to_datetime(gross["fecha"], errors="coerce").dt.normalize()
    gross["run_norm"] = gross["run"].astype(str).map(normalize_run)
    gross = gross[(gross["fecha"] > baseline_date) & (gross["fecha"] <= reference_date)]
    gross = gross.drop_duplicates(["fecha", "run_norm"], keep="last")

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
    """YTD calendario = 31-12 previo -> última cartola disponible.

    La base es el YTD validado al corte histórico original (21-08-2026 en el
    dataset actual). Después se compone únicamente con retornos CMF posteriores
    a ese corte. La ventana de 52 semanas nunca se usa para construir el YTD.
    """
    baseline = BASELINE_REFERENCE.get(name)
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
            daily = pd.concat(pieces, axis=1).sort_index().dropna(how="any")
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

    # Mientras no exista una reconstrucción histórica individual de cada peer
    # para YTD, conservamos el ranking validado del corte histórico.
    corrected["Percentil YTD"] = baseline.get("Percentil YTD")
    corrected["Cuartil YTD"] = baseline.get("Cuartil YTD")

    # TE / IR se actualizan con cierres semanales completos.
    next_friday = baseline_date + pd.offsets.Week(weekday=4)
    if reference_date < next_friday:
        corrected["TE EWMA anual"] = baseline.get("TE EWMA anual")
        corrected["Information Ratio"] = baseline.get("Information Ratio")
        corrected["Information Ratio YTD"] = v4.information_ratio_ytd(name, baseline_date)
        corrected["Alpha anual"] = baseline.get("Alpha anual")

    return corrected


def compute_reference(
    name: str,
    peer_runs: list[str] | None = None,
    cutoff_date: pd.Timestamp | None = None,
) -> dict | None:
    _, cfg = base.config_by_name(name)
    if cfg is None:
        return None

    if peer_runs is not None or cutoff_date is not None:
        effective_peers = peer_runs if peer_runs is not None else [str(run) for run in cfg.get("peers", [])]
        return v4.compute_custom_reference(name, effective_peers, cutoff_date)

    try:
        live = v4.compute_live_reference(name)
    except Exception:
        live = None

    if not live:
        live = dict(BASELINE_REFERENCE.get(name, {}))
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

    # CLAVE: además de nuestro cache, reemplazamos la referencia que usa
    # flask_app_v2. Así, aunque una ruta llame al fund_dashboard original,
    # el dashboard queda obligado a mostrar el YTD corregido.
    for name, ref in refreshed.items():
        base.REFERENCE[name] = dict(ref)

    cp = LIVE_REFERENCES.get("CP Activa", {})
    print(
        "V5_RUNTIME_CP_ACTIVA",
        "fecha=", cp.get("Fecha"),
        "ytd=", cp.get("Retorno YTD"),
        "alpha_ytd=", cp.get("Alpha YTD"),
        "ir=", cp.get("Information Ratio"),
        flush=True,
    )
    return LIVE_REFERENCES


def persist_quota_and_recompute(frame, source_filename: str, validation: dict):
    saved = _ORIGINAL_PERSIST_QUOTA(frame, source_filename, validation)
    recompute_all_metrics()
    return saved


def compute_live_reference(
    name: str,
    peer_runs: list[str] | None = None,
    cutoff_date: pd.Timestamp | None = None,
) -> dict | None:
    if peer_runs is not None or cutoff_date is not None:
        ref = compute_reference(name, peer_runs, cutoff_date)
        return dict(ref) if ref else None
    ref = LIVE_REFERENCES.get(name)
    if ref is None:
        ref = compute_reference(name)
    return dict(ref) if ref else None


def _peer_selection(name: str, requested_runs: list[str] | None):
    _, cfg = base.config_by_name(name)
    default_runs = [normalize_run(run) for run in (cfg.get("peers", []) if cfg else [])]
    available_runs = set(v4.available_peer_runs(name))
    if requested_runs is None:
        return default_runs, False, None

    selected = []
    for run in requested_runs:
        normalized = normalize_run(run)
        if normalized and normalized not in selected:
            selected.append(normalized)

    invalid = [run for run in selected if run not in available_runs]
    if invalid:
        return default_runs, False, f"RUN sin serie histórica disponible: {', '.join(invalid)}"
    if not selected:
        return default_runs, False, "Selecciona al menos un peer RUN para recalcular."

    is_custom = set(selected) != set(default_runs)
    return selected, is_custom, None


def _cutoff_selection(name: str, peer_runs: list[str], requested_date: str | None):
    minimum, common_maximum = v4.analysis_date_bounds(name, peer_runs)
    default_ref = LIVE_REFERENCES.get(name) or BASELINE_REFERENCE.get(name, {})
    configured_date = default_ref.get("Fecha")
    configured_maximum = pd.Timestamp(configured_date).normalize() if configured_date is not None else None
    maximum_candidates = [date for date in (common_maximum, configured_maximum) if date is not None]
    maximum = max(maximum_candidates) if maximum_candidates else None
    if minimum is None or maximum is None:
        return None, None, None, False, "No hay fechas comunes para los RUN seleccionados."
    if not requested_date:
        return maximum, minimum, maximum, False, None

    parsed = pd.to_datetime(requested_date, errors="coerce")
    if pd.isna(parsed):
        return maximum, minimum, maximum, False, "La fecha de corte no es válida."

    selected = pd.Timestamp(parsed).normalize()
    if selected < minimum:
        return minimum, minimum, maximum, True, f"La primera fecha disponible es {minimum:%Y-%m-%d}."
    if selected > maximum:
        return maximum, minimum, maximum, False, f"La última fecha disponible es {maximum:%Y-%m-%d}."
    return selected, minimum, maximum, selected < maximum, None


def live_fund_dashboard(
    selected_run: str,
    peer_runs: list[str] | None = None,
    cutoff_date: str | None = None,
):
    catalog = base.bci_catalog()
    choice = next((x for x in catalog if normalize_run(x["run"]) == normalize_run(selected_run)), catalog[0])
    name = choice["fondo"]
    _, cfg = base.config_by_name(name)
    selected_peers, is_custom, peer_error = _peer_selection(name, peer_runs)
    selected_cutoff, cutoff_min, cutoff_max, cutoff_is_custom, cutoff_error = _cutoff_selection(
        name, selected_peers, cutoff_date
    )
    analysis_is_custom = bool(is_custom or cutoff_is_custom)
    custom_peers = selected_peers if analysis_is_custom else None
    custom_cutoff = selected_cutoff if cutoff_is_custom else None
    ref = compute_live_reference(name, custom_peers, custom_cutoff) or base.REFERENCE.get(name, {})

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
        "peer_rows": v4.custom_peer_rows(name, selected_peers, custom_cutoff) if analysis_is_custom else base.peer_rows_for(name),
        "chart": v4.live_historical_te(name, custom_peers, custom_cutoff),
        "peer_options": [
            {**peer, "selected": peer["run"] in selected_peers}
            for peer in v4.available_peer_options(name)
        ],
        "peer_default_count": len(cfg.get("peers", [])),
        "peer_count": len(selected_peers),
        "peer_is_custom": is_custom,
        "peer_error": peer_error,
        "cutoff_value": selected_cutoff.strftime("%Y-%m-%d") if selected_cutoff is not None else "",
        "cutoff_min": cutoff_min.strftime("%Y-%m-%d") if cutoff_min is not None else "",
        "cutoff_max": cutoff_max.strftime("%Y-%m-%d") if cutoff_max is not None else "",
        "cutoff_is_custom": cutoff_is_custom,
        "cutoff_error": cutoff_error,
        "analysis_is_custom": analysis_is_custom,
        "analysis_date": pd.Timestamp(ref.get("Fecha")).strftime("%Y-%m-%d") if ref.get("Fecha") is not None else "—",
    }


def verified_health():
    ref = compute_live_reference("CP Activa") or {}
    ytd = ref.get("Retorno YTD")
    dt = ref.get("Fecha")

    valid_ytd = ytd is not None and not pd.isna(ytd) and 0.10 < float(ytd) < 0.25
    valid_date = dt is not None and pd.Timestamp(dt).normalize() >= pd.Timestamp("2026-08-25")

    payload = {
        "ok": bool(valid_ytd and valid_date),
        "runtime": "flask_app_v5_authoritative_ytd",
        "cp_activa_fecha": pd.Timestamp(dt).strftime("%Y-%m-%d") if dt is not None else None,
        "cp_activa_ytd": float(ytd) if ytd is not None and not pd.isna(ytd) else None,
        "cp_activa_alpha_ytd": ref.get("Alpha YTD"),
        "cp_activa_ir": ref.get("Information Ratio"),
    }
    print("V5_HEALTH", payload, flush=True)
    return payload, (200 if payload["ok"] else 500)


base.persist_quota = persist_quota_and_recompute
base.compute_live_reference = compute_live_reference
base.fund_dashboard = live_fund_dashboard
recompute_all_metrics()

if "health" in app.view_functions:
    app.view_functions["health"] = verified_health


@app.after_request
def _disable_browser_cache(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=False)
