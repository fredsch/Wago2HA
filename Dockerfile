FROM python:3.12-slim

LABEL org.opencontainers.image.title="Wago2HA" \
      org.opencontainers.image.description="Passerelle Wago 750-881 (Calaos) vers Home Assistant via MQTT" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"

ENV PYTHONUNBUFFERED=1 \
    CONFIG=/config/config.yaml

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY wago2ha ./wago2ha

# Le port UDP Calaos doit etre joignable depuis l'automate (4646 par defaut).
EXPOSE 4646/udp

ENTRYPOINT ["python", "-m", "wago2ha"]
