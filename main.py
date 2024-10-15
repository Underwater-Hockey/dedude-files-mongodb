import os
import argparse
import logging
from pymongo import MongoClient
import gridfs
import hashlib
import os

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Get MongoDB URI from the environment (for Docker)
MONGO_URI = os.getenv('MONGO_URI', 'mongodb://localhost:27017/')

# Database connection
client = MongoClient(MONGO_URI)
db = client['hash_index_db']
collection = db['files']
fs = gridfs.GridFS(db)

def upload_file_to_db(file_path, dry_run):
    """
    Uploads the entire file using GridFS to handle larger files.
    """
   
    if dry_run:
        logging.info(f"[Dry Run] File would be uploaded: {file_path}")
    else:
        # Store the file in GridFS
        with open(file_path, 'rb') as file:
            file_id = fs.put(file, filename=os.path.basename(file_path), original_file_path=file_path)
        # Insert metadata into the files collection
        collection.insert_one({
            "file_id": file_id,
            "original_file_path": file_path
        })

def check_existing_files(directory_path):
    """
    Checks the directory for how many new original file paths are in the directory and how many already exist in the database.
    """
    new_files = 0
    existing_files = 0

    for root, _, files in os.walk(directory_path):
        for file in files:
            file_path = os.path.join(root, file)
            if collection.find_one({"original_file_path": file_path}):
                existing_files += 1
            else:
                new_files += 1

    logging.info(f"Number of new files: {new_files}")
    logging.info(f"Number of existing files: {existing_files}")

def process_directory(directory_path, dry_run):
    all_files = []
    for root, _, files in os.walk(directory_path):
        for file in files:
            all_files.append(os.path.join(root, file))

    total_files = len(all_files)
    logging.info(f"Total files to process: {total_files}")

    for i, file_path in enumerate(all_files):
        upload_file_to_db(file_path, dry_run)
        logging.info(f"Progress: {i + 1}/{total_files} files processed")


def generate_summary():
    """
    Generates a summary of the state of the file contents in the database.
    """
    total_files = collection.count_documents({})
    matching_hashes = collection.aggregate([
        {"$group": {"_id": "$file_hash", "count": {"$sum": 1}}},
        {"$match": {"count": {"$gt": 1}}}
    ])
    total_matching_hashes = sum(1 for _ in matching_hashes)

    logging.info(f"Total number of files in the database: {total_files}")
    logging.info(f"Number of matching file hashes: {total_matching_hashes}")
    
def digest_database():
    """
    Iterates through all documents in the database and adds a hash value for the file data to the metadata of the document.
    """
    total_files = collection.count_documents({})
    logging.info(f"Digesting metadata for {total_files} files in the database.")

    processed_files = 0
    for document in collection.find(no_cursor_timeout=True, batch_size=10):
        file_id = document['file_id']
        try:
            grid_out = fs.get(file_id)
            file_content = grid_out.read(1024 * 1024)  # Read 1MB at a time to limit memory usage
            grid_out.close()
            hasher = hashlib.sha256()
            while file_content:
                hasher.update(file_content)
                file_content = grid_out.read(1024 * 1024)  # Continue reading in chunks
            file_hash = hasher.hexdigest()

            # Update the document with the new hash
            collection.update_one({'_id': document['_id']}, {'$set': {'file_hash': file_hash}})
            logging.info(f"Updated hash for file with ID: {file_id}")
        except gridfs.errors.NoFile:
            logging.warning(f"File with ID {file_id} not found in GridFS.")

        processed_files += 1
        if processed_files % 10 == 0:
            logging.info(f"Progress: {processed_files}/{total_files} files processed")

def main():
    # Argument parser for command-line usage
    parser = argparse.ArgumentParser(description="Upload files in a directory to the database, perform database operations, or provide a summary of the current state.")
    parser.add_argument('directory', type=str, nargs='?', help="Path to the directory to upload. Required unless using --check-duplicates.")
    parser.add_argument('--dry-run', action='store_true', help="Perform a dry run without uploading files to the database.")
    parser.add_argument('--check', action='store_true', help="Check how many files are new and how many already exist in the database.")
    parser.add_argument('--summary', action='store_true', help="Provide a summary of the state of the file contents in the database.")
    parser.add_argument('--digest', action='store_true', help="Digest the database to add hash metadata for each file.")
    args = parser.parse_args()
    
    # Digest database if requested
    if args.digest:
        logging.info("Digesting the database to add or update metadata.")
        digest_database()
        return

    # Generate summary if requested
    if args.summary:
        logging.info("Generating summary of the database.")
        generate_summary()
        return
    if args.check:
        logging.info(f"Checking for existing files in directory: {args.directory}")
        check_existing_files(args.directory)
        return
    if args.directory:
        logging.info(f"Starting upload process for directory: {args.directory}")
        process_directory(args.directory, args.dry_run)
        logging.info("Upload process completed.")
        return

if __name__ == '__main__':
    main()

# Close the database connection when the script ends
client.close()
