FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN adduser --disabled-password --gecos "" appuser \
    && mkdir -p /var/log/aquira \
    && chown -R appuser:appuser /var/log/aquira /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY README.md ./README.md

USER appuser
EXPOSE 8080

CMD ["python", "-m", "app"]
