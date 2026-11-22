FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV VALA_CLOUD_MENU=1
ENV VALA_DB_PATH=/data/vala.db
RUN mkdir -p /data
EXPOSE 5000
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "30", "wsgi:app"]
