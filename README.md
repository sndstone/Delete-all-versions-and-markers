# Delete-all-versions-and-markers
!!!WARNING THIS WILL DELETE ALL DATA IN THE SPECIFIED BUCKET!!!

This script deletes all versions and delete markers from an S3 bucket using Boto3. It is a work in progress, so please let me know if you have any ideas to improve it and I will add them to the project.

To run the script, please follow these steps:
1. Install Python3 and boto3 on your machine
2. Run the script using the command "python3 deleteall.py"
3. Enter the required information when prompted:
   - Bucket name: the name of the S3 bucket you want to clear out
   - S3 endpoint URL and port number: the URL and port number of the S3 endpoint you are using
   - AWS access key ID: your AWS access key ID
   - AWS secret access key: your AWS secret access key

Please note that this script will permanently delete all versions and delete markers from the specified S3 bucket. Use with caution.
