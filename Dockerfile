FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY db ./db
COPY scripts ./scripts

ENV RUN_MODE=web PYTHONUNBUFFERED=1
CMD ["python", "-m", "inais.main"]
