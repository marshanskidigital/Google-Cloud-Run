# Use the official Python lightweight image.
FROM python:3.13-slim

# Allow statements and log messages to immediately appear in the Knative logs
ENV PYTHONUNBUFFERED True

# Copy local code to the container image.
ENV APP_HOME /app
WORKDIR $APP_HOME
COPY . ./

# Install production dependencies.
RUN pip install --no-cache-dir -r requirements.txt

# Run the web service on container startup using gunicorn.
# We set timeout to 300 seconds to allow for long Claude API processing time.
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 300 main:app
