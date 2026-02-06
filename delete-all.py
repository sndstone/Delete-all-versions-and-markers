import argparse
import boto3
import json
import logging
import threading
import time
import os
import multiprocessing
import queue
import asyncio
import concurrent.futures
import signal
import sys
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
from botocore.config import Config
from functools import partial
from itertools import islice

# Set up argument parsing
parser = argparse.ArgumentParser(description='High-performance S3 bucket cleanup tool.')
parser.add_argument('--debug', action='store_true', help='Enable debug logging to a file')
parser.add_argument('--json_file_path', type=str, help='JSON file path for configuration')
parser.add_argument('--checksum', type=str, choices=['CRC32', 'CRC32C', 'SHA1', 'SHA256', 'MD5'], 
                    help='Checksum algorithm to use for S3 operations')
parser.add_argument('--batch_size', type=int, default=1000, 
                    help='Batch size for delete operations (default: 1000)')
parser.add_argument('--max_workers', type=int, default=50, 
                    help='Maximum number of worker threads (default: 50)')
parser.add_argument('--max_retries', type=int, default=5,
                    help='Maximum number of retries for failed API calls (default: 5)')
parser.add_argument('--retry_mode', type=str, choices=['standard', 'adaptive'], default='adaptive',
                    help='Retry mode for AWS API calls (default: adaptive)')
parser.add_argument('--max_requests_per_second', type=int, default=10000,
                    help='Maximum S3 API requests per second (default: 10000)')
parser.add_argument('--max_connections', type=int, default=1000,
                    help='Maximum concurrent connections (default: 1000)')
parser.add_argument('--pipeline_size', type=int, default=50,
                    help='Number of simultaneous object listing operations (default: 50)')
parser.add_argument('--list_max_keys', type=int, default=1000,
                    help='Maximum keys per list request (default: 1000)')
parser.add_argument('--immediate_deletion', action='store_true', default=True,
                    help='Start deleting objects immediately while listing (default: True)')
parser.add_argument('--deletion_delay', type=float, default=0,
                    help='Delay in seconds between deletion batches to avoid overwhelming the S3 service (default: 0)')
args = parser.parse_args()

# Set up logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Create console handler with INFO log level
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)

# Create formatters and add it to the handlers
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
console_handler.setFormatter(formatter)

# Add the console handler to the logger
logger.addHandler(console_handler)

# Set up debug logging to file if requested
if args.debug:
    logger.setLevel(logging.DEBUG)
    file_handler = logging.FileHandler('debug.log')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.debug("Debug logging enabled")

# Global variables for tracking progress
stats = {
    'delete_requests_sent': 0,
    'objects_deleted': 0,
    'delete_errors': 0,
    'list_requests': 0,
    'objects_found': 0,
    'start_time': 0
}

# Thread-safe counter for deletion rate limiting
request_semaphore = threading.Semaphore(args.max_connections)
deletion_queue = queue.Queue(maxsize=10000)  # Buffer for objects to delete
stop_event = threading.Event()  # Event to signal script termination

# Function to read credentials from JSON file
def read_credentials_from_json(file_path):
    try:
        with open(file_path, "r") as json_file:
            credentials = json.load(json_file)
            return credentials
    except Exception as e:
        logger.error(f"Failed to read JSON file: {e}")
        exit(1)

# Get configuration either from JSON file or user input
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

# Configure S3 client with highly optimized settings
s3_config = Config(
    retries={
        'max_attempts': args.max_retries,
        'mode': args.retry_mode
    },
    max_pool_connections=args.max_connections,
    connect_timeout=1,  # Fast connection timeout for quick failure detection
    read_timeout=30,    # Reasonable read timeout for S3 operations
    tcp_keepalive=True  # Keep connections alive
)

# Add checksum if specified
if args.checksum:
    s3_config = Config(
        retries={
            'max_attempts': args.max_retries,
            'mode': args.retry_mode
        },
        s3={
            'payload_signing_enabled': True,
            'checksum_algorithm': args.checksum,
            'addressing_style': 'path',  # More efficient URL style
            'us_east_1_regional_endpoint': 'regional'  # Use regional endpoint for better performance
        },
        max_pool_connections=args.max_connections,
        connect_timeout=1,
        read_timeout=30,
        tcp_keepalive=True
    )
    logger.info(f"Using {args.checksum} checksum algorithm for S3 operations")

# Create S3 client pools for better throughput
def create_s3_client():
    try:
        return boto3.client(
            's3',
            verify=False,
            endpoint_url=S3_ENDPOINT_URL,
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
            config=s3_config)
    except Exception as e:
        logger.error(f"Failed to create S3 client: {e}")
        exit(1)

