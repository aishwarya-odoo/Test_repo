FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends supervisor \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data /var/log/tally_connector

CMD ["/usr/bin/supervisord", "-c", "/app/supervisord.conf", "-n"]
