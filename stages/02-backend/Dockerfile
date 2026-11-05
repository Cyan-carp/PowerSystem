FROM golang:1.24-alpine AS build
ARG GOPROXY=https://goproxy.cn,direct
ENV GOPROXY=${GOPROXY}
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
ARG APP=api
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/server ./cmd/${APP}
# 一并构建一次性真值修正二进制，随 API 镜像交付；运行时不依赖 Go 编译器。
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/prediction-label-fix ./cmd/prediction-label-fix

FROM alpine:3.21
RUN apk add --no-cache ca-certificates
WORKDIR /app
COPY --from=build /out/server ./server
COPY --from=build /out/prediction-label-fix ./prediction-label-fix
COPY deploy ./deploy
COPY test ./test
ENTRYPOINT ["/app/server"]
