import os
import argparse
import logging
from pymongo import MongoClient
import gridfs
import hashlib

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Database connection
client = MongoClient('mongodb://localhost:27017/')
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
            logging.info(f"Uploaded file to GridFS with ID: {file_id}")
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

def check_and_delete_duplicates(dry_run):
    """
    Checks the database for duplicate original file names and performs a bit-level inspection to confirm duplicates, then deletes one.
    """
    duplicates = collection.aggregate([
        {"$group": {"_id": "$original_file_path", "count": {"$sum": 1}, "ids": {"$push": "$_id"}}},
        {"$match": {"count": {"$gt": 1}}}
    ])

    for duplicate in duplicates:
        if dry_run:
            logging.info(f"[Dry Run] Found duplicate files for original path: {duplicate['_id']}")
            continue
        file_ids = duplicate['ids']
        original_file_path = duplicate['_id']

        # Retrieve file contents for bit-level comparison
        file_hashes = {}
        for file_id in file_ids:
            grid_out = fs.get(file_id)
            file_content = grid_out.read()
            file_hash = hashlib.sha256(file_content).hexdigest()
            file_hashes[file_id] = file_hash

        # Find duplicates by comparing hashes
        unique_hashes = {}
        duplicates_to_delete = []
        for file_id, file_hash in file_hashes.items():
            if file_hash in unique_hashes:
                duplicates_to_delete.append(file_id)
            else:
                unique_hashes[file_hash] = file_id

        # Delete confirmed duplicates
        for file_id in duplicates_to_delete:
            fs.delete(file_id)
            logging.info(f"Deleted duplicate file with ID: {file_id} for original path: {original_file_path}")

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
    
    logging.info(f"Total number of files in the database: {total_files}")

def main():
    # Argument parser for command-line usage
    parser = argparse.ArgumentParser(description="Upload files in a directory to the database, perform database operations, or provide a summary of the current state.")
    parser.add_argument('directory', type=str, nargs='?', help="Path to the directory to upload. Required unless using --check-duplicates.")
    parser.add_argument('--dry-run', action='store_true', help="Perform a dry run without uploading files to the database.")
    parser.add_argument('--check', action='store_true', help="Check how many files are new and how many already exist in the database.")
    parser.add_argument('--check-duplicates', action='store_true', help="Check for duplicate original file names in the database and delete one after bit-level inspection.")
    parser.add_argument('--summary', action='store_true', help="Provide a summary of the state of the file contents in the database.")
    args = parser.parse_args()

    # Check for existing files if requested
    if args.check:
        if not args.directory:
            parser.error("The --check option requires a directory argument.")
        logging.info(f"Checking for existing files in directory: {args.directory}")
        check_existing_files(args.directory)

    # Check for duplicates if requested
    if args.check_duplicates:
        if args.directory:
            parser.error("The --check-duplicates option cannot be used with a directory argument.")
        logging.info("Checking for duplicate files in the database.")
        check_and_delete_duplicates(args.dry_run)

    # Process the provided directory
    if args.directory:
        logging.info(f"Starting upload process for directory: {args.directory}")
        process_directory(args.directory, args.dry_run)
        logging.info("Upload process completed.")

    # Generate summary if requested
    if args.summary:
        if args.directory:
            parser.error("The --summary option cannot be used with a directory argument.")
        logging.info("Generating summary of the database.")
        generate_summary()

if __name__ == '__main__':
    main()

# Close the database connection when the script ends
client.close()
