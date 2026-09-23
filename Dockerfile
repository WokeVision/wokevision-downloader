FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fontconfig curl unzip git \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp needs a real JavaScript runtime to solve YouTube's anti-bot
# challenges (part of YouTube's 2026 infrastructure changes) -- without
# one, extraction fails regardless of client/cookie settings.
RUN curl -fsSL https://deno.land/install.sh | sh -s -- -y
ENV DENO_INSTALL="/root/.deno"
ENV PATH="$DENO_INSTALL/bin:$PATH"

# PO-token provider: generates the "proof of origin" token YouTube now
# requires, without needing a logged-in account or browser cookies.
# Runs as a small background process alongside the main app.
RUN git clone --single-branch --branch 2.0.0 \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/pot-provider \
    && cd /opt/pot-provider/server \
    && deno install --allow-scripts=npm:canvas --frozen

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py render.py emoji_names.py .
COPY assets ./assets
COPY fonts ./fonts
COPY emoji_pack ./emoji_pack
COPY start.sh .
RUN chmod +x start.sh

CMD ["./start.sh"]
