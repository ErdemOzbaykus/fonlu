FROM python:3.14-slim

# PYTHONUNBUFFERED olmadan seed/refresh ciktilari tampona takilip
# 'docker compose logs' icinde is bitene kadar gorunmuyor.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Bagimliliklar once: kod degistiginde pip katmani yeniden kurulmasin.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pull edip .env'i elle doldurabilmek icin sablon imajda dursun:
#   docker cp fonlu:/app/.env.example .env
COPY .env.example .

COPY fonlu/ fonlu/
COPY static/ static/
COPY scripts/ scripts/
COPY test_fonlu.py .

RUN useradd --create-home fonlu
USER fonlu

EXPOSE 8000
CMD ["uvicorn", "fonlu.main:app", "--host", "0.0.0.0", "--port", "8000"]
