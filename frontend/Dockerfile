# Build stage
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci
COPY . .
ARG REACT_APP_API_URL=http://localhost:8000
ENV REACT_APP_API_URL=$REACT_APP_API_URL
RUN npm run build

# Production stage
FROM node:20-alpine
WORKDIR /app
RUN npm install -g serve@14
RUN addgroup -S appgroup && adduser -S appuser -G appgroup
COPY --from=build /app/build ./build
RUN chown -R appuser:appgroup /app
USER appuser
EXPOSE 3001
CMD ["serve", "build", "-l", "3001"]
