#########################################
# Stage 1 - Builder
#########################################

FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /build

# Dependências necessárias apenas para build
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --upgrade pip

RUN pip install \
    --prefix=/install \
    --no-cache-dir \
    -r requirements.txt

#########################################
# Stage 2 - Runtime
#########################################

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8003

WORKDIR /app

# Apenas runtime do PostgreSQL
RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends libpq5 && \
    rm -rf /var/lib/apt/lists/*

# Usuário não privilegiado
RUN groupadd -r appgroup && \
    useradd -r -g appgroup -u 10001 appuser

# Dependências Python vindas do builder
COPY --from=builder /install /usr/local

# Código da aplicação
COPY app.py .

# Ajusta permissões
RUN chown -R appuser:appgroup /app

USER appuser

EXPOSE 8003

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
CMD python -c "import requests; requests.get('http://localhost:8003/health', timeout=2)"

CMD ["gunicorn", \
     "--bind", "0.0.0.0:8003", \
     "--workers", "2", \
     "--threads", "4", \
     "--timeout", "30", \
     "app:app"]