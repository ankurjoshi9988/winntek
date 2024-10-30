# Use the official Python base image
FROM python:3.10-slim

# Install build tools and basic system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    ghostscript \
    && rm -rf /var/lib/apt/lists/*

# Install OpenCV and image processing dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libopencv-dev \
    libsm6 \
    libxext6 \
    libxrender-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Tesseract and audio processing dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    portaudio19-dev \
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory in the container
WORKDIR /app

# Copy only the requirements.txt to leverage Docker cache
COPY requirements.txt .

# Install Python dependencies without cache
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir opencv-python-headless

# Copy the rest of the application files into the container
COPY . .

# Verify gunicorn installation
RUN gunicorn --version

# Expose port 8000 to the outside world
EXPOSE 8000

# Command to run the application
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "main:app", "--timeout", "120", "--workers", "3"]
