FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir fastapi "uvicorn[standard]" python-multipart mutagen

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", \
     "--app-dir", "/opt/data/skills/podcast/admin"]
