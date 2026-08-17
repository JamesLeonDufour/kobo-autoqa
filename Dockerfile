FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

RUN useradd -u 10001 -m app && mkdir -p /data && chown app:app /data

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/ ./app/
COPY qual_survey.example.json .

USER app
VOLUME ["/data"]
EXPOSE 8000

CMD ["uvicorn", "app.webhook:app", "--host", "0.0.0.0", "--port", "8000"]
