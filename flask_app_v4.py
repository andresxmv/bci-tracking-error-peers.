from __future__ import annotations

import math
from functools import lru_cache

import numpy as np
import pandas as pd
import requests

import flask_app_v2 as base
from quota_update import (
    GROSS_RETURNS_PATH,
    MAX_RETURN_GAP_DAYS,
    SEED_RETURNS_PATH,
    load_gross_returns,
    normalize_run,
)


def _returns_signature() -> tuple:
    """Firma de los archivos de retornos, para invalidar cachés al cargar una cartola."""
    parts = []
    for path in (SEED_RETURNS_PATH, GROSS_RETURNS_PATH):
        try:
            stat = path.stat()
            parts.append((path.name, stat.st_mtime_ns, stat.st_size))
        except OSError:
            parts.append((path.name, 0, 0))
    return tuple(parts)

app = base.app
ORIGINAL_CATEGORY_LEVELS = base.category_levels
PROXY_URL = "https://nusycxhrfynrrbvdiiko.supabase.co/functions/v1/cmf-cartola-proxy"
PROXY_KEY = "bci-tracking-error-peers-v1"
# The Excel dictionary is the authoritative peer universe. A configured RUN
# without current history remains visible and becomes usable when its cartola
# is loaded; no configured peer is silently removed from the calculation.
EXCLUDED_RUNS = frozenset()
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
    # A RUN may not be present in the embedded weekly workbook but can already
    # have daily returns in the versioned seed or in runtime_data. Reflect that
    # fact in the selector instead of labelling a usable peer as unavailable.
    history_runs.update(_gross_by_run(_returns_signature()).keys())
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
    if frame is None or frame.empty or not {"fecha", "ret_bruta"}.issubset(frame.columns):
        return pd.Series(dtype=float)

    prepared = frame.copy()
    prepared["fecha"] = pd.to_datetime(prepared["fecha"], errors="coerce").dt.normalize()
    prepared["ret_bruta"] = pd.to_numeric(prepared["ret_bruta"], errors="coerce")
    prepared = prepared.dropna(subset=["fecha", "ret_bruta"]).sort_values("fecha")
    # El agregado bruto es único por RUN y fecha. La defensa también cubre
    # históricos antiguos que todavía puedan traer la misma fecha repetida.
    prepared = prepared.drop_duplicates("fecha", keep="last")
    ret = prepared.set_index("fecha")["ret_bruta"].astype(float).sort_index()

    # La semilla versionada ya trae los retornos PROM convertidos a CLP. Una
    # cartola cargada en runtime conserva MONEDA=PROM; al mezclar ambas fuentes
    # hay filas CLP y PROM en el mismo RUN. Convertir toda la serie sólo porque
    # existe un PROM vuelve a convertir los días de la semilla y altera alpha.
    # Aplicamos FX exclusivamente a las filas que todavía están denominadas en
    # PROM, dejando intactas las filas CLP.
    if "moneda" not in prepared.columns:
        return ret
    es_prom = prepared["moneda"].astype(str).str.strip().str.upper().eq("PROM")
    prom_dates = pd.DatetimeIndex(prepared.loc[es_prom, "fecha"].drop_duplicates())
    if len(prom_dates) == 0:
        return ret
    fx = _fx_returns_for(prom_dates)
    aligned = pd.concat(
        [ret.reindex(prom_dates).rename("fund"), fx.rename("fx")], axis=1
    ).dropna()
    if aligned.empty:
        return ret
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
    # Se trabaja sobre un diccionario y se escribe la columna una sola vez al
    # final. Insertar fila por fila con .loc reconstruye el DataFrame en cada
    # iteración; con retornos diarios de todo el año eso dejaba el arranque de
    # la aplicación en decenas de segundos. La lógica es la misma.
    values: dict[pd.Timestamp, float] = {
        pd.Timestamp(index).normalize(): value
        for index, value in levels[column].items()
    }
    touched = False
    for _, block in returns.groupby(segment):
        first_date = pd.Timestamp(block.index.min()).normalize()
        existing = sorted(date for date, value in values.items() if pd.notna(value))
        anchors = [date for date in existing if date < first_date]
        if not anchors:
            continue
        anchor_date = anchors[-1]
        current = float(values[anchor_date])

        if (first_date - anchor_date).days > 7:
            baseline = pd.Timestamp(baseline_date).normalize() if baseline_date is not None else None
            if baseline is None or not (anchor_date < baseline < first_date) or (first_date - baseline).days > 7:
                continue
            # La referencia validada llega al baseline aunque la serie gráfica
            # cierre el viernes anterior. El nivel absoluto es irrelevante para
            # los retornos posteriores; este punto evita reutilizar el salto.
            values[baseline] = current
            touched = True

        for dt, ret in block.items():
            dt = pd.Timestamp(dt).normalize()
            # No sobrescribimos un cierre que ya estaba en la historia
            # validada (o que fue cargado previamente). Reiniciar `current`
            # aquí también evita arrastrar un retorno compuesto sobre el mismo
            # 31-07 hacia los días posteriores del bloque.
            previous = values.get(dt)
            if previous is not None and pd.notna(previous):
                current = float(previous)
                continue
            current *= 1.0 + float(ret)
            values[dt] = current
            touched = True

    if not touched:
        return levels
    updated = pd.Series(values).sort_index()
    updated.index = pd.DatetimeIndex(updated.index)
    levels = levels.reindex(levels.index.union(updated.index))
    levels[column] = updated.reindex(levels.index)
    return levels


