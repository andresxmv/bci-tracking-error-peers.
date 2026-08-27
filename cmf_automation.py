from __future__ import annotations

import base64
import os
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
CMF_PROXY_URL = os.getenv(
    "CMF_PROXY_URL",
    "https://nusycxhrfynrrbvdiiko.supabase.co/functions/v1/cmf-cartola-proxy",
).strip()
CMF_PROXY_KEY = os.getenv("CMF_PROXY_KEY", "bci-tracking-error-peers-v1").strip()


class CMFAutomationError(RuntimeError):
    pass


@dataclass
class PreparedCaptcha:
    image: bytes
    debug: dict[str, Any]


class CMFQuotaSession:
    """Sesión CMF con proxy serverless opcional.

    Railway tiene egress bloqueado hacia cmfchile.cl en algunos rangos. Cuando
    CMF_PROXY_URL está configurado, el proxy mantiene la sesión lógica mediante
    las cookies que devuelve en prepare y recibe nuevamente en submit.
    """

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(CMF_HEADERS)
        self.prepared_date: date | None = None
        self.proxy_cookies: str = ""

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

    def _proxy_post(self, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
        headers = {"x-proxy-key": CMF_PROXY_KEY}
        try:
            response = requests.post(CMF_PROXY_URL, json=payload, headers=headers, timeout=timeout)
            data = response.json()
        except Exception as exc:
            raise CMFAutomationError(f"No pude comunicarme con el puente CMF: {exc}") from exc
        if not response.ok or not data.get("ok"):
            detail = data.get("detail") or data.get("validate_text") or data.get("download_head") or ""
            error = data.get("error") or f"HTTP {response.status_code}"
            if error == "captcha_rejected":
                raise CMFAutomationError("Captcha CMF rechazado. Vuelve a prepararlo e inténtalo de nuevo.")
            raise CMFAutomationError(f"Puente CMF: {error}{' · ' + str(detail)[:180] if detail else ''}")
        return data

    def prepare(self, target_date: date) -> PreparedCaptcha:
        if CMF_PROXY_URL and CMF_PROXY_KEY:
            data = self._proxy_post({"action": "prepare"}, timeout=70)
            try:
                image = base64.b64decode(data["image_b64"])
            except Exception as exc:
                raise CMFAutomationError("El puente CMF devolvió un captcha inválido.") from exc
            if not image:
                raise CMFAutomationError("El puente CMF devolvió un captcha vacío.")
            self.proxy_cookies = str(data.get("cookies") or "")
            self.prepared_date = target_date
            return PreparedCaptcha(
                image=image,
                debug={
                    "url": data.get("form_url") or CMF_FORM,
                    "fecha": target_date.isoformat(),
                    "captcha_url": data.get("image_url"),
                    "transport": "serverless-proxy",
                },
            )

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
            debug={"url": CMF_FORM, "fecha": target_date.isoformat(), "captcha_url": image_url, "transport": "requests-session"},
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

        target = self.prepared_date
        if CMF_PROXY_URL and CMF_PROXY_KEY:
            data = self._proxy_post(
                {"action": "submit", "cookies": self.proxy_cookies, "code": code, "date": target.isoformat()},
                timeout=150,
            )
            try:
                content = base64.b64decode(data["file_b64"])
            except Exception as exc:
                raise CMFAutomationError("El puente CMF devolvió una cartola inválida.") from exc
            if not content or b"RUN_FM" not in content[:500]:
                raise CMFAutomationError("El puente CMF respondió sin una cartola válida.")
            return content, str(data.get("filename") or f"cartola_todos_{target:%Y%m%d}_{target:%Y%m%d}.txt")

        try:
            if not self._validate_captcha(code):
                raise CMFAutomationError("Captcha CMF rechazado. Vuelve a prepararlo e inténtalo de nuevo.")
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
        return content, f"cartola_todos_{target:%Y%m%d}_{target:%Y%m%d}.txt"
