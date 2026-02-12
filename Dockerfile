# Use lightweight Python base image
FROM python:3.9-slim

# Set environment variables to prevent .pyc files and buffer issues
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install system dependencies: FFmpeg is required for the transcoding logic
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements file first to leverage Docker cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code
COPY app.py .

# Expose the port (Render/Zeabur/Heroku will override this with the PORT env var)
EXPOSE 8080

# Start the application using the python interpreter
# We use direct python execution to maintain the single-process model 
# required for managing the global 'active_streams' dictionary.
CMD ["python", "app.py"]