def live_category_levels(name: str, extra_runs: list[str] | None = None) -> pd.DataFrame:
    """Niveles de la categoría con los retornos diarios ya incorporados.

    Cada request del dashboard la pide varias veces (métricas, IR YTD, gráfico
    y tabla de peers). Reconstruirla cada vez, ahora que hay retornos diarios
    de todo el año, dejaba la página en varios segundos por consulta y el
    arranque en cerca de un minuto. Se cachea por nombre, peers y firma de los
    archivos de retornos, y se devuelve una copia para que nadie mute el caché.
    """
    signature = _returns_signature()
    key = (name, tuple(extra_runs) if extra_runs else None)
    return _cached_category_levels(key, signature).copy()


@lru_cache(maxsize=128)
def _cached_category_levels(key: tuple, signature: tuple) -> pd.DataFrame:
    name, extra = key
    return _build_live_category_levels(name, list(extra) if extra else None)


def _build_live_category_levels(name: str, extra_runs: list[str] | None = None) -> pd.DataFrame:
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

    by_run = _gross_by_run(_returns_signature())
    if not by_run:
        return levels.sort_index()
    for run in runs:
        col = base.column_for_run(levels.columns, run)
        run_norm = normalize_run(run)
        sub = by_run.get(run_norm)
        if sub is None or sub.empty:
            continue
        sub = sub.copy()
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


@lru_cache(maxsize=1)
def _gross_by_run(signature: tuple) -> dict[str, pd.DataFrame]:
    """Retornos brutos agrupados por RUN, una sola vez por versión del archivo.

    load_gross_returns() ya devuelve el RUN normalizado; volver a mapear
    normalize_run sobre las 55 mil filas por cada RUN y cada fondo costaba
    decenas de millones de llamadas en cada arranque.
    """
    gross = load_gross_returns()
    if gross.empty:
        return {}
    gross = gross.dropna(subset=["fecha", "ret_bruta"])
    gross = gross.sort_values(["run", "fecha"]).drop_duplicates(["fecha", "run"], keep="last")
    return {str(run): frame for run, frame in gross.groupby("run", sort=False)}


@lru_cache(maxsize=1)
def _daily_returns_index(signature: tuple) -> dict[str, pd.Series]:
    """Retorno bruto diario por RUN, en pesos, indexado una sola vez."""
    out: dict[str, pd.Series] = {}
    for run, sub in _gross_by_run(signature).items():
        try:
            serie = _adjust_prom_returns(sub)
        except Exception:
            serie = sub.set_index("fecha")["ret_bruta"].astype(float).sort_index()
        serie = pd.to_numeric(serie, errors="coerce").dropna()
        if not serie.empty:
            out[str(run)] = serie
    return out


