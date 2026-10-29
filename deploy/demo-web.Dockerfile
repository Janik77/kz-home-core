FROM node:22-bookworm-slim AS build
WORKDIR /web
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# Public build inputs only; never put credentials in this stage or VITE variables.
ENV VITE_DATA_MODE=core VITE_API_URL=/core-api
RUN npm run build

FROM nginx:1.28-alpine
COPY --from=build /web/dist/ /usr/share/nginx/html/
