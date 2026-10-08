FROM python:3.10.16-slim-bookworm@sha256:f9fd9a142c9e3bc54d906053b756eb7e7e386ee1cf784d82c251cf640c502512

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates curl ffmpeg libgl1 sqlite3 \
    && rm -rf /var/lib/apt/lists/*

COPY deploy/dev/annotation-requirements.lock ./requirements.txt
RUN pip install --no-cache-dir --no-deps --no-build-isolation -r requirements.txt \
    && pip check

COPY vendor/sop-monitoring-blueprints/microservices/sop-training-bp/microservices/video-annotator-ms/annotation_backend/inference.py ./
COPY vendor/sop-monitoring-blueprints/microservices/sop-training-bp/microservices/video-annotator-ms/annotation_backend/run.sh ./
COPY vendor/sop-monitoring-blueprints/microservices/sop-training-bp/microservices/video-annotator-ms/annotation_backend/utils ./utils
COPY vendor/sop-monitoring-blueprints/microservices/sop-training-bp/microservices/video-annotator-ms/annotation_backend/validations ./validations
COPY vendor/sop-monitoring-blueprints/microservices/sop-training-bp/microservices/video-annotator-ms/annotation_backend/components ./components
COPY vendor/sop-monitoring-blueprints/microservices/sop-training-bp/microservices/video-annotator-ms/annotation_backend/exceptions ./exceptions
COPY deploy/dev/annotation-entrypoint.sh /usr/local/bin/annotation-entrypoint

RUN chmod +x /app/run.sh /usr/local/bin/annotation-entrypoint

EXPOSE 8100
ENTRYPOINT ["/usr/local/bin/annotation-entrypoint"]
