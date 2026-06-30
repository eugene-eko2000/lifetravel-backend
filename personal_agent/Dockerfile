# Build context must be the workspace root (lifetravel-backend/):
#   docker build -f personal_agent/Dockerfile .
FROM python:3.11-slim

# Playwright system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl ca-certificates \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libexpat1 \
    libxcb1 libxkbcommon0 libx11-6 libxcomposite1 \
    libxdamage1 libxext6 libxfixes3 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    libglib2.0-0 libgdk-pixbuf2.0-0 libgtk-3-0 \
    fonts-liberation xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

COPY scraper_common ./scraper_common/
COPY personal_agent/pyproject.toml ./personal_agent/
COPY personal_agent/src ./personal_agent/src/

WORKDIR /workspace/personal_agent

RUN pip install --no-cache-dir . \
    && playwright install chromium \
    && playwright install-deps chromium

EXPOSE 8082

CMD ["python", "/workspace/personal_agent/src/main.py"]
