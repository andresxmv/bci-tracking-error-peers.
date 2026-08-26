from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, TimeoutError as PlaywrightTimeoutError, sync_playwright

CMF_URL = "https://www.cmfchile.cl/institucional/estadisticas/fm.bpr_menu.php"


class CMFAutomationError(RuntimeError):
    pass


@dataclass
class PreparedCaptcha:
    image: bytes
    debug: dict[str, Any]


class CMFQuotaSession:
    """Human-in-the-loop browser session for CMF.

    The user solves the captcha. The browser session is kept server-side so the
    same cookies/session can continue the request and collect the result.
    """

    def __init__(self) -> None:
        self.pw: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.form = None
        self.captcha_input = None
        self.prepared_date: date | None = None

    def close(self) -> None:
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass
        try:
            if self.browser:
                self.browser.close()
        except Exception:
            pass
        try:
            if self.pw:
                self.pw.stop()
        except Exception:
            pass
        self.pw = self.browser = self.context = self.page = self.form = self.captcha_input = None

    def _ensure_browser(self) -> Page:
        self.close()
        self.pw = sync_playwright().start()
        try:
            self.browser = self.pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
        except Exception as exc:
            self.close()
            raise CMFAutomationError(
                "No pude iniciar Chromium en el servidor. Revisa que Playwright/Chromium esté instalado."
            ) from exc
        self.context = self.browser.new_context(
            accept_downloads=True,
            locale="es-CL",
            timezone_id="America/Santiago",
            viewport={"width": 1280, "height": 1400},
        )
        self.page = self.context.new_page()
        self.page.set_default_timeout(15000)
        return self.page

    @staticmethod
    def _norm(text: str | None) -> str:
        return re.sub(r"\s+", " ", (text or "").strip().lower())

    def _choose_daily_form(self, page: Page):
        forms = page.locator("form")
        best = None
        best_score = -1
        for i in range(forms.count()):
            form = forms.nth(i)
            txt = self._norm(form.inner_text())
            score = 0
            for token in ("periodicidad", "tipo de consulta", "seleccione dia", "valor", "cuota"):
                if token in txt:
                    score += 1
            if score > best_score:
                best, best_score = form, score
        if best is None:
            raise CMFAutomationError("CMF no expuso el formulario esperado de fondos mutuos.")
        return best

    def _select_by_option_text(self, form, keywords: tuple[str, ...], preferred: tuple[str, ...]) -> bool:
        selects = form.locator("select")
        for i in range(selects.count()):
            sel = selects.nth(i)
            name = self._norm(sel.get_attribute("name"))
            sid = self._norm(sel.get_attribute("id"))
            opts = sel.locator("option")
            option_texts = [self._norm(opts.nth(j).inner_text()) for j in range(opts.count())]
            haystack = " ".join([name, sid] + option_texts)
            if not any(k in haystack for k in keywords):
                continue
            for pref in preferred:
                for j, txt in enumerate(option_texts):
                    if pref in txt:
                        value = opts.nth(j).get_attribute("value")
                        if value is not None:
                            sel.select_option(value=value)
                        else:
                            sel.select_option(index=j)
                        return True
        return False

    def _select_date(self, form, target: date) -> None:
        selects = form.locator("select")
        day_done = month_done = year_done = False
        for i in range(selects.count()):
            sel = selects.nth(i)
            name = self._norm(sel.get_attribute("name"))
            sid = self._norm(sel.get_attribute("id"))
            key = f"{name} {sid}"
            opts = sel.locator("option")
            texts = [self._norm(opts.nth(j).inner_text()) for j in range(opts.count())]
            vals = [self._norm(opts.nth(j).get_attribute("value")) for j in range(opts.count())]

            def choose(candidates: tuple[str, ...]) -> bool:
                for cand in candidates:
                    for j, (txt, val) in enumerate(zip(texts, vals)):
                        if txt == cand or val == cand:
                            v = opts.nth(j).get_attribute("value")
                            if v is not None:
                                sel.select_option(value=v)
                            else:
                                sel.select_option(index=j)
                            return True
                return False

            if not year_done and ("ano" in key or "anio" in key or "year" in key or str(target.year) in texts or str(target.year) in vals):
                year_done = choose((str(target.year),)) or year_done
                if year_done:
                    continue
            if not month_done and ("mes" in key or "month" in key):
                month_names = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre")
                month_done = choose((str(target.month), f"{target.month:02d}", month_names[target.month - 1])) or month_done
                if month_done:
                    continue
            if not day_done and ("dia" in key or "day" in key):
                day_done = choose((str(target.day), f"{target.day:02d}")) or day_done

        # Fallback by option domains for legacy forms with opaque names.
        if not year_done or not month_done or not day_done:
            for i in range(selects.count()):
                sel = selects.nth(i)
                opts = sel.locator("option")
                vals = [self._norm(opts.nth(j).get_attribute("value")) for j in range(opts.count())]
                texts = [self._norm(opts.nth(j).inner_text()) for j in range(opts.count())]
                universe = set(vals + texts)
                if not year_done and str(target.year) in universe:
                    try:
                        sel.select_option(label=str(target.year))
                    except Exception:
                        sel.select_option(value=str(target.year))
                    year_done = True
                    continue
                if not month_done and ({str(target.month), f"{target.month:02d}"} & universe):
                    candidate = str(target.month) if str(target.month) in universe else f"{target.month:02d}"
                    try:
                        sel.select_option(value=candidate)
                    except Exception:
                        sel.select_option(label=candidate)
                    month_done = True
                    continue
                if not day_done and ({str(target.day), f"{target.day:02d}"} & universe):
                    candidate = str(target.day) if str(target.day) in universe else f"{target.day:02d}"
                    try:
                        sel.select_option(value=candidate)
                    except Exception:
                        sel.select_option(label=candidate)
                    day_done = True

        if not (year_done and month_done and day_done):
            raise CMFAutomationError(
                f"No pude fijar completamente la fecha {target:%d-%m-%Y} en el formulario de CMF."
            )

    def _captcha_locator(self, form):
        inputs = form.locator("input")
        for i in range(inputs.count()):
            inp = inputs.nth(i)
            typ = self._norm(inp.get_attribute("type"))
            if typ not in ("", "text", "number"):
                continue
            key = " ".join(
                self._norm(inp.get_attribute(a)) for a in ("name", "id", "placeholder", "aria-label")
            )
            if any(k in key for k in ("captcha", "codigo", "código", "verifica", "seguridad")):
                return inp
        # Legacy fallback: last visible text input in the selected form.
        visible = []
        for i in range(inputs.count()):
            inp = inputs.nth(i)
            if self._norm(inp.get_attribute("type")) in ("", "text") and inp.is_visible():
                visible.append(inp)
        return visible[-1] if visible else None

    def _captcha_image(self, form, captcha_input) -> bytes:
        imgs = form.locator("img")
        scored: list[tuple[int, Any]] = []
        for i in range(imgs.count()):
            img = imgs.nth(i)
            src = self._norm(img.get_attribute("src"))
            alt = self._norm(img.get_attribute("alt"))
            score = 0
            if any(k in src for k in ("captcha", "codigo", "verifica", "seguridad")):
                score += 4
            if any(k in alt for k in ("captcha", "codigo", "verifica", "seguridad")):
                score += 3
            if img.is_visible():
                score += 1
            scored.append((score, img))
        if scored:
            scored.sort(key=lambda x: x[0], reverse=True)
            if scored[0][0] > 0:
                return scored[0][1].screenshot(type="png")

        # If the captcha is rendered as background/canvas, crop a region around the input.
        box = captcha_input.bounding_box() if captcha_input else None
        if box:
            page = self.page
            assert page is not None
            x = max(0, box["x"] - 340)
            y = max(0, box["y"] - 150)
            w = min(680, 1280 - x)
            h = min(260, 1400 - y)
            return page.screenshot(type="png", clip={"x": x, "y": y, "width": w, "height": h})
        return form.screenshot(type="png")

    def prepare(self, target_date: date) -> PreparedCaptcha:
        page = self._ensure_browser()
        try:
            page.goto(CMF_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            self.close()
            raise CMFAutomationError("No pude abrir la consulta de CMF desde el servidor.") from exc

        form = self._choose_daily_form(page)
        self.form = form

        # Prefer daily periodicity and a quota/value-related query. Legacy page labels vary.
        self._select_by_option_text(form, ("periodicidad", "diaria", "diario"), ("diaria", "diario", "día", "dia"))
        self._select_by_option_text(form, ("tipo", "consulta", "valor", "cuota"), ("valor cuota", "valor de la cuota", "cuota"))

        # Broad filters: all managers/fund types/currencies whenever those controls exist.
        for keywords in (("administradora", "adm"), ("tipo", "fondo"), ("moneda", "currency")):
            self._select_by_option_text(form, keywords, ("todas", "todos", "total", "--"))

        self._select_date(form, target_date)
        self.prepared_date = target_date

        captcha_input = self._captcha_locator(form)
        if captcha_input is None:
            # Some legacy forms reveal captcha only after a first submit/continue.
            submit = form.locator('input[type="submit"], button[type="submit"], button').first
            if submit.count():
                try:
                    submit.click()
                    page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                form = self._choose_daily_form(page)
                self.form = form
                captcha_input = self._captcha_locator(form)
        if captcha_input is None:
            raise CMFAutomationError("No encontré el campo de captcha en la consulta CMF.")

        self.captcha_input = captcha_input
        image = self._captcha_image(self.form, captcha_input)
        debug = {
            "url": page.url,
            "fecha": target_date.isoformat(),
            "form_action": self.form.get_attribute("action") if self.form else None,
            "captcha_name": captcha_input.get_attribute("name"),
        }
        return PreparedCaptcha(image=image, debug=debug)

    def _read_result_from_page(self, page: Page) -> tuple[bytes, str]:
        # First preference: a visible download/export link.
        links = page.locator("a")
        candidates = []
        for i in range(links.count()):
            a = links.nth(i)
            text = self._norm(a.inner_text())
            href = self._norm(a.get_attribute("href"))
            score = 0
            if any(k in text for k in ("excel", "csv", "descargar", "exportar", "xls")):
                score += 3
            if any(ext in href for ext in (".csv", ".xls", ".xlsx", "excel", "export")):
                score += 2
            if score:
                candidates.append((score, a))
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            for _, a in candidates:
                try:
                    with page.expect_download(timeout=15000) as di:
                        a.click()
                    download = di.value
                    path = download.path()
                    if path:
                        return Path(path).read_bytes(), download.suggested_filename or "cmf_valores_cuota.xlsx"
                except Exception:
                    continue

        # Second preference: parse result tables and convert the most data-rich one to CSV.
        html = page.content()
        try:
            tables = pd.read_html(io.StringIO(html))
        except Exception as exc:
            raise CMFAutomationError("CMF respondió, pero no encontré una descarga ni una tabla de resultados utilizable.") from exc
        tables = [t for t in tables if t.shape[0] >= 2 and t.shape[1] >= 3]
        if not tables:
            raise CMFAutomationError("CMF respondió sin una tabla de resultados utilizable.")
        table = max(tables, key=lambda t: t.shape[0] * t.shape[1])
        return table.to_csv(index=False).encode("utf-8-sig"), "cmf_valores_cuota.csv"

    def submit_captcha(self, code: str) -> tuple[bytes, str]:
        if not self.page or not self.form or not self.captcha_input or not self.prepared_date:
            raise CMFAutomationError("La sesión CMF no está preparada. Vuelve a elegir la fecha.")
        code = (code or "").strip()
        if not code:
            raise CMFAutomationError("Ingresa el código del captcha.")

        page = self.page
        self.captcha_input.fill(code)
        submit = self.form.locator('input[type="submit"], button[type="submit"], button').first
        if not submit.count():
            raise CMFAutomationError("No encontré el botón de consulta de CMF.")

        direct_download = None
        try:
            with page.expect_download(timeout=5000) as di:
                submit.click()
            direct_download = di.value
        except PlaywrightTimeoutError:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
        except Exception as exc:
            raise CMFAutomationError("CMF no aceptó la consulta. Revisa el captcha y vuelve a intentar.") from exc

        if direct_download is not None:
            path = direct_download.path()
            if path:
                return Path(path).read_bytes(), direct_download.suggested_filename or "cmf_valores_cuota.xlsx"

        body_text = self._norm(page.locator("body").inner_text())
        if any(msg in body_text for msg in ("captcha incorrect", "codigo incorrect", "código incorrect", "verificacion incorrect")):
            raise CMFAutomationError("El captcha fue rechazado por CMF. Genera uno nuevo y vuelve a intentarlo.")

        return self._read_result_from_page(page)
