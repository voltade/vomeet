# Use an official Python runtime as a parent image
FROM python:3.10-slim

# Set the working directory in the container
WORKDIR /app

# --- Install Shared Library ---
# Copy the shared library source code
COPY ./libs/shared-models/ /app/libs/shared-models/
# Install the shared library so it's available to the app
RUN pip install --no-cache-dir /app/libs/shared-models/

# --- Install App Dependencies ---
# Copy the requirements file
COPY ./services/google-integration/requirements.txt .
# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# --- Copy App Source ---
# Copy the application source code
COPY ./services/google-integration/main.py .

# Expose the port the app runs on
EXPOSE 8000

# Run the application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
