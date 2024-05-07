# Use the official Python base image
FROM python:3.10-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    portaudio19-dev \
    && apt-get clean

# Set the working directory in the container
WORKDIR /app

# Copy the current directory contents into the container at /app
COPY . /app

# Create and activate a virtual environment, then install dependencies
RUN python -m venv venv && \
    . venv/bin/activate && \
    pip install --upgrade pip && \
    pip install -r requirements.txt

# Set the environment variable for the virtual environment
ENV PATH="/app/venv/bin:$PATH"

# Command to run the application
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "main:app"]
