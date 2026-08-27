from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import urljoin

import requests

CMF_BASE = "https://www.cmfchile.cl"
CMF_FORM = f"{CMF_BASE}/institucional/estadisticas/fondos_cartola_diaria.php"
CMF_CAPTCHA_VALIDATE = f"{CMF_BASE}/sitio/biblioteca/captcha2/captcha.php"
CMF_DOWNLOAD = f"{CMF_BASE}/institucional/estadisticas/cfm_download.php"
CMF_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": CMF_FORM,
}


class CMFAutomationError(RuntimeError):
    pass


@dataclass
class PreparedCaptcha:
    image: bytes
    debug: dict[str, Any]


class CMFQuotaSession:
    """Sesión HTTP CMF sin Playwright/Chromium.

    Mantiene las mismas cookies entre captcha, validación y descarga de la
    cartola diaria, tal como en el proyecto original.
    """

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(CMF_HEADERS)
        self.prepared_date: date | None = None

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

    @staticmethod
    def _captcha_src(html: str) -> str:
        for pattern in (
            r'<img[^>]+id=["\']captcha_img["\'][^>]+src=["\']([^"\']+)',
            r'<img[^>]+src=["\']([^"\']*captcha[^"\']*)["\']',
        ):
            match = re.search(pattern, html, flags=re.I)
            if match:
                return match.group(1)
        raise CMFAutomationError("CMF respondió, pero no encontré la imagen del captcha.")

    def prepare(self, target_date: date) -> PreparedCaptcha:
        try:
            response = self.session.get(CMF_FORM, timeout=60)
            response.raise_for_status()
            src = self._captcha_src(response.text)
            image_url = urljoin(CMF_BASE, src)
            image_response = self.session.get(image_url, timeout=30)
            image_response.raise_for_status()
        except CMFAutomationError:
            raise
        except Exception as exc:
            raise CMFAutomationError("No pude abrir la cartola diaria de CMF desde el servidor.") from exc

        if not image_response.content:
            raise CMFAutomationError("CMF devolvió un captcha vacío.")

        self.prepared_date = target_date
        return PreparedCaptcha(
            image=image_response.content,
            debug={
                "url": CMF_FORM,
                "fecha": target_date.isoformat(),
                "captcha_url": image_url,
                "transport": "requests-session",
            },
        )

    def _validate_captcha(self, code: str) -> bool:
        response = self.session.post(
            CMF_CAPTCHA_VALIDATE,
            data={"accion": "valida", "valor": code},
            timeout=30,
        )
        response.raise_for_status()
        return response.text.strip() == "1"

    def submit_captcha(self, code: str) -> tuple[bytes, str]:
        if self.prepared_date is None:
            raise CMFAutomationError("La sesión CMF no está preparada. Vuelve a elegir la fecha.")
        code = (code or "").strip()
        if not code:
            raise CMFAutomationError("Ingresa el código del captcha.")

        try:
            if not self._validate_captcha(code):
                raise CMFAutomationError("Captcha CMF rechazado. Vuelve a prepararlo e inténtalo de nuevo.")

            target = self.prepared_date
            payload = {
                "ffmm": "%",
                "txt_inicio": f"{target:%d/%m/%Y}",
                "txt_termino": f"{target:%d/%m/%Y}",
                "enviar": "Buscar",
                "btnConsulta": "GENERAR ARCHIVO",
                "captcha": code,
            }
            response = self.session.post(CMF_DOWNLOAD, data=payload, timeout=300)
            response.raise_for_status()
        except CMFAutomationError:
            raise
        except Exception as exc:
            raise CMFAutomationError("CMF no pudo generar la cartola diaria.") from exc

        content_type = (response.headers.get("content-type") or "").lower()
        content = response.content
        if "html" in content_type or not content or b"RUN_FM" not in content[:500]:
            raise CMFAutomationError("CMF respondió sin una cartola válida. Revisa el captcha y la fecha.")

        target = self.prepared_date
        return content, f"cartola_todos_{target:%Y%m%d}_{target:%Y%m%d}.txt"
