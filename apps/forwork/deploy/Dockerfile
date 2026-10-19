# ForWork in a container (for a VM or Azure Container Apps).  Build from apps/forwork: docker build -f deploy/Dockerfile -t forwork .
# The data folder must be a persistent volume on the host's own disk:  docker run -p 8000:8000 -v /srv/forwork:/data --env-file forwork.env forwork
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 FORWORK_DATA=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY forwork ./forwork
RUN useradd --create-home forwork && mkdir -p /data && chown forwork /data
USER forwork
EXPOSE 8000
CMD ["python", "-m", "forwork", "serve", "--host", "0.0.0.0", "--port", "8000"]
