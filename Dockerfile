# CPU-baserad image. All GPU-detektion (modules/transcription.py:_resolve_device)
# sker redan vid körning via WHISPER_DEVICE/torch - inget applikationskod behöver
# ändras för att köra på GPU senare, bara basimagen (se README, avsnittet om
# GPU-transkribering i Docker).
FROM python:3.11-slim

# ffmpeg krävs av pydub för att klippa/normalisera ljud.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# uploads/, processed/, bulk_import/ och predikan.db ska normalt monteras
# som volymer (se docker-compose.yml) så data överlever att containern byts ut.
EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
