# Lightweight ARM-compatible Python base (works on Raspberry Pi)
FROM python:3.11-slim

# Set timezone
ENV TZ=Asia/Kolkata
RUN apt-get update && apt-get install -y tzdata && \
    ln -fs /usr/share/zoneinfo/$TZ /etc/localtime && \
    dpkg-reconfigure -f noninteractive tzdata && \
    rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# Copy main script
COPY sql.py /app/autoclock.py

# Install dependencies
RUN pip install --no-cache-dir requests

# Create persistent directories (will be overridden by volumes)
RUN mkdir -p /app/state /app/images

# Declare volumes for SQLite DB + images
VOLUME ["/app/state", "/app/images"]

# Run script
CMD ["python", "-u", "/app/autoclock.py"]