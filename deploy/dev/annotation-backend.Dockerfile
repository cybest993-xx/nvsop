FROM python:3.10.16-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates curl ffmpeg libgl1 sqlite3 \
    && rm -rf /var/lib/apt/lists/*

COPY vendor/sop-monitoring-blueprints/microservices/sop-training-bp/microservices/video-annotator-ms/annotation_backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir "numpy<2"

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
