# Job Application Tracker — container image.
# The app is still local-first: the SQLite DB lives in /app/data, which you
# mount as a volume so your data persists outside the container.

FROM python:3.12-slim

# Don't buffer stdout (so logs show up) and don't write .pyc files.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app

# Install dependencies first so this layer caches across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code (fonts, htmx and templates are committed, so the image is offline-ok).
COPY run.py .
COPY app/ app/
COPY templates/ templates/
COPY static/ static/

# Persist the SQLite database here.
VOLUME ["/app/data"]

EXPOSE 8000

CMD ["python", "run.py"]
