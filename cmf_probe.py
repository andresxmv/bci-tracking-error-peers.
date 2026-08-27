from __future__ import annotations

from datetime import date

from cmf_automation import CMFQuotaSession

try:
    cmf = CMFQuotaSession()
    prepared = cmf.prepare(date.today())
    print(
        "CMF_PROXY_PROBE_OK",
        len(prepared.image),
        prepared.debug.get("transport"),
        prepared.debug.get("captcha_url"),
        flush=True,
    )
    cmf.close()
except Exception as e:
    print("CMF_PROXY_PROBE_ERROR", type(e).__name__, repr(e), flush=True)
