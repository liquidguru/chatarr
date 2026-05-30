FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/liquidguru/chatarr" \
      org.opencontainers.image.description="Chat with your media server in plain English — Sonarr/Radarr requests via Groq + TMDB. Web + Telegram." \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY core.py bot.py web.py run.py ./
CMD ["python", "run.py"]
