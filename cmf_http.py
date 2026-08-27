from __future__ import annotations

from datetime import date
from urllib.parse import urlencode
from urllib.request import Request, urlopen

CMF_DAILY_URL = "https://www.cmfchile.cl/institucional/estadisticas/fm.fm_bpr_dia.php"
USER_AGENT = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1"


class CMFHTTPError(RuntimeError):
    pass


def download_daily_quota(target: date) -> tuple[bytes, str, str]:
    params = {
        "admins": "0",
        "tipofondo": "0",
        "moneda": "0",
        "dia_select": str(target.day),
        "mes_peri": f"{target.month:02d}",
        "anio_peri": str(target.year),
        "out": "excel",
        "lang": "es",
    }
    url = CMF_DAILY_URL + "?" + urlencode(params)
    req = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,text/html;q=0.9,*/*;q=0.8",
            "Referer": "https://www.cmfchile.cl/institucional/estadisticas/fm.bpr_menu.php",
        },
    )
    try:
        with urlopen(req, timeout=45) as resp:
            payload = resp.read()
            content_type = (resp.headers.get("Content-Type") or "").lower()
            disposition = resp.headers.get("Content-Disposition") or ""
    except Exception as exc:
        raise CMFHTTPError(f"No pude descargar los valores cuota desde CMF: {exc}") from exc

    if len(payload) < 100:
        raise CMFHTTPError("CMF devolvió una respuesta demasiado corta.")

    # Detecta mensajes HTML de error. Un HTML con tabla sí puede ser una exportación Excel válida.
    low = payload[:1000].lstrip().lower()
    if (low.startswith(b"<html") or low.startswith(b"<!doctype")) and b"<table" not in payload[:10000].lower():
        text = payload[:3000].decode("latin-1", errors="ignore")
        raise CMFHTTPError("CMF no devolvió el archivo de cuotas. Respuesta: " + " ".join(text.split())[:300])

    filename = f"cmf_valores_cuota_{target:%Y%m%d}.xls"
    if "filename=" in disposition.lower():
        raw = disposition.split("filename=", 1)[1].strip().strip('"')
        if raw:
            filename = raw
    if "spreadsheetml" in content_type and not filename.lower().endswith(".xlsx"):
        filename = filename.rsplit(".", 1)[0] + ".xlsx"
    return payload, filename, url
