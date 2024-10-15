# Use an official Python runtime as the base image
FROM python:3.9-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file
COPY requirements.txt /app/requirements.txt

# Install the Python dependencies
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy the current directory contents into the container at /app
COPY . /app

# Command to run the application
CMD ["python", "main.py"]
