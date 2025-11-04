# Use a lightweight ARM-compatible Python base for Raspberry Pi
FROM python:3.11-slim

# Set timezone to India (Asia/Kolkata)
ENV TZ=Asia/Kolkata
RUN apt-get update && apt-get install -y tzdata && \
    ln -fs /usr/share/zoneinfo/$TZ /etc/localtime && dpkg-reconfigure -f noninteractive tzdata && \
    rm -rf /var/lib/apt/lists/*

# Set work directory
WORKDIR /app

# Copy Python script and images
COPY final.py /app/
COPY images /app/images

# Install Python dependencies
RUN pip install --no-cache-dir requests

# Ensure state file persists (will be mounted as volume)
VOLUME ["/app/state"]

# Start script
CMD ["python", "-u", "final.py"]
