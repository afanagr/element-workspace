# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY requirements.txt requirements-local.txt ./
ARG INSTALL_LOCAL_EMBEDDINGS=1
RUN pip install --upgrade pip setuptools wheel \
    && pip install -r requirements.txt \
    && if [ "$INSTALL_LOCAL_EMBEDDINGS" = "1" ]; then \
         pip install --index-url https://download.pytorch.org/whl/cpu torch==2.5.1 ; \
         pip install -r requirements-local.txt ; \
       fi

FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    HF_HOME=/data/models \
    SENTENCE_TRANSFORMERS_HOME=/data/models \
    TESSERACT_LANG=eng+rus

# tesseract: image and scanned-PDF OCR; antiword/catdoc: legacy .doc extraction
# tessdata_best models are noticeably more accurate than the fast ones shipped
# by Debian, at the cost of a larger tessdata directory.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-eng tesseract-ocr-rus tesseract-ocr-osd \
        antiword catdoc curl fonts-dejavu-core \
    && TESSDATA="$(find /usr/share/tesseract-ocr -maxdepth 2 -type d -name tessdata | head -n1)" \
    && if [ -n "$TESSDATA" ]; then \
         (curl -fsSL https://github.com/tesseract-ocr/tessdata_best/raw/main/eng.traineddata -o /tmp/eng.traineddata \
            && mv /tmp/eng.traineddata "$TESSDATA/eng.traineddata") || echo "[ocr] eng best model not downloaded"; \
         (curl -fsSL https://github.com/tesseract-ocr/tessdata_best/raw/main/rus.traineddata -o /tmp/rus.traineddata \
            && mv /tmp/rus.traineddata "$TESSDATA/rus.traineddata") || echo "[ocr] rus best model not downloaded"; \
       fi \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

# PDF rasteriser for OCR. Installed separately so the heavy builder layer
# (torch/embeddings) stays cached when this small dependency changes.
COPY requirements-ocr.txt ./
RUN pip install --no-cache-dir -r requirements-ocr.txt

WORKDIR /app
COPY . .

RUN useradd -u 1000 -m app \
    && mkdir -p /data/workspace/blobs /data/models \
    && chown -R app:app /data /app

USER app

EXPOSE 8000

# ---- API image -------------------------------------------------------------
FROM runtime AS api
CMD ["python", "-m", "app.entrypoint"]

# ---- Celery worker image (same deps, different command) --------------------
FROM runtime AS worker
CMD ["celery", "-A", "app.worker.celery_app:celery_app", "worker", "--loglevel=INFO", "--concurrency=2"]
