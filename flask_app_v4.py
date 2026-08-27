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
EXCLUDED_RUNS = frozenset({"10331"})
REFERENCE_BASELINE = {key: dict(value) for key, value in base.REFERENCE.items()}


@lru_cache(maxsize=1)
def historical_universe_levels() -> pd.DataFrame:
    """Une todas las series históricas embebidas por RUN.

    El JSON define el P-group inicial; la unión histórica se conserva para
    recalcular una vez que el RUN tenga cuotas cargadas.
    """
    by_run: dict[str, pd.Series] = {}
    for payload in base.series.values():
        frame = base._payload_to_levels(payload)
        if frame.empty:
            continue
        for column in frame.columns:
            run = normalize_run(str(column).split("-", 1)[0])
            if not run or run in EXCLUDED_RUNS:
                continue
            values = frame[column].dropna().sort_index()
            if values.empty:
                continue
            current = by_run.get(run)
            by_run[run] = values if current is None else current.combine_first(values).sort_index()
    return pd.concat(by_run, axis=1).sort_index() if by_run else pd.DataFrame()


def available_peer_runs(name: str) -> list[str]:
    """RUN del P-group configurado, aunque todavía no tenga historia local."""
    _, cfg = base.config_by_name(name)
    if cfg is None or not cfg.get("peers"):
        return []
    bci_run = normalize_run(cfg.get("bci"))
    configured = [normalize_run(run) for run in cfg.get("peers", [])]
    return list(dict.fromkeys(
        run for run in configured
        if run and run not in EXCLUDED_RUNS and run != bci_run
    ))


@lru_cache(maxsize=1)
def historical_universe_metadata() -> dict[str, dict[str, str]]:
    embedded: dict[str, dict[str, str]] = {}
    for candidate in base.CONFIG.values():
        if not candidate.get("peers"):
            continue
        frame = ORIGINAL_CATEGORY_LEVELS(candidate["nombre"])
        for column in frame.columns:
            parts = str(column).split("-", 1)
            run = normalize_run(parts[0])
            if run and run not in EXCLUDED_RUNS and run not in embedded:
                embedded[run] = {
                    "fondo": parts[1].strip().title() if len(parts) > 1 else f"RUN {run}",
                    "categoria": candidate["nombre"],
                }
    return embedded


def available_peer_options(name: str) -> list[dict]:
    """Catálogo completo para el selector, con los peers JSON identificados."""
    _, cfg = base.config_by_name(name)
    if cfg is None:
        return []
    defaults = {normalize_run(run) for run in cfg.get("peers", [])}
    history_runs = set(historical_universe_levels().columns.astype(str))
    metadata = base.df.copy()
    metadata["run_norm"] = metadata["run"].astype(str).map(normalize_run)
    embedded_metadata = historical_universe_metadata()
    rows = []
    for run in available_peer_runs(name):
        match = metadata[metadata["run_norm"] == run]
        fallback = embedded_metadata.get(run, {"fondo": f"RUN {run}", "categoria": "Serie histórica"})
        rows.append({
            "run": run,
            "fondo": str(match.iloc[0].fondo) if not match.empty else fallback["fondo"],
            "categoria": str(cfg.get("grupo") or (match.iloc[0].categoria if not match.empty else fallback["categoria"])),
            "json_default": run in defaults,
            "has_history": run in history_runs,
        })
    return sorted(rows, key=lambda row: (not row["json_default"], row["categoria"], row["fondo"], row["run"]))


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


