# obd2: OBD-II adapter → MQTT, with a read-only HTTP API on 8090
FROM python:3.13.16-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY obd2_bridge/ obd2_bridge/

# Non-root; a USB adapter's group (dialout) is added in the override, not here
USER 10001:10001
EXPOSE 8090
ENTRYPOINT ["python", "-m", "obd2_bridge"]
