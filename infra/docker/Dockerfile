FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    git \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir -e .

# Copy application code
COPY agents/ agents/
COPY infra/ infra/

# Create directories for agent memory
RUN mkdir -p agents/squad/jarvis/daily \
    agents/squad/friday/daily \
    agents/squad/vision/daily \
    agents/squad/wong/daily \
    agents/squad/shuri/daily \
    agents/squad/fury/daily \
    agents/squad/pepper/daily

# Expose port for potential API
EXPOSE 8000

# Default command
CMD ["python", "-m", "agents.cli", "start"]
