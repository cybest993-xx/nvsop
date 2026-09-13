FROM node:22.23.2-bookworm-slim AS build

WORKDIR /app

RUN corepack enable && corepack prepare pnpm@11.22.0 --activate
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/control-web/package.json apps/control-web/package.json
RUN pnpm install --frozen-lockfile --filter control-web...

COPY apps/control-web/ apps/control-web/
RUN pnpm --filter control-web run build

FROM nginx:1.27.3-alpine

COPY --from=build /app/apps/control-web/dist /usr/share/nginx/html
COPY deploy/dev/web-nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 8080
