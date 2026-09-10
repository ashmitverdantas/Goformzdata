# =============================================================================
# GoFormz Extractor — production image for Azure Container Apps (Job)
# Python 3.11 + Chromium + chromedriver for headless Selenium.
# =============================================================================
FROM python:3.11-slim

# ---- OS packages: Chromium, driver, and the libs Chrome needs -------------
# 'chromium' and 'chromium-driver' come from Debian; versions stay in sync.
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        chromium-driver \
        ca-certificates \
        fonts-liberation \
        libnss3 \
        libxss1 \
        libasound2 \
        libatk-bridge2.0-0 \
        libgtk-3-0 \
        libgbm1 \
        tini \
    && rm -rf /var/lib/apt/lists/*

# ---- Environment ----------------------------------------------------------
# Tell the script exactly where Chromium and the driver live in this image.
ENV CHROME_BIN=/usr/bin/chromium \
    CHROMEDRIVER_PATH=/usr/bin/chromedriver \
    HEADLESS=true \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# ---- Non-root user --------------------------------------------------------
RUN useradd --create-home --uid 10001 appuser
WORKDIR /app

# ---- Python dependencies (layer-cached) -----------------------------------
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# ---- Application ----------------------------------------------------------
COPY formsDataDownload.py .

USER appuser

# tini gives us proper signal handling / zombie reaping for the browser.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "formsDataDownload.py"]