def _merge_return_segments(
    levels: pd.DataFrame,
    column: str,
    returns: pd.Series,
    baseline_date: pd.Timestamp | None,
) -> pd.DataFrame:
    """Incorpora bloques contiguos sin volver a componer cierres existentes.

    Las series embebidas contienen cierres semanales ya validados (incluido el
    31-07). Una cartola con solape vuelve a traer esos mismos días; si se
    capitaliza el retorno sobre un cierre que ya existe, el nivel queda
    duplicado y el YTD del corte cambia. Los cierres existentes son, por tanto,
    anclas autoritativas: se conservan y el nivel acumulado se reinicia allí.
    """
    returns = pd.to_numeric(returns, errors="coerce").dropna().sort_index()
    if returns.empty:
        return levels
    returns.index = pd.to_datetime(returns.index).normalize()
    # Un archivo histórico antiguo puede contener más de un retorno por
    # RUN/fecha. El retorno diario del RUN es único; conservar el último valor
    # evita capitalizar dos veces el mismo día.
    if returns.index.has_duplicates:
        returns = returns.groupby(level=0, sort=True).last()
    segment = returns.index.to_series().diff().dt.days.gt(7).cumsum().to_numpy()
    for _, block in returns.groupby(segment):
        first_date = pd.Timestamp(block.index.min()).normalize()
        existing = levels[column].dropna().sort_index()
        anchors = existing[existing.index < first_date]
        if anchors.empty:
            continue
        anchor_date = pd.Timestamp(anchors.index[-1]).normalize()
        current = float(anchors.iloc[-1])

        if (first_date - anchor_date).days > 7:
            baseline = pd.Timestamp(baseline_date).normalize() if baseline_date is not None else None
            if baseline is None or not (anchor_date < baseline < first_date) or (first_date - baseline).days > 7:
                continue
            # La referencia validada llega al baseline aunque la serie gráfica
            # cierre el viernes anterior. El nivel absoluto es irrelevante para
            # los retornos posteriores; este punto evita reutilizar el salto.
            levels.loc[baseline, column] = current

        for dt, ret in block.items():
            dt = pd.Timestamp(dt).normalize()
            # No sobrescribimos un cierre que ya estaba en la historia
            # validada (o que fue cargado previamente). Reiniciar `current`
            # aquí también evita arrastrar un retorno compuesto sobre el mismo
            # 31-07 hacia los días posteriores del bloque.
            if dt in levels.index and pd.notna(levels.at[dt, column]):
                current = float(levels.at[dt, column])
                continue
            current *= 1.0 + float(ret)
            levels.loc[dt, column] = current
    return levels


def live_category_levels(name: str, extra_runs: list[str] | None = None) -> pd.DataFrame:
    levels = ORIGINAL_CATEGORY_LEVELS(name).copy()
    _, cfg = base.config_by_name(name)
    if cfg is None:
        return levels

    runs = [cfg.get("bci"), *cfg.get("peers", []), *(extra_runs or [])]
    runs = list(dict.fromkeys(
        normalize_run(run) for run in runs
        if run and normalize_run(run) not in EXCLUDED_RUNS
    ))
    reference = REFERENCE_BASELINE.get(name, {})
    baseline_value = reference.get("Fecha")
    baseline_date = pd.Timestamp(baseline_value).normalize() if baseline_value is not None else None
    universe = historical_universe_levels()
    for run in runs:
        if base.column_for_run(levels.columns, run) is not None:
            continue
        source = base.column_for_run(universe.columns, run)
        if source is not None:
            levels = levels.join(universe[source].rename(source), how="outer")

    gross = load_gross_returns()
    if gross.empty:
        return levels.sort_index()
    for run in runs:
        col = base.column_for_run(levels.columns, run)
        run_norm = normalize_run(run)
        sub = gross[gross["run"].astype(str).map(normalize_run) == run_norm].copy()
        if sub.empty:
            continue
        sub["fecha"] = pd.to_datetime(sub["fecha"], errors="coerce").dt.normalize()
        sub = sub.dropna(subset=["fecha", "ret_bruta"]).sort_values("fecha")
        # Defensa adicional para archivos generados por versiones anteriores:
        # live_category_levels debe recibir un único retorno por RUN y fecha.
        sub = sub.drop_duplicates(["fecha", "run"], keep="last")
        returns = _adjust_prom_returns(sub)
        if col is None and not returns.empty:
            # Para RUN nuevos del Excel sin serie embebida, el primer nivel es
            # sintético (100). La escala no afecta retornos, TE, alpha ni IR;
            # permite que una cartola cargada por el usuario entre al P-group.
            cumulative = (1.0 + returns).cumprod()
            levels = levels.join(cumulative.rename(run_norm), how="outer")
            continue
        if col is None:
            continue
        levels = _merge_return_segments(levels, col, returns, baseline_date)
    return levels.sort_index()


