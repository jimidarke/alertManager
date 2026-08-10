# Alert Router Service Docker Image
FROM python:3.11-slim

# Set metadata
LABEL maintainer="Alert Router Service"
LABEL description="MQTT-based alert routing service"
LABEL version="1.0"

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH=/app

# Create application directory
WORKDIR /app

# Create data and log directories
RUN mkdir -p /data/logs /data/config

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        netcat-traditional \
        jq \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better layer caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY src/ ./src/
COPY pathways/ ./pathways/
COPY sql/ ./sql/
COPY tools/*.py ./tools/
COPY VERSION .
COPY data/config/docker.json /data/config/docker.json

# Note: no image-level HEALTHCHECK here. Each service's healthcheck is
# defined at the compose level (see docker-compose.yml) since the correct
# check differs per service (heartbeat-file freshness vs. HTTP /health).

# Expose HTTP gateway port (conditional based on service)
EXPOSE 8080

# Default command (can be overridden by docker-compose)
CMD ["python3", "src/alert_router.py", "--config", "/data/config/docker.json"]