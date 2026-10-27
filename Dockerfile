FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static
COPY glossary.toml .

ENV PDT_HOST=0.0.0.0
EXPOSE 8765
VOLUME ["/srv/app/data"]

CMD ["python", "-m", "app"]