def daily_returns_by_run(runs: list[str]) -> dict[str, pd.Series]:
    """Retorno bruto diario por RUN, ya convertido a pesos si el fondo informa en dólares."""
    index = _daily_returns_index(_returns_signature())
    out: dict[str, pd.Series] = {}
    for run in runs:
        run_norm = normalize_run(run)
        if not run_norm or run_norm in EXCLUDED_RUNS:
            continue
        serie = index.get(run_norm)
        if serie is not None and not serie.empty:
            out[run_norm] = serie
    return out


def _calendar_ytd(serie: pd.Series, cutoff: pd.Timestamp) -> float | None:
    """Retorno YTD calendario capitalizando los retornos diarios del año.

    Devuelve None si la serie no cubre el año completo hasta el corte. Un hueco
    mayor a MAX_RETURN_GAP_DAYS invalida la ventana: capitalizar sobre un salto
    de semanas daría un YTD inventado.
    """
    cutoff = pd.Timestamp(cutoff).normalize()
    start = pd.Timestamp(cutoff.year, 1, 1)
    window = serie.loc[(serie.index >= start) & (serie.index <= cutoff)]
    if window.empty:
        return None
    if pd.Timestamp(window.index.min()) > start + pd.Timedelta(days=MAX_RETURN_GAP_DAYS):
        return None
    if pd.Timestamp(window.index.max()) < cutoff - pd.Timedelta(days=MAX_RETURN_GAP_DAYS):
        return None
    if len(window) > 1:
        gaps = pd.Series(window.index).diff().dt.days.dropna()
        if not gaps.empty and gaps.max() > MAX_RETURN_GAP_DAYS:
            return None
    return float((1.0 + window).prod() - 1.0)


def calendar_ytd_metrics(
    name: str,
    peer_runs: list[str] | None,
    cutoff_date: pd.Timestamp,
) -> dict | None:
    """YTD calendario exacto del fondo y su P-group, desde retornos diarios.

    Es el mismo cálculo del panel de riesgo de mercado: se capitaliza el retorno
    bruto diario desde el 31-12 anterior hasta el corte, y el benchmark es el
    promedio simple de los YTD del grupo completo (BCI + peers). No reemplaza
    las fórmulas de TE ni de IR, que siguen usando los cierres semanales.

    Devuelve None si falta cobertura diaria; el llamador conserva su cálculo.
    """
    _, cfg = base.config_by_name(name)
    if cfg is None or not cfg.get("bci"):
        return None
    requested = cfg.get("peers", []) if peer_runs is None else peer_runs
    if not requested:
        return None

    bci_run = normalize_run(cfg["bci"])
    peers = [normalize_run(run) for run in requested]
    peers = [run for run in dict.fromkeys(peers) if run and run != bci_run]
    if not peers:
        return None

    series = daily_returns_by_run([bci_run, *peers])
    cutoff = pd.Timestamp(cutoff_date).normalize()

    if bci_run not in series:
        return None
    portfolio = _calendar_ytd(series[bci_run], cutoff)
    if portfolio is None:
        return None

    peer_ytd: dict[str, float] = {}
    for run in peers:
        serie = series.get(run)
        if serie is None:
            continue
        value = _calendar_ytd(serie, cutoff)
        if value is not None:
            peer_ytd[run] = value
    if not peer_ytd:
        return None

    benchmark = float(np.mean([portfolio, *peer_ytd.values()]))
    percentile = float(sum(1 for value in peer_ytd.values() if value > portfolio) / len(peer_ytd))
    return {
        "Fecha": cutoff,
        "Retorno YTD": portfolio,
        "Retorno benchmark YTD": benchmark,
        "Alpha YTD": portfolio - benchmark,
        "Percentil YTD": percentile,
        "Cuartil YTD": max(1, min(4, math.ceil(percentile * 4))),
        "Peers YTD": len(peer_ytd),
    }


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
