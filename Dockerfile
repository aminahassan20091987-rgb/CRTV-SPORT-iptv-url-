FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY app.py .
COPY source.txt .

EXPOSE 8080

CMD ["python", "-u", "app.py"]
