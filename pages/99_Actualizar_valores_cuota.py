from __future__ import annotations

import base64
import gzip
import json
import os

import pandas as pd
import streamlit as st

from metrics_data import METRICS_GZ_B64
from quota_update import load_latest_quota, load_status, normalize_run, parse_quota_file, persist_quota, validate_quota_file

CMF_URL = "https://www.cmfchile.cl/institucional/estadisticas/fm.bpr_menu.php"

st.set_page_config(page_title="Actualizar valores cuota", page_icon="🔄", layout="centered")

st.markdown(
    """
<style>
.block-container { max-width: 780px; padding-top: .7rem; padding-bottom: 2rem; }
.admin-head { background:#064A91; color:white; padding:1rem 1.1rem; border-radius:10px; margin-bottom:.8rem; }
.admin-head h1 { margin:0; color:white; font-size:1.45rem; }
.admin-head p { margin:.35rem 0 0; opacity:.9; font-size:.88rem; }
.step { background:white; border:1px solid #D8E2EA; border-radius:10px; padding:.8rem .9rem; margin:.65rem 0; }
.step strong { color:#0B2F4A; }
@media (max-width:720px) {
  .block-container { padding:.35rem .55rem 1.5rem; }
  .admin-head h1 { font-size:1.15rem; }
}
</style>
""",
    unsafe_allow_html=True,
)

ADMIN_PIN = os.getenv("ADMIN_PIN", "")
if ADMIN_PIN:
    if "quota_admin_ok" not in st.session_state:
        st.session_state.quota_admin_ok = False
    if not st.session_state.quota_admin_ok:
        st.markdown('<div class="admin-head"><h1>Panel de actualización</h1><p>Acceso restringido</p></div>', unsafe_allow_html=True)
        pin = st.text_input("PIN", type="password")
        if st.button("Entrar", width="stretch"):
            if pin == ADMIN_PIN:
                st.session_state.quota_admin_ok = True
                st.rerun()
            else:
                st.error("PIN incorrecto")
        st.stop()

metrics = json.loads(gzip.decompress(base64.b64decode(METRICS_GZ_B64)).decode("utf-8"))
expected_runs = {normalize_run(row["run"]) for row in metrics if row.get("run")}

st.markdown(
    '<div class="admin-head"><h1>Actualizar valores cuota</h1><p>CMF → captcha → archivo → validación → histórico</p></div>',
    unsafe_allow_html=True,
)

status = load_status()
if status:
    a, b, c = st.columns(3)
    a.metric("Última fecha", status.get("latest_date", "—"))
    b.metric("RUN cubiertos", f"{status.get('matched_runs', 0)}/{status.get('expected_runs', 0)}")
    c.metric("Histórico", f"{status.get('history_rows', 0)} filas")
else:
    st.info("Todavía no hay una carga diaria guardada en este deployment.")

st.markdown('<div class="step"><strong>1 · Abrir la consulta oficial CMF</strong><br>Selecciona la consulta de valor cuota, resuelve el captcha y descarga el archivo del período disponible.</div>', unsafe_allow_html=True)
st.link_button("Abrir consulta CMF", CMF_URL, width="stretch")

st.markdown('<div class="step"><strong>2 · Subir el archivo descargado</strong><br>Admite CSV, XLS, XLSX o XLSM. La app intenta reconocer automáticamente Fecha, RUN y Valor Cuota.</div>', unsafe_allow_html=True)
uploaded = st.file_uploader("Archivo CMF", type=["csv", "xls", "xlsx", "xlsm"], label_visibility="collapsed")

if uploaded is not None:
    try:
        frame = parse_quota_file(uploaded.getvalue(), uploaded.name)
        validation = validate_quota_file(frame, expected_runs)
    except Exception as exc:
        st.error(f"No pude procesar el archivo: {exc}")
        st.stop()

    latest_date = validation["latest_date"]
    st.success(f"Archivo leído. Última fecha detectada: {latest_date}")

    a, b, c = st.columns(3)
    a.metric("Filas válidas", validation["rows"])
    b.metric("RUN esperados", validation["matched_runs"])
    c.metric("Cobertura", f"{validation['coverage']:.1%}")

    latest = frame[frame["fecha"] == pd.Timestamp(latest_date)].copy()
    st.dataframe(latest.head(30), width="stretch", hide_index=True)

    if validation["missing_runs"]:
        with st.expander(f"RUN faltantes ({len(validation['missing_runs'])})"):
            st.write(", ".join(validation["missing_runs"]))

    if not validation["ok"]:
        st.error("La carga no supera los controles mínimos. No se guardará. Se exige al menos 80% de cobertura de los RUN del dashboard y ningún valor cuota no positivo.")
    else:
        confirm = st.checkbox("Confirmo que este archivo corresponde a la descarga oficial de CMF")
        if st.button("Guardar actualización", type="primary", width="stretch", disabled=not confirm):
            saved = persist_quota(frame, uploaded.name, validation)
            st.success(f"Actualización guardada: {saved['latest_date']} · {saved['matched_runs']}/{saved['expected_runs']} RUN cubiertos.")
            st.info("La carga válida anterior no se reemplaza hasta que este paso termina correctamente.")

st.markdown("### Última carga guardada")
latest_saved = load_latest_quota()
if latest_saved.empty:
    st.caption("Sin datos diarios persistidos todavía.")
else:
    st.dataframe(latest_saved, width="stretch", hide_index=True)
    st.download_button(
        "Descargar respaldo CSV",
        latest_saved.to_csv(index=False).encode("utf-8-sig"),
        "ultima_carga_valores_cuota.csv",
        "text/csv",
        width="stretch",
    )

st.caption("Nota: el captcha se resuelve en el sitio oficial de CMF. La aplicación automatiza la validación y almacenamiento posterior, sin intentar eludir el captcha.")
