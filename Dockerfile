FROM python:3.14-slim

# PYTHONUNBUFFERED olmadan seed/refresh ciktilari tampona takilip
# 'docker compose logs' icinde is bitene kadar gorunmuyor.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FONLU_DB=/data/fonlu.db

WORKDIR /app

# Bagimliliklar once: kod degistiginde pip katmani yeniden kurulmasin.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY fonlu/ fonlu/
COPY static/ static/
COPY test_fonlu.py .

# Veritabani image'a degil volume'e yaziliyor: 140 MB'lik cache'i imaja
# gommenin anlami yok ve yeniden build'de veri kaybolmamali.
RUN useradd --create-home fonlu && mkdir -p /data && chown fonlu:fonlu /data
USER fonlu
VOLUME /data

EXPOSE 8000
CMD ["uvicorn", "fonlu.main:app", "--host", "0.0.0.0", "--port", "8000"]
