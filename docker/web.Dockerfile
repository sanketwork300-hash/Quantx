# syntax=docker/dockerfile:1
FROM node:22-alpine AS deps
WORKDIR /app
COPY web/package.json web/package-lock.json* ./
RUN npm install

FROM node:22-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY web/ ./
ARG NEXT_PUBLIC_API_BASE_URL=http://localhost:8000/api/v1
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL
RUN npm run build

FROM node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
RUN addgroup -g 10001 -S qip && adduser -S -u 10001 -G qip qip
COPY --from=builder --chown=qip:qip /app/.next/standalone ./
COPY --from=builder --chown=qip:qip /app/.next/static ./.next/static
COPY --from=builder --chown=qip:qip /app/public ./public
USER qip
EXPOSE 3000
CMD ["node", "server.js"]
