# The API and the extraction worker: one image, two commands (see
# docker-compose.prod.yml). Built from the repository root, because the app
# reads validation/thresholds.yaml at runtime.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/opt/huggingface \
    PYTHONPATH=/srv/backend

WORKDIR /srv/backend

# CPU-only torch first, from PyTorch's own index: the default PyPI wheel on
# Linux bundles CUDA libraries (gigabytes) that nothing here uses. The pinned
# version in requirements.txt is then already satisfied.
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch==2.14.0

COPY backend/requirements.txt ./
RUN pip install -r requirements.txt

# The embedding model the matcher uses, baked in so the first invoice after a
# deploy doesn't wait on (or fail on) a download.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"
# From here on, use only what's baked in: no update checks against the
# model hub at runtime (which also tried to write to this root-owned cache).
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1

COPY validation/thresholds.yaml /srv/validation/thresholds.yaml
COPY backend/alembic.ini ./
COPY backend/alembic ./alembic
COPY backend/app ./app
COPY backend/scripts ./scripts

RUN useradd --system --uid 10001 app \
    && mkdir -p /srv/backend/uploads \
    && chown app /srv/backend/uploads
USER app

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
