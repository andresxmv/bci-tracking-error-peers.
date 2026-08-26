from __future__ import annotations

import base64
import gzip
import json
import os
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from cmf_automation import CMFAutomationError, CMFQuotaSession
from metrics_data import METRICS_GZ_B64
from quota_update import load_latest_quota, load_status, normalize_run, parse_quota_file, persist_quota, validate_quota_file

st.set_page_config(page_title="Actualizar valores cuota", page_icon="🔄", layout="centered")

st.markdown(
    """
<style>
.block-container { max-width: 760px; padding-top: .6rem; padding-bottom: 2rem; }
.admin-head { background:#064A91; color:white; padding:1rem 1.1rem; border-radius:10px; margin-bottom:.8rem; }
.admin-head h1 { margin:0; color:white; font-size:1.4rem; }
.admin-head p { margin:.35rem 0 0; opacity:.92; font-size:.86rem; }
.step { background:white; border:1px solid #D8E2EA; border-radius:10px; padding:.85rem .95rem; margin:.65rem 0; }
.done { background:#F0FBF3; border:1px solid #9AD7A6; border-radius:10px; padding:.85rem .95rem; }
@media (max-width:720px) {
  .block-container { padding:.3rem .55rem 1.5rem; }
  .admin-head h1 { font-size:1.12rem; }
  div[data-testid="stHorizontalBlock"] { gap:.4rem; }
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

if "cmf_session" not in st.session_state:
    st.session_state.cmf_session = None
if "cmf_captcha" not in st.session_state:
    st.session_state.cmf_captcha = None
if "cmf_prepared_date" not in st.session_state:
    st.session_state.cmf_prepared_date = None

st.markdown(
    '<div class="admin-head"><h1>Actualizar valores cuota</h1><p>Elige fecha → resuelve captcha → listo</p></div>',
    unsafe_allow_html=True,
)

status = load_status()
if status:
    a, b, c = st.columns(3)
    a.metric("Última fecha", status.get("latest_date", "—"))
    b.metric("RUN cubiertos", f"{status.get('matched_runs', 0)}/{status.get('expected_runs', 0)}")
    c.metric("Histórico", f"{status.get('history_rows', 0)} filas")
else:
    st.info("Aún no hay una actualización diaria guardada en este deployment.")

st.markdown('<div class="step"><strong>1 · Elige la fecha</strong><br>Normalmente usarás el día anterior cuando la CMF ya haya publicado los valores cuota.</div>', unsafe_allow_html=True)
selected_date = st.date_input(
    "Fecha a descargar",
    value=date.today() - timedelta(days=1),
    max_value=date.today(),
    format="DD/MM/YYYY",
)

prepare_label = "Preparar captcha CMF"
if st.session_state.cmf_prepared_date == selected_date and st.session_state.cmf_captcha:
    prepare_label = "Generar captcha nuevo"

if st.button(prepare_label, type="primary", width="stretch"):
    old = st.session_state.cmf_session
    if old is not None:
        try:
            old.close()
        except Exception:
            pass
    session = CMFQuotaSession()
    try:
        with st.spinner("Abriendo CMF y preparando la consulta..."):
            prepared = session.prepare(selected_date)
    except Exception as exc:
        try:
            session.close()
        except Exception:
            pass
        st.session_state.cmf_session = None
        st.session_state.cmf_captcha = None
        st.error(f"No pude preparar la consulta CMF: {exc}")
    else:
        st.session_state.cmf_session = session
        st.session_state.cmf_captcha = prepared.image
        st.session_state.cmf_prepared_date = selected_date
        st.rerun()

if st.session_state.cmf_captcha and st.session_state.cmf_prepared_date == selected_date:
    st.markdown('<div class="step"><strong>2 · Resuelve el captcha</strong><br>Este captcha pertenece a la misma sesión que descargará los datos. Escríbelo y la app hará todo lo demás.</div>', unsafe_allow_html=True)
    st.image(st.session_state.cmf_captcha, caption=f"Captcha CMF · consulta {selected_date:%d/%m/%Y}", use_container_width=False)
    captcha_code = st.text_input("Código captcha", placeholder="Escribe el código de la imagen")

    if st.button("Resolver captcha y actualizar", type="primary", width="stretch", disabled=not captcha_code.strip()):
        session = st.session_state.cmf_session
        if session is None:
            st.error("La sesión expiró. Genera un captcha nuevo.")
        else:
            try:
                with st.spinner("Validando captcha, descargando y procesando valores cuota..."):
                    payload, filename = session.submit_captcha(captcha_code)
                    frame = parse_quota_file(payload, filename)
                    validation = validate_quota_file(frame, expected_runs)

                actual_date = pd.Timestamp(validation["latest_date"]).date()
                if actual_date != selected_date:
                    raise ValueError(
                        f"CMF devolvió datos con fecha {actual_date:%d/%m/%Y}, pero pediste {selected_date:%d/%m/%Y}. No se guardó nada."
                    )
                if not validation["ok"]:
                    raise ValueError(
                        f"La descarga no supera los controles: cobertura {validation['coverage']:.1%}; "
                        f"RUN cubiertos {validation['matched_runs']}/{validation['expected_runs']}. No se guardó nada."
                    )

                saved = persist_quota(frame, f"CMF automático · {filename}", validation)
            except Exception as exc:
                st.error(str(exc))
                st.info("Si el captcha estaba mal, pulsa “Generar captcha nuevo” e inténtalo otra vez.")
            else:
                st.session_state.cmf_captcha = None
                st.session_state.cmf_prepared_date = None
                try:
                    session.close()
                except Exception:
                    pass
                st.session_state.cmf_session = None
                st.markdown(
                    f'<div class="done"><strong>✓ Actualización lista</strong><br>{saved["latest_date"]} · '
                    f'{saved["matched_runs"]}/{saved["expected_runs"]} RUN cubiertos · '
                    f'{saved["history_rows"]} filas acumuladas.</div>',
                    unsafe_allow_html=True,
                )
                st.success("No necesitas descargar ni subir ningún archivo.")

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

with st.expander("Qué hace automáticamente"):
    st.write(
        "La aplicación mantiene una sesión de navegador en el servidor, configura la consulta diaria de CMF para la fecha elegida, "
        "te muestra el captcha de esa misma sesión y, una vez que tú lo resuelves, continúa la consulta, recoge la descarga o tabla "
        "de resultados, identifica Fecha/RUN/Valor Cuota, valida cobertura y valores positivos y guarda la actualización. El captcha "
        "siempre lo resuelves tú; la aplicación no intenta eludirlo."
    )
