FROM node:22.23.2-bookworm-slim AS build

WORKDIR /app
RUN corepack enable

COPY vendor/sop-monitoring-blueprints/microservices/sop-training-bp/microservices/video-annotator-ms/annotation_frontend/package.json ./
COPY vendor/sop-monitoring-blueprints/microservices/sop-training-bp/microservices/video-annotator-ms/annotation_frontend/package-lock.json ./
RUN npm ci --ignore-scripts

COPY vendor/sop-monitoring-blueprints/microservices/sop-training-bp/microservices/video-annotator-ms/annotation_frontend/ ./
RUN npm run build

FROM nginx:1.27.3-alpine
COPY --from=build /app/build /usr/share/nginx/html
COPY deploy/dev/annotation-nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
