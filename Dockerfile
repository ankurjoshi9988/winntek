FROM python:3.10-slim

RUN apt-get update && apt-get install -y portaudio19-dev

WORKDIR /app
COPY . /app

RUN python -m venv venv && \
    . venv/bin/activate && \
    pip install -r requirements.txt

CMD ["venv/bin/gunicorn", "--bind", "0.0.0.0:8000", "main:app"]