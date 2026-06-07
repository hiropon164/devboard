FROM python:3.12-slim

# git is required so the scanner can read branch/status/commit info
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY scanner.py server.py index.html ./

# Treat any mounted projects dir as a safe git directory (avoids
# "dubious ownership" errors when host UID differs from container)
RUN git config --global --add safe.directory '*'

ENV PROJECTS_DIR=/data/projects \
    PORT=8080 \
    SCAN_DEPTH=4 \
    CACHE_TTL=30

EXPOSE 8080
CMD ["python", "server.py"]
