FROM python:3.12-slim

WORKDIR /app

# Abhängigkeiten installieren
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Skript kopieren & Daten-Ordner vorbereiten
COPY anilist_sorter.py .
RUN mkdir -p /app/data

EXPOSE 8501

# Streamlit im Server-Modus starten (ohne automatischen Browser-Tab)
CMD ["streamlit", "run", "anilist_sorter.py", "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true"]