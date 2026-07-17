# Sampler del fondo — servizio Python (Flask) per Render
FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir flask requests pillow gunicorn
COPY app.py .
ENV PORT=10000
EXPOSE 10000
CMD ["sh","-c","gunicorn --bind 0.0.0.0:${PORT:-10000} --workers 1 --threads 4 --timeout 120 app:app"]
