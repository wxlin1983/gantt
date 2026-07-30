# Frontend image: build the bundle, then serve it from nginx.
#
# nginx also reverse-proxies /api, so the browser sees one origin and the
# session cookie works without CORS or SameSite exceptions.

FROM node:20-slim AS build

# pnpm 11 reads build-script approval from pnpm-workspace.yaml; esbuild ships a
# prebuilt binary and Vite cannot start without it.
RUN corepack enable && corepack prepare pnpm@10 --activate

WORKDIR /web
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

COPY tsconfig.json vite.config.ts index.html ./
COPY src ./src
RUN pnpm build


FROM nginx:1.27-alpine AS runtime

COPY --from=build /web/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