def _columns_for_peers(name: str, levels: pd.DataFrame, peer_runs: list[str] | None = None):
    _, cfg = base.config_by_name(name)
    if cfg is None or levels.empty:
        return None, []
    bci = base.column_for_run(levels.columns, cfg["bci"])
    requested = cfg.get("peers", []) if peer_runs is None else peer_runs
    peers = [base.column_for_run(levels.columns, run) for run in requested]
    peers = [column for column in peers if column is not None and column != bci]
    return bci, list(dict.fromkeys(peers))


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


def information_ratio_ytd(
    name: str,
    reference_date: pd.Timestamp | None = None,
    peer_runs: list[str] | None = None,
) -> float:
    """IR anualizado usando retornos activos semanales del año calendario."""
    _, cfg = base.config_by_name(name)
    requested = cfg.get("peers", []) if peer_runs is None and cfg is not None else (peer_runs or [])
    if cfg is None or not requested:
        return float("nan")

    levels = live_category_levels(name, requested)
    bci_col, peer_cols = _columns_for_peers(name, levels, requested)
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


def compute_custom_reference(
    name: str,
    peer_runs: list[str],
    cutoff_date: pd.Timestamp | None = None,
) -> dict | None:
    """Recalcula el fondo contra una selección explícita de peer RUN."""
    _, cfg = base.config_by_name(name)
    if cfg is None or not peer_runs:
        return None

    levels = live_category_levels(name, peer_runs)
    if cutoff_date is not None:
        levels = levels.loc[:pd.Timestamp(cutoff_date).normalize()]
    bci_col, peer_cols = _columns_for_peers(name, levels, peer_runs)
    if levels.empty or bci_col is None or not peer_cols:
        return None

    required = [bci_col, *peer_cols]
    metric_levels = levels[required].dropna(how="any")
    if metric_levels.empty:
        return None
    reference_date = pd.Timestamp(metric_levels.index.max()).normalize()

    weekly_levels = metric_levels.resample("W-FRI").last().dropna(how="all")
    weekly_all = weekly_levels.pct_change(fill_method=None).dropna(how="all")
    weekly = weekly_all[weekly_all.index <= reference_date].tail(52)
    if len(weekly) < 2 or weekly.isna().any(axis=None):
        return None
    portfolio = weekly[bci_col]
    benchmark = weekly[peer_cols].mean(axis=1)
    active = portfolio - benchmark

    te_display = base.ewma_te(active, annualize=bool(cfg.get("te_anualizado", True)))
    te_ir = base.ewma_te(active, annualize=True)
    ir = float(active.mean() * 52 / te_ir) if pd.notna(te_ir) and te_ir > 0 else float("nan")
    ir_ytd = information_ratio_ytd(name, reference_date, peer_runs)

    ytd = _ytd_returns(levels[required], reference_date)
    portfolio_ytd = float(ytd[bci_col])
    peer_ytd = ytd[peer_cols]
    percentile = float((peer_ytd > portfolio_ytd).sum() / len(peer_ytd))
    quartile = max(1, min(4, math.ceil(percentile * 4)))
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


def analysis_date_bounds(name: str, peer_runs: list[str] | None = None):
    levels = live_category_levels(name, peer_runs)
    bci_col, peer_cols = _columns_for_peers(name, levels, peer_runs)
    use = [column for column in [bci_col, *peer_cols] if column is not None]
    if levels.empty or not use:
        return None, None
    common = levels[use].dropna(how="any")
    if common.empty:
        return None, None
    return pd.Timestamp(common.index.min()).normalize(), pd.Timestamp(common.index.max()).normalize()


def live_historical_te(
    name: str,
    peer_runs: list[str] | None = None,
    cutoff_date: pd.Timestamp | None = None,
):
    levels = live_category_levels(name, peer_runs)
    if cutoff_date is not None:
        levels = levels.loc[:pd.Timestamp(cutoff_date).normalize()]
    bci_col, peer_cols = _columns_for_peers(name, levels, peer_runs)
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


