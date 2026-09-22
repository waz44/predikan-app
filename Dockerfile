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

# Kör som en icke-root-användare (härdning). OBS: vid bind-monterade volymer
# på Linux måste värdkatalogerna vara skrivbara för uid 1000 - på Docker
# Desktop (Windows/Mac) sköts det transparent.
RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

# uploads/, processed/, bulk_import/ och predikan.db ska normalt monteras
# som volymer (se docker-compose.yml) så data överlever att containern byts ut.
EXPOSE 8000

# Enkel hälsokoll mot versions-endpointen (ingen curl i slim-imagen, så
# python används i stället).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/version', timeout=3).status==200 else 1)"

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
