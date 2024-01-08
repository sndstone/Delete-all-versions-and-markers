# Import the required modules
import boto3
from concurrent.futures import ThreadPoolExecutor, as_completed

# Function to read credentials from JSON file
def read_credentials_from_json(file_path):
    with open(file_path, "r") as json_file:
        credentials = json.load(json_file)
        return credentials

# Asks inputs to run run the script
JSON_IMPORT = input("Do you want to import JSON file for configuration? (yes/no): ")

if JSON_IMPORT.lower() == "yes":
    JSON_FILE_PATH = input("Enter the JSON file path: ")
    credentials = read_credentials_from_json(JSON_FILE_PATH)
    BUCKET_NAME = credentials["bucket_name"]
    S3_ENDPOINT_URL = credentials["s3_endpoint_url"]
    AWS_ACCESS_KEY_ID = credentials["aws_access_key_id"]
    AWS_SECRET_ACCESS_KEY = credentials["aws_secret_access_key"]
else:
    BUCKET_NAME = input("Enter the bucket name: ")
    S3_ENDPOINT_URL = input("Enter the S3 endpoint URL, (EXAMPLE http://example.com:443): ")
    AWS_ACCESS_KEY_ID = input("Enter the AWS access key ID: ")
    AWS_SECRET_ACCESS_KEY = input("Enter the AWS secret access key: ")

# Define the S3 client
s3_client = boto3.client(
    's3',
    verify=False,
    endpoint_url=S3_ENDPOINT_URL,
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY)

# Get a paginator for listing object versions
object_response_paginator = s3_client.get_paginator('list_object_versions')

# Initialize empty lists for delete markers and versions
delete_marker_list = []
version_list = []

# Iterate over the paginated response
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

# Delete delete markers in parallel
with ThreadPoolExecutor() as executor:
    # Create a list of futures for each batch of delete markers
    futures = []
    for i in range(0, len(delete_marker_list), 1000):
        # Delete the objects
        future = executor.submit(
            s3_client.delete_objects,
            Bucket=BUCKET_NAME,
            Delete={
                'Objects': delete_marker_list[i:i+1000],
                'Quiet': True
            }
        )
        # Append the future to the list
        futures.append(future)

    # Iterate over the futures as they are completed
    for future in as_completed(futures):
        # Print the result of each future
        print(future.result())

# Delete versions in parallel
with ThreadPoolExecutor() as executor:
    # Create a list of futures for each batch of versions
    futures = []
    for i in range(0, len(version_list), 1000):
        # Delete the objects
        future = executor.submit(
            s3_client.delete_objects,
            Bucket=BUCKET_NAME,
            Delete={
                'Objects': version_list[i:i+1000],
                'Quiet': True
            }
        )
        # Append the future to the list
        futures.append(future)

    # Iterate over the futures as they are completed
    for future in as_completed(futures):
        # Print the result of each future
        print(future.result())

# Delete versions in parallel
with ThreadPoolExecutor() as executor:
    # Create a list of futures for each batch of versions
    futures = []
    for i in range(0, len(version_list), 1000):
        # Delete the objects
        future = executor.submit(
            s3_client.delete_objects,
            Bucket=BUCKET_NAME,
            Delete={
                'Objects': version_list[i:i+1000],
                'Quiet': True
            }
        )
        # Append the future to the list
        futures.append(future)

    # Iterate over the futures as they are completed
    for future in as_completed(futures):
        # Print the result of each future
        print(future.result())
    exit()
