# Imagen del frontend público. Multi-stage: node:20 construye, el runtime sólo
# lleva el `dist/` y las dependencias de producción.
FROM node:20-slim AS build

WORKDIR /app

# Las dependencias se copian antes que el código para que el `npm ci` se cachee
# entre builds mientras el lockfile no cambie.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

# ── Runtime ──
FROM node:20-slim

ENV NODE_ENV=production \
    HOST=0.0.0.0 \
    PORT=3000

WORKDIR /app

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force

COPY --from=build /app/dist ./dist

# Usuario sin privilegios: nada de esto necesita root en runtime.
RUN useradd --create-home --uid 10002 centinela && chown -R centinela:centinela /app
USER centinela

EXPOSE 3000

# `API_READ_KEY` y `API_BASE_URL` llegan por entorno, nunca horneadas en la
# imagen: rotar la llave no debe exigir reconstruir.
CMD ["node", "./dist/server/entry.mjs"]
