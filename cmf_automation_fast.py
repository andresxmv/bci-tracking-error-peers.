from __future__ import annotations

from datetime import date

from cmf_automation import (
    CMFAutomationError,
    CMFQuotaSession as BaseCMFQuotaSession,
    PreparedCaptcha,
    CMF_URL,
)


class CMFQuotaSession(BaseCMFQuotaSession):
    """CMF browser session with resilient initial navigation.

    CMF's main HTML responds quickly, but third-party resources can keep
    DOMContentLoaded pending in headless Chromium. We therefore wait only for
    the document commit and then for the form itself.
    """

    def prepare(self, target_date: date) -> PreparedCaptcha:
        page = self._ensure_browser()

        # Avoid unrelated third-party resources delaying the legacy CMF page.
        def route_handler(route):
            req = route.request
            url = req.url
            resource_type = req.resource_type
            if resource_type in {"font", "media"}:
                return route.abort()
            if resource_type == "stylesheet" and "cmfchile.cl" not in url:
                return route.abort()
            return route.continue_()

        page.route("**/*", route_handler)

        try:
            # `commit` only waits until the main document response starts.
            # The form itself is the actual readiness condition we need.
            page.goto(CMF_URL, wait_until="commit", timeout=15000)
            page.wait_for_selector("form", state="attached", timeout=15000)
        except Exception as exc:
            self.close()
            raise CMFAutomationError(
                "No pude abrir el formulario de CMF desde el servidor."
            ) from exc

        form = self._choose_daily_form(page)
        self.form = form

        self._select_by_option_text(
            form,
            ("periodicidad", "diaria", "diario"),
            ("diaria", "diario", "día", "dia"),
        )
        self._select_by_option_text(
            form,
            ("tipo", "consulta", "valor", "cuota"),
            ("valor cuota", "valor de la cuota", "cuota"),
        )

        for keywords in (
            ("administradora", "adm"),
            ("tipo", "fondo"),
            ("moneda", "currency"),
        ):
            self._select_by_option_text(
                form, keywords, ("todas", "todos", "total", "--")
            )

        self._select_date(form, target_date)
        self.prepared_date = target_date

        captcha_input = self._captcha_locator(form)
        if captcha_input is None:
            submit = form.locator(
                'input[type="submit"], button[type="submit"], button'
            ).first
            if submit.count():
                try:
                    submit.click(no_wait_after=True)
                    page.wait_for_timeout(1200)
                except Exception:
                    pass
                form = self._choose_daily_form(page)
                self.form = form
                captcha_input = self._captcha_locator(form)

        if captcha_input is None:
            raise CMFAutomationError(
                "No encontré el campo de captcha en la consulta CMF."
            )

        self.captcha_input = captcha_input
        image = self._captcha_image(self.form, captcha_input)
        debug = {
            "url": page.url,
            "fecha": target_date.isoformat(),
            "form_action": self.form.get_attribute("action") if self.form else None,
            "captcha_name": captcha_input.get_attribute("name"),
            "navigation": "commit+form",
        }
        return PreparedCaptcha(image=image, debug=debug)
