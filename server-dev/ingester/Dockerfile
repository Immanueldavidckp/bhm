FROM python:3.12-slim
RUN pip install --no-cache-dir paho-mqtt==2.1.0 PyMySQL==1.1.1 cryptography==43.0.3
COPY ingester.py /app/ingester.py
CMD ["python", "-u", "/app/ingester.py"]
