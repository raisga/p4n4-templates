# ELM327 simulator for the obd2 extra: a "car" on TCP 35000, for CI and p4n4-emu
FROM python:3.13.16-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir pyyaml==6.0.3

WORKDIR /app
COPY elm327_sim.py ./
COPY scenarios/ scenarios/

USER 10001:10001
EXPOSE 35000
ENTRYPOINT ["python", "/app/elm327_sim.py"]
