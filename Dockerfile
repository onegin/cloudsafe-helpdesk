# Dockerfile for cloudsafe-helpdesk
FROM python:3.12-slim

# Set workdir
WORKDIR /app

# Save dependencies first for caching
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose port
EXPOSE 5000

# Application env defaults
ENV FLASK_APP=app.py
ENV FLASK_ENV=production
ENV DATABASE_URL=sqlite:///ticket_system.db

# Recommended to set SECRET_KEY for production
# ENV SECRET_KEY="your-secret-key"

# Start command
CMD ["python", "app.py"]