# Create a pool of S3 clients for better connection distribution
s3_client_pool = [create_s3_client() for _ in range(min(20, args.max_workers))]
s3_client = s3_client_pool[0]  # Main client for single operations

# Function to get a client from the pool
def get_s3_client():
    # Round-robin client selection
    client_idx = stats['delete_requests_sent'] % len(s3_client_pool)
    return s3_client_pool[client_idx]

# Function to handle graceful shutdown
def signal_handler(sig, frame):
    logger.info("Shutdown signal received, cleaning up...")
    stop_event.set()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# Function to print status periodically
def status_reporter():
    last_objects_deleted = 0
    last_time = time.time()
    
    while not stop_event.is_set():
        time.sleep(5)  # Update every 5 seconds
        
        current_time = time.time()
        elapsed = current_time - last_time
        deleted_since_last = stats['objects_deleted'] - last_objects_deleted
        
        if elapsed > 0:
            delete_rate = deleted_since_last / elapsed
            total_elapsed = current_time - stats['start_time'] if stats['start_time'] > 0 else 0
            
            if total_elapsed > 0:
                avg_rate = stats['objects_deleted'] / total_elapsed
                
                # Calculate ETA if we know the total objects
                if stats['objects_found'] > 0:
                    remaining = stats['objects_found'] - stats['objects_deleted']
                    eta_seconds = remaining / avg_rate if avg_rate > 0 else 0
                    eta_mins = int(eta_seconds / 60)
                    eta_secs = int(eta_seconds % 60)
                    eta_str = f", ETA: {eta_mins}m {eta_secs}s"
                else:
                    eta_str = ""
                
                # Print status with current and average rates
                logger.info(f"Status: Deleted {stats['objects_deleted']:,}/{stats['objects_found']:,} objects "
                          f"({stats['delete_errors']} errors) - "
                          f"Current rate: {delete_rate:.1f}/s, Average: {avg_rate:.1f}/s"
                          f"{eta_str}")
            
        last_objects_deleted = stats['objects_deleted']
        last_time = current_time

