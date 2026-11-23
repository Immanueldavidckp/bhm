# build context is server-dev/ (see docker-compose.yml), so the page is baked into the image:
# after changing dashboard.html run  docker compose up -d --build dashboard
FROM python:3.12-slim
RUN pip install --no-cache-dir PyMySQL==1.1.1 cryptography==43.0.3
COPY api/api.py /app/api.py
COPY dashboard.html /app/dashboard.html
CMD ["python", "-u", "/app/api.py"]
