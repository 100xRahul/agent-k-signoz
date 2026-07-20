FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir redis opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc

COPY . /app/chaos/

# Keep container alive for exec commands
CMD ["tail", "-f", "/dev/null"]
