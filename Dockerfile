# Deploys the FastAPI web demo using the CURRENT source (no PyPI needed):
# `pip install .` builds the centerline_svg package from pyproject.toml right here.
FROM python:3.11-slim

WORKDIR /app
COPY . .

# install the local library from source + the web-server dependencies
RUN pip install --no-cache-dir . \
        "fastapi>=0.110" "uvicorn[standard]>=0.29" "python-multipart>=0.0.9"

# Railway (and most PaaS) inject the port to listen on via $PORT
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn webapp.app:app --host 0.0.0.0 --port ${PORT}"]
