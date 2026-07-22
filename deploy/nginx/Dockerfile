FROM node:22.14.0-alpine@sha256:9bef0ef1e268f60627da9ba7d7605e8831d5b56ad07487d24d1aa386336d1944 AS frontend-build

WORKDIR /opt/beyondcandidate/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --fund=false --audit=false
COPY frontend/index.html frontend/vite.config.js frontend/vite.config.mjs ./
COPY frontend/public ./public
COPY frontend/src ./src
RUN npm run build

FROM nginx:1.28.0-alpine@sha256:30f1c0d78e0ad60901648be663a710bdadf19e4c10ac6782c235200619158284

COPY --from=frontend-build /opt/beyondcandidate/frontend/dist /usr/share/nginx/html
