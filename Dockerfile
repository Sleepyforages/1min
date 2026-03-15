FROM python:3.12-slim

# Security: run as non-root
RUN groupadd -r botuser && useradd -r -g botuser botuser

WORKDIR /app

# Install system deps first (layer-cache friendly)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps before copying source
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY config/ ./config/
COPY src/ ./src/

# Create runtime directories and fix ownership
RUN mkdir -p data logs && chown -R botuser:botuser /app

USER botuser

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
