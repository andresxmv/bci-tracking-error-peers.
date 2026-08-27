from __future__ import annotations

import re
import requests
from urllib.parse import urljoin

FORM = "https://www.cmfchile.cl/institucional/estadisticas/fondos_cartola_diaria.php"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer": FORM,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}

s = requests.Session()
s.headers.update(HEADERS)
try:
    r = s.get(FORM, timeout=30, allow_redirects=True)
    print("CMF_PROBE_FORM", r.status_code, r.url, len(r.content), r.headers.get("content-type"), flush=True)
    print("CMF_PROBE_COOKIES", s.cookies.get_dict(), flush=True)
    r.raise_for_status()
    m = re.search(r'<img[^>]+id=["\']captcha_img["\'][^>]+src=["\']([^"\']+)', r.text, flags=re.I)
    if not m:
        m = re.search(r'<img[^>]+src=["\']([^"\']*captcha[^"\']*)["\']', r.text, flags=re.I)
    print("CMF_PROBE_CAPTCHA_SRC", m.group(1) if m else None, flush=True)
    if m:
        u = urljoin(r.url, m.group(1))
        ir = s.get(u, timeout=30, allow_redirects=True)
        print("CMF_PROBE_IMAGE", ir.status_code, ir.url, len(ir.content), ir.headers.get("content-type"), flush=True)
        ir.raise_for_status()
except Exception as e:
    print("CMF_PROBE_ERROR", type(e).__name__, repr(e), flush=True)
