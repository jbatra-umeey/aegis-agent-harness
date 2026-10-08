FROM python:3.12-slim AS builder
WORKDIR /build
COPY pyproject.toml requirements.lock ./
COPY aegis ./aegis
RUN python -m pip install --no-cache-dir --prefix=/install -c requirements.lock .

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 aegis && mkdir /data && chown aegis:aegis /data
COPY --from=builder /install /usr/local
USER 10001:10001
WORKDIR /home/aegis
ENV AEGIS_DATA_DIR=/data PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=2)"
CMD ["python", "-m", "aegis", "serve", "--host", "0.0.0.0"]
