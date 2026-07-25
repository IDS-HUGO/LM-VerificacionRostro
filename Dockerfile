FROM python:3.11-slim

# libgl1/libglib2.0-0: dependencias nativas de opencv-python-headless para
# decodificar imágenes (aun en headless, algunas rutas de OpenCV requieren
# estas librerías del sistema).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/kyc-service

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Este servicio nunca escribe imágenes a disco por diseño; no se declara
# ningún volumen de datos persistente a propósito.
EXPOSE 8000

ENV PORT=8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
