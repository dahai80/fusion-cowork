# syntax=docker/dockerfile:1.7
# fusion-cowork v0.5.3 — 多租户云容器镜像
# 多阶段: builder (装 [cloud,web]) → runtime (瘦镜像)

ARG PYTHON_VERSION=3.12-slim

FROM python:${PYTHON_VERSION} AS builder
LABEL stage=builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# 先装 fusion-core (公共底座, in-tree) + 本项目 cloud/web extras
# 注: 本构建仅含 fusion-cowork 自身; fusion-core 需随上下文一并 COPY (见 .dockerignore 白名单)
COPY fusion-core /build/fusion-core
COPY fusion-cowork /build/fusion-cowork

RUN pip install --user --no-cache-dir /build/fusion-core
RUN pip install --user --no-cache-dir "/build/fusion-cowork[cloud,web]"


FROM python:${PYTHON_VERSION} AS runtime
LABEL version="0.5.3" \
      org.opencontainers.image.title="fusion-cowork" \
      org.opencontainers.image.version="0.5.3" \
      org.opencontainers.image.description="Local-first Apple Silicon AI 多租户云工作流引擎"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/root/.local/bin:$PATH \
    FUSION_BIND_HOST=0.0.0.0

# 非 root 运行
RUN useradd --create-home --uid 1000 fcuser
USER fcuser
WORKDIR /app

COPY --from=builder --chown=fcuser:fcuser /root/.local /home/fcuser/.local

EXPOSE 11438 11439

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:11438/health', timeout=4); sys.exit(0)" || exit 1

ENTRYPOINT ["python", "-m", "fusion_cowork"]
CMD ["serve", "--port", "11438"]
