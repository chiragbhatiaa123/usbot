FROM python:3.11-slim-bookworm

# Prevent Python from buffering stdout/stderr and generating .pyc files
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Install system dependencies with retries for apt-get update
RUN apt-get clean && \
    for i in 1 2 3 4 5; do apt-get update && break || (echo "apt-get update failed, retrying in 5 seconds..." && sleep 5); done && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        libgl1-mesa-glx \
        libglib2.0-0 \
        curl \
        git \
    && rm -rf /var/lib/apt/lists/*

# Set up work directory
WORKDIR /app

# Copy requirements.txt and install python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy all project files into the container
COPY . .

# Create necessary runtime directories and set permissions
RUN mkdir -p /app/fonts /app/db /app/workspace && chmod -R 777 /app

# Render passes a dynamic port via the PORT environment variable (default 10000)
# We expose both 10000 and 8000 for standard Docker compatibility
EXPOSE 10000 8000

# Run the textbox_bot with unbuffered output so logs stream immediately
CMD ["python", "-u", "textbox_bot.py"]

