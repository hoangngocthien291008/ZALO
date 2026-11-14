# Dockerfile for DataMate - Zalo AI Challenge 2026
FROM python:3.11-slim

# Avoid interactive prompts
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy source code
COPY datamate /app/datamate
COPY predict.py /app/predict.py
COPY predict.sh /app/predict.sh
COPY evaluate.py /app/evaluate.py
COPY README.md /app/README.md

# Make entrypoint executable
RUN chmod +x /app/predict.sh /app/predict.py

# Expected mount volumes:
# - /data (contains data/, questions.jsonl, calendar.csv, rulebook.md)
# - /result (output submission.jsonl and logs/)

ENTRYPOINT ["/app/predict.sh"]
