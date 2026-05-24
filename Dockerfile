FROM python:3.12-slim

# Run as non-root user (security best practice)
RUN useradd --create-home --shell /bin/bash guardian
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY guardian.py .

# Switch to non-root
USER guardian

ENV PORT=8080
EXPOSE 8080

# Use thread-safe single Gunicorn worker process to correctly maintain shared in-memory daily budget limits
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", "--worker-class", "gthread", "--timeout", "60", "guardian:app"]
