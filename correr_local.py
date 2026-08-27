# -*- coding: utf-8 -*-
# =====================================================================
#   DASHBOARD EN LOCAL  ·  aprieta Play
#
#   Levanta la misma aplicacion que corre en Railway, pero en tu maquina.
#   Cada vez que guardas un .py o un .html la pagina se actualiza sola:
#   no hay que desplegar para ver un cambio.
#
#       py -3 correr_local.py
#       -> http://127.0.0.1:8000
#
#   Para salir: Ctrl+C en la consola.
# =====================================================================

PUERTO = 8000
RECARGA_AUTOMATICA = True    # False si prefieres reiniciar a mano
ABRIR_NAVEGADOR = True
# =====================================================================

import os
import sys
import threading
import webbrowser
from pathlib import Path

AQUI = Path(__file__).resolve().parent
os.chdir(AQUI)
sys.path.insert(0, str(AQUI))

# Los datos que cargues en local quedan aqui, no en /app/runtime_data (que es
# la ruta del contenedor y en Windows no existe). Asi puedes probar el flujo de
# /actualizar sin tocar produccion.
DATOS_LOCALES = AQUI / "runtime_data"
DATOS_LOCALES.mkdir(exist_ok=True)
os.environ.setdefault("DATA_DIR", str(DATOS_LOCALES))

# Sesion estable entre reinicios: si la clave cambia, el login se pierde en cada
# recarga y hay que volver a poner el PIN.
os.environ.setdefault("FLASK_SECRET_KEY", "desarrollo-local-no-usar-en-produccion")

faltan = []
for modulo, paquete in [("flask", "flask"), ("pandas", "pandas"), ("numpy", "numpy"),
                        ("requests", "requests")]:
    try:
        __import__(modulo)
    except ImportError:
        faltan.append(paquete)
if faltan:
    print("=" * 68)
    print("FALTAN LIBRERIAS EN ESTE PYTHON")
    print("=" * 68)
    print(f"Python en uso:\n    {sys.executable}\n")
    print("Instalalas con:")
    print(f'    "{sys.executable}" -m pip install ' + " ".join(faltan))
    raise SystemExit(1)

print("Cargando metricas...", flush=True)
import flask_app_v5 as v5  # noqa: E402

app = v5.app
# Con esto un cambio en templates/*.html se ve recargando el navegador,
# sin reiniciar el proceso.
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

URL = f"http://127.0.0.1:{PUERTO}"
PIN = os.getenv("ADMIN_PIN", "1405")

# El reloader arranca un proceso hijo; sin esta guarda el navegador se abriria
# dos veces y el mensaje saldria duplicado.
if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
    print()
    print("=" * 68)
    print("  DASHBOARD LOCAL")
    print("=" * 68)
    print(f"  {URL}")
    print(f"  admin:  {URL}/login   (PIN {PIN})")
    print(f"  salud:  {URL}/health")
    print()
    print(f"  Datos de runtime: {DATOS_LOCALES}")
    print(f"  Recarga automatica: {'si' if RECARGA_AUTOMATICA else 'no'}")
    print("  Ctrl+C para salir.")
    print("=" * 68)
    print()
    if ABRIR_NAVEGADOR:
        threading.Timer(1.5, lambda: webbrowser.open(URL)).start()

if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=PUERTO,
        debug=RECARGA_AUTOMATICA,
        use_reloader=RECARGA_AUTOMATICA,
        threaded=True,
    )
