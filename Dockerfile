FROM python:3.12-slim

WORKDIR /app

# Install system deps (build-essential + python3-dev needed for pysha3/eip712-structs)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    build-essential \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps before copying source
# pysha3 (needed by eip712-structs) requires Python headers; in the official
# python:slim image they live under /usr/local/include, not /usr/include.
COPY requirements.txt .
RUN CFLAGS="-I/usr/local/include/python3.12" pip install --no-cache-dir pysha3 \
    && pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY config/ ./config/
COPY src/ ./src/

# Create runtime directories
RUN mkdir -p data logs

# Expose Streamlit port
EXPOSE 8501

# Streamlit health-check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD curl -f http://localhost:8501/_stcore/health || exit 1

CMD ["streamlit", "run", "src/ui.py", \
     "--server.port=8501", \
     "--server.address=0.0.0.0", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
