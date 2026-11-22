# Menggunakan image resmi Python 3.13 yang ramping (slim)
FROM python:3.13-slim

# Set zona waktu ke Asia/Jakarta (WIB)
ENV TZ=Asia/Jakarta
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Set direktori kerja di dalam kontainer
WORKDIR /app

# Salin dan pasang dependencies terlebih dahulu untuk caching layer
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Salin seluruh kode aplikasi
COPY . .

# Jalankan bot
CMD ["python", "main.py"]