# Function to list object versions efficiently
async def list_object_versions(client, marker_batch_queue, version_batch_queue):
    """List object versions and feed them into the processing queue"""
    try:
        # Create multiple paginators for better performance
        paginators = [client.get_paginator('list_object_versions') for _ in range(min(5, args.max_workers // 10))]
        
        # Pagination parameters
        pagination_config = {
            'MaxItems': None,  # No limit to total items
            'PageSize': args.list_max_keys,  # Maximum keys per page
        }
        
        # Get initial page to find if we need continuations
        initial_response = client.list_object_versions(
            Bucket=BUCKET_NAME,
            MaxKeys=args.list_max_keys
        )
        
        # Process initial page
        version_batch = []
        marker_batch = []
        stats['list_requests'] += 1
        
        # Process delete markers from initial page
        if 'DeleteMarkers' in initial_response:
            for marker in initial_response['DeleteMarkers']:
                marker_batch.append({
                    'Key': marker['Key'],
                    'VersionId': marker['VersionId']
                })
                
                # Send batch immediately if full
                if len(marker_batch) >= args.batch_size:
                    await marker_batch_queue.put(marker_batch.copy())
                    stats['objects_found'] += len(marker_batch)
                    logger.debug(f"Queued {len(marker_batch)} delete markers for processing")
                    marker_batch = []
        
        # Process object versions from initial page
        if 'Versions' in initial_response:
            for version in initial_response['Versions']:
                version_batch.append({
                    'Key': version['Key'],
                    'VersionId': version['VersionId']
                })
                
                # Send batch immediately if full
                if len(version_batch) >= args.batch_size:
                    await version_batch_queue.put(version_batch.copy())
                    stats['objects_found'] += len(version_batch)
                    logger.debug(f"Queued {len(version_batch)} versions for processing")
                    version_batch = []
        
        # Check if we need to continue with pagination
        is_truncated = initial_response.get('IsTruncated', False)
        
        # If there are more objects to list
        if is_truncated:
            logger.info("Initial page processed, continuing with pagination")
            
            # Setup pagination iterator
            paginator = paginators[0]
            page_iterator = paginator.paginate(
                Bucket=BUCKET_NAME,
                PaginationConfig=pagination_config
            )
            
            # Skip the first page as we've already processed it
            page_count = 1  # Start at 1 since we already processed initial page
            
            for page in page_iterator:
                if page_count == 1:
                    # Skip first page from paginator as we already processed it
                    page_count += 1
                    continue
                    
                page_count += 1
                stats['list_requests'] += 1
                
                # Process delete markers
                if 'DeleteMarkers' in page:
                    for marker in page['DeleteMarkers']:
                        marker_batch.append({
                            'Key': marker['Key'],
                            'VersionId': marker['VersionId']
                        })
                        
                        if len(marker_batch) >= args.batch_size:
                            await marker_batch_queue.put(marker_batch.copy())
                            stats['objects_found'] += len(marker_batch)
                            logger.debug(f"Queued {len(marker_batch)} delete markers for processing")
                            marker_batch = []
                
                # Process object versions
                if 'Versions' in page:
                    for version in page['Versions']:
                        version_batch.append({
                            'Key': version['Key'],
                            'VersionId': version['VersionId']
                        })
                        
                        if len(version_batch) >= args.batch_size:
                            await version_batch_queue.put(version_batch.copy())
                            stats['objects_found'] += len(version_batch)
                            logger.debug(f"Queued {len(version_batch)} versions for processing")
                            version_batch = []
                
                # Check if we need to stop
                if stop_event.is_set():
                    break
                    
                # Log progress periodically
                if page_count % 10 == 0:
                    logger.debug(f"Listed {page_count} pages of objects")
                    
                # Apply small delay if requested to avoid overwhelming S3
                if args.deletion_delay > 0:
                    await asyncio.sleep(args.deletion_delay)
        
        # Put any remaining batches in the queue
        if marker_batch:
            await marker_batch_queue.put(marker_batch)
            stats['objects_found'] += len(marker_batch)
            logger.debug(f"Queued final {len(marker_batch)} delete markers for processing")
            
        if version_batch:
            await version_batch_queue.put(version_batch)
            stats['objects_found'] += len(version_batch)
            logger.debug(f"Queued final {len(version_batch)} versions for processing")
            
    except Exception as e:
        logger.error(f"Error listing object versions: {e}")
        logger.exception("Exception details:")
    finally:
        # Signal completion
        await marker_batch_queue.put(None)
        await version_batch_queue.put(None)
        logger.info(f"Finished listing objects, found {stats['objects_found']} objects")

# Function to delete objects in batch
def delete_object_batch(batch):
    """Delete a batch of objects"""
    if not batch:
        return 0, 0
        
    # Get a client from the pool
    client = get_s3_client()
    
    try:
        with request_semaphore:
            result = client.delete_objects(
                Bucket=BUCKET_NAME,
                Delete={
                    'Objects': batch,
                    'Quiet': True
                }
            )
            
            # Update stats
            stats['delete_requests_sent'] += 1
            deleted_count = len(batch)
            stats['objects_deleted'] += deleted_count
            
            # Check for errors
            error_count = 0
            if 'Errors' in result and result['Errors']:
                error_count = len(result['Errors'])
                stats['delete_errors'] += error_count
                
                # Only log a sample of errors to avoid flooding logs
                if error_count > 0 and stats['delete_errors'] % 100 == 1:
                    for i, error in enumerate(result['Errors'][:5]):  # Log at most 5 errors
                        logger.error(f"Delete error: {error}")
                    if error_count > 5:
                        logger.error(f"... and {error_count - 5} more errors")
            
            return deleted_count, error_count
            
    except Exception as e:
        logger.error(f"Batch deletion error: {e}")
        stats['delete_errors'] += 1
        return 0, 1

# Worker function for deletion consumer
async def deletion_worker(worker_id, queue):
    """Process batches from the deletion queue"""
    logger.debug(f"Deletion worker {worker_id} starting")
    
    consecutive_failures = 0
    backoff_time = 0.1  # initial backoff time in seconds
    
    while not stop_event.is_set():
        try:
            # Apply adaptive backoff if we're having issues
            if backoff_time > 0.1:
                await asyncio.sleep(backoff_time)
            
            # Get a batch to delete with a timeout
            try:
                batch = await asyncio.wait_for(queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                # If we've been waiting too long, check if listing is complete
                if queue.empty() and stats['list_requests'] > 0:
                    logger.debug(f"Worker {worker_id} timed out waiting for items, checking if we're done")
                    # Allow worker to exit if there are no more items expected
                    continue
                else:
                    # Otherwise keep waiting
                    continue
            
            # None is our signal to stop
            if batch is None:
                queue.task_done()
                break
                
            # Process this batch with the thread pool
            loop = asyncio.get_running_loop()
            deleted, errors = await loop.run_in_executor(None, delete_object_batch, batch)
            
            # Reset backoff on success
            if errors == 0:
                consecutive_failures = 0
                backoff_time = 0.1
            else:
                # Increment failures and increase backoff
                consecutive_failures += 1
                if consecutive_failures > 3:
                    backoff_time = min(backoff_time * 2, 5.0)  # Exponential backoff up to 5 seconds
                    logger.warning(f"Worker {worker_id} experiencing consecutive failures, backing off for {backoff_time}s")
            
            # Apply custom delay if configured
            if args.deletion_delay > 0:
                await asyncio.sleep(args.deletion_delay)
                
            # Mark task as done
            queue.task_done()
            
        except Exception as e:
            logger.error(f"Error in deletion worker {worker_id}: {e}")
            consecutive_failures += 1
            
            if consecutive_failures > 5:
                logger.warning(f"Worker {worker_id} experiencing too many errors, backing off...")
                await asyncio.sleep(min(backoff_time * 2, 10.0))
                
            if queue.qsize() > 0:
                queue.task_done()
    
    logger.debug(f"Deletion worker {worker_id} stopping")

# Main async processing function
async def process_bucket():
    """Main async function to orchestrate the bucket processing"""
    logger.info(f"Starting high-performance S3 bucket cleanup for {BUCKET_NAME}")
    logger.info(f"Configuration: batch_size={args.batch_size}, max_workers={args.max_workers}, "
                f"max_connections={args.max_connections}, immediate_deletion={args.immediate_deletion}")
    
    if args.checksum:
        logger.info(f"Using {args.checksum} checksum algorithm for S3 operations")
    
    # Set start time for statistics
    stats['start_time'] = time.time()
    
    # Create queues for object batches
    marker_queue = asyncio.Queue(maxsize=2000)  # Increased queue size for better buffering
    version_queue = asyncio.Queue(maxsize=2000)  # Increased queue size for better buffering
    
    # Start status reporting thread
    status_thread = threading.Thread(target=status_reporter)
    status_thread.daemon = True
    status_thread.start()
    
    # Start listing task first if using immediate deletion
    if args.immediate_deletion:
        # Start listing task
        listing_task = asyncio.create_task(list_object_versions(s3_client, marker_queue, version_queue))
        
        # Give listing a small head start to fill queues
        await asyncio.sleep(0.5)
        
        # Start worker tasks for object deletion
        deletion_workers = []
        logger.info(f"Starting {args.max_workers} deletion workers with immediate processing")
        
        for i in range(args.max_workers):
            # Distribute workers - more for versions if that's typically more common
            queue_to_use = version_queue if i < (args.max_workers * 0.7) else marker_queue
            worker = asyncio.create_task(deletion_worker(i, queue_to_use))
            deletion_workers.append(worker)
        
        # Wait for listing to complete
        await listing_task
        logger.info("Object listing completed, waiting for deletion to finish")
        
        # Send termination signals to workers
        for i in range(args.max_workers):
            if i < (args.max_workers * 0.7):
                await version_queue.put(None)
            else:
                await marker_queue.put(None)
        
        # Wait for all tasks to complete
        await asyncio.gather(*deletion_workers)
        
        # Wait for queues to be fully processed
        await marker_queue.join()
        await version_queue.join()
    else:
        # Traditional approach - wait for listing to complete first
        logger.info("Using traditional mode: listing all objects before deleting")
        
        # Start listing task
        listing_task = asyncio.create_task(list_object_versions(s3_client, marker_queue, version_queue))
        
        # Wait for listing to complete
        await listing_task
        
        # Start worker tasks for object deletion
        deletion_workers = []
        for i in range(args.max_workers):
            worker = asyncio.create_task(deletion_worker(i, marker_queue if i < args.max_workers/2 else version_queue))
            deletion_workers.append(worker)
        
        # Send termination signals to workers when they're done
        for _ in range(args.max_workers // 2):
            await marker_queue.put(None)
        for _ in range(args.max_workers - args.max_workers // 2):
            await version_queue.put(None)
        
        # Wait for all tasks to complete
        await asyncio.gather(*deletion_workers)
        
        # Wait for queues to be fully processed
        await marker_queue.join()
        await version_queue.join()
    
    # Calculate statistics
    end_time = time.time()
    elapsed_time = end_time - stats['start_time']
    hours, remainder = divmod(elapsed_time, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    avg_rate = stats['objects_deleted'] / elapsed_time if elapsed_time > 0 else 0
    
    logger.info(f"Bucket cleanup completed in {int(hours)}h {int(minutes)}m {int(seconds)}s")
    logger.info(f"Objects processed: {stats['objects_found']:,}, Deleted: {stats['objects_deleted']:,}, "
              f"Errors: {stats['delete_errors']:,}")
    logger.info(f"Average deletion rate: {avg_rate:.1f} objects/second")
    
    return 0

# Main entry point
def main():
    try:
        if sys.platform == 'win32':
            # Windows requires specific setup for asyncio
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        
        # Run the async processing function
        return asyncio.run(process_bucket())
    except KeyboardInterrupt:
        logger.info("Process interrupted by user")
        stop_event.set()
        return 130
    except Exception as e:
        logger.error(f"Script failed with error: {e}")
        logger.exception("Exception details:")
        return 1

if __name__ == "__main__":
    exit(main())
