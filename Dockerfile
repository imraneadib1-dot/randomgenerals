# RandomGenerals AI - container image for any CPU-only host.
#
# Render uses render.yaml (its native Python runtime) rather than this
# file; this stays for hosts that want a container instead.
#
# WHAT IS AND ISN'T IN HERE
# No Ollama and no torch/diffusers. The free tier has 2 vCPU and no GPU:
# a 7B model would answer at about a word per second, and the local
# image model would take minutes per picture. Chat comes from Groq
# instead (free tier, already supported in app.py) and image generation
# from a cloud API. Everything else - accounts, threads, memory,
# credits, the sandboxed code runner, web search - runs here unchanged.
#
# Skipping torch also takes the image from ~2.5GB to ~250MB, which
# matters when the Space rebuilds.

FROM python:3.11-slim

# Hugging Face Spaces runs containers as UID 1000. Files written by root
# at build time would then be unwritable at runtime, so the user is
# created up front and everything is owned by it.
RUN useradd -m -u 1000 appuser

WORKDIR /app

# Dependencies first, so a code change doesn't invalidate the pip layer.
COPY --chown=appuser:appuser requirements-cloud.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements-cloud.txt

COPY --chown=appuser:appuser . .

# Writable locations the app needs. The container filesystem is
# otherwise effectively read-only for the app user.
RUN mkdir -p /app/static/uploads /app/static/generated /app/data \
    && chown -R appuser:appuser /app

USER appuser

# 7860 is the port Spaces expects; app.py already reads PORT.
ENV PORT=7860 \
    APP_DEBUG=0 \
    ALLOW_MOCK_UPGRADE=0 \
    FORCE_HTTPS_COOKIES=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/app

EXPOSE 7860

# Spaces restarts an unhealthy container, so /api/health earns its keep.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:7860/api/health', timeout=4).status==200 else 1)"

CMD ["python", "app.py"]
