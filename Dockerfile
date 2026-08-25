FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        unzip \
    && rm -rf /var/lib/apt/lists/*

ARG WARP_PLUS_VERSION=1.2.5
RUN set -eux; \
    ARCH="$(dpkg --print-architecture)"; \
    case "$ARCH" in \
        amd64) WP_ARCH="linux-amd64" ;; \
        arm64) WP_ARCH="linux-arm64" ;; \
        *) echo "unsupported architecture: $ARCH" && exit 1 ;; \
    esac; \
    
    URL="https://github.com/bepass-org/warp-plus/releases/download/v${WARP_PLUS_VERSION}/warp-plus_${WARP_PLUS_VERSION}_${WP_ARCH}.zip"; \
    if curl -fsSL -o /tmp/warp-plus.zip "$URL"; then \
        unzip -o /tmp/warp-plus.zip -d /opt/warp-plus; \
        mv /opt/warp-plus/warp-plus /usr/local/bin/warp-plus; \
        chmod +x /usr/local/bin/warp-plus; \
        rm -rf /tmp/warp-plus.zip /opt/warp-plus; \
    else \
        echo "ERROR: Failed to download warp-plus from $URL" && exit 1; \
    fi

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x entrypoint.sh

ENV WARP_PLUS_CACHE_DIR=/app/.warp-plus-cache
RUN mkdir -p ${WARP_PLUS_CACHE_DIR}

ENV USE_PROXY=false
ENV SOCKS5_PROXY_URL=socks5://127.0.0.1:8086

ENTRYPOINT ["./entrypoint.sh"]
