FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# system deps for pillow + ticket fonts (Inter)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg62-turbo zlib1g fonts-dejavu-core wget unzip \
 && mkdir -p /usr/share/fonts/truetype/inter \
 && wget -q "https://github.com/rsms/inter/releases/download/v4.1/Inter-4.1.zip" -O /tmp/inter.zip \
 && unzip -q /tmp/inter.zip -d /tmp/inter \
 && cp /tmp/inter/Inter.ttc /usr/share/fonts/truetype/inter/ \
 && rm -rf /tmp/inter /tmp/inter.zip \
 && apt-get purge -y wget unzip && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app

EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
