# Hash-Based File Verification Project

## Overview
This project provides a set of tools to manage, verify, and deduplicate files using MongoDB as a backend. It includes operations for uploading files, generating file hashes, digesting metadata, and identifying duplicate files in a collection. The project is designed to manage large datasets by storing the file contents in MongoDB using GridFS and maintaining metadata for quick access and verification.

## Features
- **Upload Files**: Uploads files from a directory to MongoDB using GridFS, storing file metadata in a separate collection.
- **Check Existing Files**: Check which files in a directory already exist in the database based on their original file paths.
- **Digest Database**: Iterates through all documents in the database to compute a hash value for the file data and add it to the document metadata.
- **Generate Summary**: Provides a summary of the current state of the database, including the total number of files and the number of matching file hashes.
- **Find Duplicate Files**: Identifies and provides an example of two files with matching hashes for review.

## Prerequisites
- **Python 3.6+**
- **Docker** (recommended for easy deployment)
- **MongoDB**

## Installation
1. **Clone the Repository**:
   ```sh
   git clone <repository-url>
   cd hash-based-file-verification
   ```

2. **Install Dependencies**:
   Use the `requirements.txt` to install the required Python packages.
   ```sh
   pip install -r requirements.txt
   ```

3. **Setup MongoDB**:
   You can either install MongoDB locally or use Docker to run it.
   
   To run MongoDB in Docker:
   ```sh
   docker run -d -p 27017:27017 --name mongodb mongo:latest
   ```

4. **Environment Setup**:
   Set the MongoDB URI as an environment variable if you are using Docker:
   ```sh
   export MONGO_URI=mongodb://localhost:27017/
   ```

## Usage
The script provides several options for interacting with the database and files.

### Command-Line Arguments
- **Upload Files**
  ```sh
  python main.py <directory_path>
  ```
  Uploads all files in the specified directory to the database.

- **Dry Run**
  ```sh
  python main.py <directory_path> --dry-run
  ```
  Performs a dry run of the upload process without actually uploading the files.

- **Check Existing Files**
  ```sh
  python main.py <directory_path> --check
  ```
  Checks how many files in the directory are new versus those that already exist in the database.

- **Generate Summary**
  ```sh
  python main.py --summary
  ```
  Generates a summary of the state of the file contents in the database, including the total number of files and the number of matching hashes.

- **Digest Database**
  ```sh
  python main.py --digest
  ```
  Iterates through all documents in the database, computing a hash for each file and updating the metadata accordingly.

## Example Output
- **Upload Progress**: Logs progress as files are uploaded, with the option for a dry run.
- **Summary**: Logs total number of files, number of matching file hashes, and an example pair of matching files.
- **Digest Process**: Logs progress as it computes and updates hashes for each document.

## Docker Setup
To run both the MongoDB instance and the Python application using Docker Compose:

1. **Create a Docker Compose File**:
   Use a `docker-compose.yml` file to define the services (MongoDB and the Python app).

2. **Build and Run Containers**:
   ```sh
   docker-compose up --build
   ```
   This will start both MongoDB and the Python application in the specified configuration.

## Logging
- **Info Level Logs**: Provides detailed logging of progress, such as uploads, checks, and digest updates.
- **Warnings**: Issues warnings if files are missing or if other anomalies are detected.

## Notes
- **Memory Management**: The `digest_database` function reads files in chunks to prevent excessive memory usage.
- **Cursor Timeout**: The database cursor has been set to `no_cursor_timeout=True` to prevent the process from being killed prematurely while iterating through large datasets.

## Contributing
Feel free to fork this repository, create issues, or submit pull requests. Contributions are always welcome!

## License
This project is licensed under the Creative Commons Attribution 4.0 International (CC BY 4.0) License.

Under this license, you are free to:
- **Share**: Copy and redistribute the material in any medium or format.
- **Adapt**: Remix, transform, and build upon the material for any purpose, even commercially.

**Attribution** is required: You must give appropriate credit, provide a link to the license, and indicate if changes were made.

