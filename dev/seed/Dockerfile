FROM python:3.13-slim

RUN pip install --no-cache-dir opensearch-py

WORKDIR /app
COPY seed.py .
COPY data/ data/

ENTRYPOINT ["python", "seed.py"]
