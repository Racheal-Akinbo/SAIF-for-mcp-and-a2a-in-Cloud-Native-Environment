# Single image shared by all three services (mcp, a2a, gateway).
# Which service actually runs is determined by the `command:` set per
# container in docker-compose.yml, not by anything baked into this image.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Containers must bind 0.0.0.0, not 127.0.0.1 -- otherwise the service is
# unreachable from other containers (or from the host via the published
# port), since 127.0.0.1 inside a container only means "this container."
ENV HOST=0.0.0.0

EXPOSE 8000 8001 8002

# Default command; each service in docker-compose.yml overrides this.
CMD ["python3", "gateway/app.py"]
