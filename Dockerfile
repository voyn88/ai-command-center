# AML Service — производственный образ
# Python 3.13-slim, Streamlit на порту 8501
FROM python:3.13-slim AS base

WORKDIR /app

# Системные зависимости (необходимы для некоторых Python-пакетов)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Зависимости устанавливаем отдельным слоем — кешируется при изменении кода
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Исходный код приложения
COPY command_center/ ./command_center/
COPY app.py ./

# Entrypoint
COPY scripts/aml-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Директория для данных — монтируется как том
RUN groupadd --gid 10001 aicc \
    && useradd --uid 10001 --gid aicc --create-home --shell /usr/sbin/nologin aicc \
    && mkdir -p /data \
    && chown aicc:aicc /data
ENV AICC_DATA_DIR=/data

# Streamlit не должен открывать браузер в контейнере
ENV STREAMLIT_BROWSER_GATHER_USAGE_STATS=false
ENV STREAMLIT_SERVER_HEADLESS=true

EXPOSE 8501

# The service has no need for root after its image layers are assembled.
USER aicc

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8501/_stcore/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
