FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl fontconfig \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /app/fonts && \
    curl -sL -o /app/fonts/Montserrat-Bold.ttf "https://github.com/google/fonts/raw/main/ofl/montserrat/Montserrat-Bold.ttf" && \
    curl -sL -o /app/fonts/Inter-Bold.ttf "https://github.com/google/fonts/raw/main/ofl/inter/static/Inter-Bold.ttf" && \
    mkdir -p /usr/share/fonts/truetype/custom && \
    cp /app/fonts/*.ttf /usr/share/fonts/truetype/custom/ && \
    fc-cache -f

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py render.py .
COPY assets ./assets

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port $PORT"]
