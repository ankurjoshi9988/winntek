# Use the official Python base image
FROM python:3.10-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    portaudio19-dev \
    && rm -rf /var/lib/apt/lists/*  # Clean up to reduce image size

# Set the working directory in the container
WORKDIR /app

# Copy only the requirements.txt initially to leverage Docker cache
COPY requirements.txt .
# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Now copy the rest of your application files into the container
COPY . .

# Set Flask app environment variable
ENV FLASK_APP=main.py

# Verify gunicorn installation
RUN gunicorn --version

# Make port 8000 available to the world outside this container
EXPOSE 8000

# Command to run the application
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "main:app"]