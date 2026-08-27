from __future__ import annotations

import os
from datetime import date

import cmf_automation
from cmf_automation_fast import CMFQuotaSession

# Flask imports CMFQuotaSession from cmf_automation. Patch it before importing
# the application so both prepare and submit use the resilient implementation.
cmf_automation.CMFQuotaSession = CMFQuotaSession

# Production preflight: fail fast if the exact operation used by the UI cannot
# reach and parse the CMF form/captcha.
probe = CMFQuotaSession()
try:
    prepared = probe.prepare(date(2026, 8, 25))
    print(
        "CMF PREPARE OK",
        len(prepared.image),
        prepared.debug,
        flush=True,
    )
finally:
    probe.close()

from flask_app_v2 import app  # noqa: E402

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        threaded=False,
        use_reloader=False,
        debug=False,
    )
