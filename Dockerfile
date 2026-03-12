FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY watcher.py .

# Create data directories
RUN mkdir -p daily_csv

# Run the bot
CMD ["python", "watcher.py"]