def peer_name(run: str) -> str:
    target = normalize_run(run)
    match = base.df[base.df.run.astype(str).map(normalize_run) == target]
    return str(match.iloc[0].fondo) if not match.empty else f"RUN {run}"


def custom_peer_rows(
    name: str,
    peer_runs: list[str],
    cutoff_date: pd.Timestamp | None = None,
):
    _, cfg = base.config_by_name(name)
    levels = live_category_levels(name, peer_runs)
    if cutoff_date is not None:
        levels = levels.loc[:pd.Timestamp(cutoff_date).normalize()]
    bci_col, peer_cols = _columns_for_peers(name, levels, peer_runs)
    if cfg is None or levels.empty or bci_col is None or not peer_cols:
        return []

    use = [bci_col, *peer_cols]
    common = levels[use].dropna(how="any").sort_index()
    if common.empty:
        return []
    reference_date = pd.Timestamp(common.index.max()).normalize()
    weekly = common.resample("W-FRI").last().pct_change(fill_method=None).dropna(how="any")
    weekly = weekly[weekly.index <= reference_date].tail(52)
    if len(weekly) < 2:
        return []
    ytd = _ytd_returns(common, reference_date)
    run_by_column = {bci_col: str(cfg["bci"])}
    for run in peer_runs:
        column = base.column_for_run(peer_cols, run)
        if column is not None:
            run_by_column.setdefault(column, str(run))

    rows = []
    for column in use:
        others = [candidate for candidate in use if candidate != column]
        active = weekly[column] - weekly[others].mean(axis=1)
        te_display = base.ewma_te(active, annualize=bool(cfg.get("te_anualizado", True)))
        te_ir = base.ewma_te(active, annualize=True)
        ir = float(active.mean() * 52 / te_ir) if pd.notna(te_ir) and te_ir > 0 else float("nan")
        p1y, b1y = _cumulative_daily_peer(
            common, column, others, reference_date, reference_date - pd.DateOffset(years=1)
        )
        alpha = p1y - b1y if pd.notna(p1y) and pd.notna(b1y) else float("nan")
        percentile = float((ytd[others] > ytd[column]).sum() / len(others))
        run = run_by_column.get(column, str(column))
        rows.append({
            "run": run,
            "fondo": peer_name(run),
            "es_bci": column == bci_col,
            "alpha": base.pct(alpha, 2, True),
            "ir": base.number(ir, 2),
            "mer": base.pct(te_display, 2),
            "percentil": f"{percentile * 100:.0f}",
        })
    return sorted(rows, key=lambda row: (not row["es_bci"], row["fondo"]))


def live_fund_dashboard(
    selected_run: str,
    peer_runs: list[str] | None = None,
    cutoff_date: str | None = None,
):
    catalog = base.bci_catalog()
    choice = next((x for x in catalog if normalize_run(x["run"]) == normalize_run(selected_run)), catalog[0])
    name = choice["fondo"]
    _, cfg = base.config_by_name(name)
    cutoff = pd.to_datetime(cutoff_date, errors="coerce") if cutoff_date else None
    cutoff = None if cutoff is None or pd.isna(cutoff) else pd.Timestamp(cutoff).normalize()
    custom = peer_runs is not None or cutoff is not None
    selected_peers = peer_runs if peer_runs is not None else [str(run) for run in cfg.get("peers", [])]
    ref = (
        compute_custom_reference(name, selected_peers, cutoff) if custom else compute_live_reference(name)
    ) or base.REFERENCE.get(name, {})
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
        "peer_rows": custom_peer_rows(name, selected_peers, cutoff) if custom else base.peer_rows_for(name),
        "chart": live_historical_te(name, selected_peers if custom else None, cutoff),
    }


# Las rutas definidas en flask_app_v2 resuelven estas funciones en runtime.
base.category_levels = live_category_levels
base.historical_te = live_historical_te
base.fund_dashboard = live_fund_dashboard


if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=False)
