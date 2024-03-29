import argparse
import boto3
import json
import logging
import threading
import time  # Import the time module
from concurrent.futures import ThreadPoolExecutor, as_completed

# Set up argument parsing
parser = argparse.ArgumentParser(description='Process some integers.')
parser.add_argument('--logging', action='store_true', help='Enable debug logging to a file')
parser.add_argument('--json_file_path', type=str, help='JSON file path for configuration')
args = parser.parse_args()

# Set up logging
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)  # Set root logger to DEBUG level

# Create console handler with a higher log level
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)  # Set console handler to INFO level

# Create formatters and add it to the handlers
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
console_handler.setFormatter(formatter)

# Add the handlers to the logger
logger.addHandler(console_handler)

if args.logging:
    # Create file handler which logs even debug messages
    file_handler = logging.FileHandler('debug.log')
    file_handler.setLevel(logging.DEBUG)  # Set file handler to DEBUG level
    file_handler.setFormatter(formatter)
    # Add the handlers to the logger
    logger.addHandler(file_handler)

# Function to read credentials from JSON file
def read_credentials_from_json(file_path):
    try:
        with open(file_path, "r") as json_file:
            credentials = json.load(json_file)
            return credentials
    except Exception as e:
        logger.error(f"Failed to read JSON file: {e}")
        exit(1)

# Asks inputs to run run the script
if args.json_file_path:
    credentials = read_credentials_from_json(args.json_file_path)
    BUCKET_NAME = credentials.get("bucket_name")
    S3_ENDPOINT_URL = credentials.get("s3_endpoint_url")
    AWS_ACCESS_KEY_ID = credentials.get("aws_access_key_id")
    AWS_SECRET_ACCESS_KEY = credentials.get("aws_secret_access_key")
else:
    JSON_IMPORT = input("Do you want to import JSON file for configuration? (yes/no): ")
    if JSON_IMPORT.lower() == "yes":
        JSON_FILE_PATH = input("Enter the JSON file path: ")
        credentials = read_credentials_from_json(JSON_FILE_PATH)
        BUCKET_NAME = credentials.get("bucket_name")
        S3_ENDPOINT_URL = credentials.get("s3_endpoint_url")
        AWS_ACCESS_KEY_ID = credentials.get("aws_access_key_id")
        AWS_SECRET_ACCESS_KEY = credentials.get("aws_secret_access_key")
    else:
        BUCKET_NAME = input("Enter the bucket name: ")
        S3_ENDPOINT_URL = input("Enter the S3 endpoint URL, (EXAMPLE http://example.com:443): ")
        AWS_ACCESS_KEY_ID = input("Enter the AWS access key ID: ")
        AWS_SECRET_ACCESS_KEY = input("Enter the AWS secret access key: ")

# Define the S3 client
try:
    s3_client = boto3.client(
        's3',
        verify=False,
        endpoint_url=S3_ENDPOINT_URL,
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY)
except Exception as e:
    logging.error(f"Failed to create S3 client: {e}")
    exit(1)

# Get a paginator for listing object versions
try:
    object_response_paginator = s3_client.get_paginator('list_object_versions')
except Exception as e:
    logging.error(f"Failed to get paginator: {e}")
    exit(1)

# Initialize empty lists for delete markers and versions
delete_marker_list = []
version_list = []

# Function to print operation status
def print_status(operation):
    while not operation_done:
        logging.info(f"Current operation: {operation}")
        time.sleep(5)  # This function requires the time module

# Iterate over the paginated response
try:
    operation_done = False
    threading.Thread(target=print_status, args=("Listing object versions",)).start()
    for object_response_itr in object_response_paginator.paginate(Bucket=BUCKET_NAME):
        # Check for delete markers in the response
        if 'DeleteMarkers' in object_response_itr:
            for delete_marker in object_response_itr['DeleteMarkers']:
                # Append each delete marker to the list
                delete_marker_list.append({'Key': delete_marker['Key'], 'VersionId': delete_marker['VersionId']})

        # Check for versions in the response
        if 'Versions' in object_response_itr:
            for version in object_response_itr['Versions']:
                # Append each version to the list
                version_list.append({'Key': version['Key'], 'VersionId': version['VersionId']})
    operation_done = True
except Exception as e:
    logging.error(f"Failed to list object versions: {e}")
    exit(1)

# Function to delete objects
def delete_objects(object_list, object_type):
    operation_done = False
    threading.Thread(target=print_status, args=(f"Deleting {object_type}",)).start()
    with ThreadPoolExecutor() as executor:
        # Create a list of futures for each batch of delete markers
        futures = []
        for i in range(0, len(object_list), 1000):
            # Delete the objects
            try:
                future = executor.submit(
                    s3_client.delete_objects,
                    Bucket=BUCKET_NAME,
                    Delete={
                        'Objects': object_list[i:i+1000],
                        'Quiet': True
                    }
                )
                # Append the future to the list
                futures.append(future)
            except Exception as e:
                logging.error(f"Failed to submit delete task: {e}")
                continue

        # Iterate over the futures as they are completed
        for future in as_completed(futures):
            try:
                # Print the result of each future
                logging.info(future.result())
            except Exception as e:
                logging.error(f"Failed to delete objects: {e}")
    operation_done = True

# Delete delete markers and versions
delete_objects(delete_marker_list, "delete markers")
delete_objects(version_list, "versions")
