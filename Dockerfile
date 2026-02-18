FROM python:3.11-slim

# Install system dependencies
# ffmpeg: required for audio processing
# libopus0: required for discord voice
# build-essential & python3-dev: required for compiling psutil and other C extensions
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libopus0 \
    build-essential \
    libffi-dev \
    libnacl-dev \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Create cache directory if it doesn't exist
RUN mkdir -p music_cache

# Set environment variables
# PYTHONPATH ensures 'from config import ...' works from the root
ENV PYTHONPATH="/app/src"
ENV PORT=8080

# Expose the web dashboard port
EXPOSE 8080

# Start the bot
# Running from /app ensures CACHE_DIR='./music_cache' resolves to /app/music_cache
CMD ["python", "src/bot.py"]
