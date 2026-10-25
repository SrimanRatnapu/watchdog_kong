# Kong Gateway plus the watchdog-audit Go plugin (plugins/watchdog-audit). Built by compose.yaml.
FROM golang:1.24-alpine AS plugin
WORKDIR /src
COPY plugins/watchdog-audit/go.mod plugins/watchdog-audit/go.sum ./
RUN go mod download
COPY plugins/watchdog-audit/*.go ./
RUN go test ./... && CGO_ENABLED=0 go build -trimpath -o /watchdog-audit .

FROM kong/kong-gateway:3.12
COPY --from=plugin /watchdog-audit /usr/local/bin/watchdog-audit
ENV KONG_PLUGINS=bundled,watchdog-audit \
    KONG_PLUGINSERVER_NAMES=watchdog-audit \
    KONG_PLUGINSERVER_WATCHDOG_AUDIT_START_CMD=/usr/local/bin/watchdog-audit \
    KONG_PLUGINSERVER_WATCHDOG_AUDIT_QUERY_CMD="/usr/local/bin/watchdog-audit -dump"
