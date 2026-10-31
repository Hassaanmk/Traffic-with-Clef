FROM python:3.12-slim

WORKDIR /app
# Dependencies first, so code changes don't reinstall them.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY static/ static/
EXPOSE 8080
USER 1000
CMD ["python", "app.py"]
