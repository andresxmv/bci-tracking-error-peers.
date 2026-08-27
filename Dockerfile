FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium

COPY . .

EXPOSE 8080

CMD ["sh", "-c", "python -c \"from datetime import date; from cmf_automation import CMFQuotaSession; s=CMFQuotaSession(); r=s.prepare(date(2026,8,25)); print('CMF prepare OK', len(r.image), r.debug); s.close()\" && python -c \"import os; from flask_app_v2 import app; app.run(host='0.0.0.0', port=int(os.environ.get('PORT','8080')), threaded=False, use_reloader=False, debug=False)\""]
