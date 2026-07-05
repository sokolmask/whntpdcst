FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# fastapi stack for the panel + podcast_skill.py deps (episode generation runs here)
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" python-multipart mutagen \
    httpx "youtube-transcript-api>=0.6,<1.0" feedparser beautifulsoup4 pyyaml markdown

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", \
     "--app-dir", "/opt/data/skills/podcast/admin"]
