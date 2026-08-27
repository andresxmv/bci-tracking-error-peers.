from __future__ import annotations

import os
import sys

# Railway's legacy service still starts this repository with
# `streamlit run app.py`. Replace that process with the authoritative Flask v5
# application so the deployed service uses the corrected calendar-YTD logic.
print("APP_BOOTSTRAP_TO_FLASK_V5", flush=True)
os.execv(sys.executable, [sys.executable, "flask_app_v5.py"])
