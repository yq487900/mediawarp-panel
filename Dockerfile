# syntax=docker/dockerfile:1
#
# MediaWarp Panel —— 给 MediaWarp 套一个网页设置面板
#
# ⚠️ 本镜像会包含 MediaWarp 本体。MediaWarp 版权归 AkimioJR，采用 AGPL-3.0 修改版许可
#    （禁止商用；使用其代码须开源并注明出处）。详见仓库根目录 NOTICE。
#    为保持仓库轻量与合规，MediaWarp 二进制**不随仓库分发**，而是构建时从官方 Release 下载。
#
FROM python:3.12-alpine

# MediaWarp 版本（对应官方 tag，如 0.2.4 / 0.1.8；可用 --build-arg MEDIAWARP_VERSION=x.y.z 覆盖）
ARG MEDIAWARP_VERSION=0.2.4
# buildx 为每个目标平台注入 TARGETARCH（amd64 / arm64 / arm）
ARG TARGETARCH

RUN apk add --no-cache tzdata curl ca-certificates \
 && pip install --no-cache-dir pyyaml

# 按架构从官方 Release 下载 MediaWarp 到 /opt/mediawarp
RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
      amd64|x86_64)  MW_ARCH=amd64 ;; \
      arm64|aarch64) MW_ARCH=arm64 ;; \
      arm|armv7)     MW_ARCH=armv7 ;; \
      *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    base="https://github.com/AkimioJR/MediaWarp/releases/download"; \
    mkdir -p /opt/mediawarp; \
    ok=0; \
    for url in \
      "${base}/v${MEDIAWARP_VERSION}/MediaWarp_${MEDIAWARP_VERSION}_linux_${MW_ARCH}.tar.gz" \
      "${base}/${MEDIAWARP_VERSION}/MediaWarp_${MEDIAWARP_VERSION}_linux_${MW_ARCH}.tar.gz" \
      "${base}/v${MEDIAWARP_VERSION}/MediaWarp_linux_${MW_ARCH}.tar.gz" \
      "https://github.com/AkimioJR/MediaWarp/releases/latest/download/MediaWarp_linux_${MW_ARCH}.tar.gz" ; do \
        echo "trying: $url"; \
        if curl -fsSL "$url" -o /tmp/mw.tar.gz; then ok=1; echo "downloaded: $url"; break; fi; \
    done; \
    [ "$ok" = 1 ] || { echo "MediaWarp ${MEDIAWARP_VERSION} 下载失败 —— 请到 https://github.com/AkimioJR/MediaWarp/releases 确认可用版本号，再用 --build-arg MEDIAWARP_VERSION=... 重试" >&2; exit 1; }; \
    tar -xzf /tmp/mw.tar.gz -C /opt/mediawarp; \
    rm -f /tmp/mw.tar.gz; \
    if [ ! -f /opt/mediawarp/MediaWarp ]; then \
      bin="$(ls -S /opt/mediawarp/* 2>/dev/null | head -1)"; \
      [ -n "$bin" ] && mv "$bin" /opt/mediawarp/MediaWarp; \
    fi; \
    [ -f /opt/mediawarp/MediaWarp ] || { echo "release 包里没找到 MediaWarp:" >&2; ls -lR /opt/mediawarp; exit 1; }; \
    chmod +x /opt/mediawarp/MediaWarp; \
    ls -l /opt/mediawarp

# /app 挂载用户数据（config/ logs/ ui_state.json sessions.json）
ENV MW_HOME=/app \
    MW_BIN=/opt/mediawarp/MediaWarp \
    MW_CFG_FMT=new \
    UI_PORT=9009 \
    MW_PORT=9000 \
    TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1

COPY app.py /opt/ui/app.py
COPY reset_pw.py /opt/ui/reset_pw.py

# 容器内部：9000 = MediaWarp 反代本体；9009 = 设置面板
EXPOSE 9000 9009
WORKDIR /app

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python3", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9009/health', timeout=4).status == 200 else 1)"]

CMD ["python3", "/opt/ui/app.py"]
