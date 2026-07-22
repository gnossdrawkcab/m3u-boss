FROM golang:1.26-alpine AS build
WORKDIR /src
COPY go.mod ./
COPY *.go ./
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/m3u-edge .

FROM alpine:3.23
RUN apk add --no-cache ca-certificates
RUN adduser -S -D -H -u 10001 m3uboss \
    && mkdir -p /app/exports /app/cache \
    && chown -R m3uboss:m3uboss /app
COPY --from=build /out/m3u-edge /usr/local/bin/m3u-edge
USER m3uboss
EXPOSE 43817
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:43817/healthz || exit 1
ENTRYPOINT ["/usr/local/bin/m3u-edge"]
