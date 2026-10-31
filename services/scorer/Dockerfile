FROM python:3.12-slim

# ffmpeg cuts frames out of the mp4 clips.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Dependencies first, so code changes don't reinstall them.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scorer.py decision_client.py questions.yaml ./
EXPOSE 9100
USER 1000
CMD ["python", "scorer.py"]
