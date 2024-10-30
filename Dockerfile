# Use the official Python base image
FROM python:3.10-slim

# Install system dependencies, including OpenCV dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    ghostscript \
    libglib2.0-0 \
    libopencv-dev \
    libsm6 \
    libxext6 \
    libxrender-dev \
    poppler-utils \
    portaudio19-dev \
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*  # Clean up to reduce image size

# Set the working directory in the container
WORKDIR /app

# Copy only the requirements.txt initially to leverage Docker cache
COPY requirements.txt .

# Install Python dependencies
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
