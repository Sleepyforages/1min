FROM python:3.12-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps before copying source
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    # py_clob_client imports `from eip712_structs import ...` but the actual
    # package installed is poly_eip712_structs (Polymarket's fork, different name).
    # Create a shim so the import resolves correctly.
    && python -c "\
import site, pathlib; \
sp = pathlib.Path(site.getsitepackages()[0]); \
shim = sp / 'eip712_structs'; \
shim.mkdir(exist_ok=True); \
(shim / '__init__.py').write_text('from poly_eip712_structs import *\n'); \
print('eip712_structs shim created at', shim)"

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
