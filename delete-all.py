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
from itertools import islice, cycle

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
                    help='Maximum concurrent listing shards when LIST_PREFIXES env var is provided (default: 50)')
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
    'delete_attempted': 0,
    'delete_errors': 0,
    'pages_listed': 0,
    'list_requests': 0,
    'objects_found': 0,
    'list_backpressure_events': 0,
    'start_time': 0
}
stats_lock = threading.Lock()

# Thread-safe counter for deletion rate limiting
request_semaphore = threading.Semaphore(args.max_connections)
stop_event = threading.Event()  # Event to signal script termination


def increment_stat(name, value=1):
    with stats_lock:
        stats[name] += value


def get_stats_snapshot():
    with stats_lock:
        return dict(stats)

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
s3_client_cycle = cycle(s3_client_pool)
s3_client_cycle_lock = threading.Lock()

# Function to get a client from the pool
def get_s3_client():
    # Round-robin client selection with lock-protected cycle
    with s3_client_cycle_lock:
        return next(s3_client_cycle)

# Function to handle graceful shutdown
def signal_handler(sig, frame):
    logger.info("Shutdown signal received, cleaning up...")
    stop_event.set()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

def get_listing_prefixes():
    """Return explicit listing shard prefixes, bounded by --pipeline_size."""
    raw_prefixes = os.environ.get('LIST_PREFIXES', '')
    if not raw_prefixes.strip():
        return [None]

    prefixes = [p.strip() for p in raw_prefixes.split(',') if p.strip()]
    if not prefixes:
        return [None]

    return prefixes[:max(1, args.pipeline_size)]


async def queue_batch(output_queue, batch):
    """Put a batch into a bounded queue while tracking backpressure."""
    if output_queue.full():
        increment_stat('list_backpressure_events')
    await output_queue.put(batch)


# Function to print status periodically
def status_reporter(marker_queue, version_queue):
    last_time = time.time()
    last_pages = 0
    last_deleted = 0

    while not stop_event.is_set():
        time.sleep(5)

        current_time = time.time()
        elapsed = current_time - last_time
        snapshot = get_stats_snapshot()

        pages_delta = snapshot['pages_listed'] - last_pages
        deleted_delta = snapshot['objects_deleted'] - last_deleted
        page_rate = pages_delta / elapsed if elapsed > 0 else 0
        delete_rate = deleted_delta / elapsed if elapsed > 0 else 0

        marker_fill = marker_queue.qsize() / marker_queue.maxsize if marker_queue.maxsize else 0
        version_fill = version_queue.qsize() / version_queue.maxsize if version_queue.maxsize else 0

        delete_success_rate = 0
        if snapshot['delete_attempted'] > 0:
            delete_success_rate = (snapshot['objects_deleted'] / snapshot['delete_attempted']) * 100

        logger.info(
            "Metrics: pages=%s (%.2f/s), listed=%s, deleted=%s (%.1f/s), delete_errors=%s, "
            "delete_success=%.2f%%, backlog(marker=%s, version=%s), queue_fill(marker=%.1f%%, version=%.1f%%), "
            "backpressure_events=%s",
            f"{snapshot['pages_listed']:,}",
            page_rate,
            f"{snapshot['objects_found']:,}",
            f"{snapshot['objects_deleted']:,}",
            delete_rate,
            f"{snapshot['delete_errors']:,}",
            delete_success_rate,
            marker_queue.qsize(),
            version_queue.qsize(),
            marker_fill * 100,
            version_fill * 100,
            snapshot['list_backpressure_events'],
        )

        last_pages = snapshot['pages_listed']
        last_deleted = snapshot['objects_deleted']
        last_time = current_time


# Function to list object versions efficiently
async def list_object_versions(client, marker_batch_queue, version_batch_queue, prefix=None):
    """List object versions via paginator and stream batches to queues."""
    paginator = client.get_paginator('list_object_versions')
    pagination_config = {'PageSize': args.list_max_keys}
    operation_params = {'Bucket': BUCKET_NAME, 'PaginationConfig': pagination_config}
    if prefix is not None:
        operation_params['Prefix'] = prefix

    marker_batch = []
    version_batch = []

    try:
        for page in paginator.paginate(**operation_params):
            if stop_event.is_set():
                break

            increment_stat('list_requests')
            increment_stat('pages_listed')

            for marker in page.get('DeleteMarkers', []):
                marker_batch.append({'Key': marker['Key'], 'VersionId': marker['VersionId']})
                if len(marker_batch) >= args.batch_size:
                    await queue_batch(marker_batch_queue, marker_batch)
                    increment_stat('objects_found', len(marker_batch))
                    marker_batch = []

            for version in page.get('Versions', []):
                version_batch.append({'Key': version['Key'], 'VersionId': version['VersionId']})
                if len(version_batch) >= args.batch_size:
                    await queue_batch(version_batch_queue, version_batch)
                    increment_stat('objects_found', len(version_batch))
                    version_batch = []

            if args.deletion_delay > 0:
                await asyncio.sleep(args.deletion_delay)

        if marker_batch:
            await queue_batch(marker_batch_queue, marker_batch)
            increment_stat('objects_found', len(marker_batch))

        if version_batch:
            await queue_batch(version_batch_queue, version_batch)
            increment_stat('objects_found', len(version_batch))

    except Exception as e:
        logger.error(f"Error listing object versions for prefix {prefix!r}: {e}")
        logger.exception("Exception details:")

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
            increment_stat('delete_requests_sent')
            deleted_count = len(batch)
            increment_stat('delete_attempted', deleted_count)
            increment_stat('objects_deleted', deleted_count)
            
            # Check for errors
            error_count = 0
            if 'Errors' in result and result['Errors']:
                error_count = len(result['Errors'])
                increment_stat('delete_errors', error_count)
                
                # Only log a sample of errors to avoid flooding logs
                if error_count > 0 and get_stats_snapshot()['delete_errors'] % 100 == 1:
                    for i, error in enumerate(result['Errors'][:5]):  # Log at most 5 errors
                        logger.error(f"Delete error: {error}")
                    if error_count > 5:
                        logger.error(f"... and {error_count - 5} more errors")
            
            return deleted_count, error_count
            
    except Exception as e:
        logger.error(f"Batch deletion error: {e}")
        increment_stat('delete_errors')
        return 0, 1

# Worker function for deletion consumer
async def deletion_worker(worker_id, queue, executor):
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
                if queue.empty() and get_stats_snapshot()['list_requests'] > 0:
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
            deleted, errors = await loop.run_in_executor(executor, delete_object_batch, batch)
            
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
    """Main async function to orchestrate bounded producer-consumer processing."""
    logger.info(f"Starting high-performance S3 bucket cleanup for {BUCKET_NAME}")
    logger.info(f"Configuration: batch_size={args.batch_size}, max_workers={args.max_workers}, "
                f"max_connections={args.max_connections}, immediate_deletion={args.immediate_deletion}, "
                f"pipeline_size={args.pipeline_size}")

    if args.checksum:
        logger.info(f"Using {args.checksum} checksum algorithm for S3 operations")

    with stats_lock:
        stats['start_time'] = time.time()

    queue_depth = max(100, args.pipeline_size * 20)
    marker_queue = asyncio.Queue(maxsize=queue_depth)
    version_queue = asyncio.Queue(maxsize=queue_depth)

    status_thread = threading.Thread(target=status_reporter, args=(marker_queue, version_queue), daemon=True)
    status_thread.start()

    prefixes = get_listing_prefixes()
    active_listers = min(len(prefixes), max(1, args.pipeline_size))
    logger.info(f"Listing shards: {active_listers} (LIST_PREFIXES={'enabled' if prefixes != [None] else 'disabled'})")

    executor = ThreadPoolExecutor(max_workers=args.max_workers)
    try:
        deletion_workers = []
        marker_workers = max(1, args.max_workers // 3)
        version_workers = max(1, args.max_workers - marker_workers)

        for i in range(version_workers):
            deletion_workers.append(asyncio.create_task(deletion_worker(i, version_queue, executor)))
        for i in range(marker_workers):
            deletion_workers.append(asyncio.create_task(deletion_worker(version_workers + i, marker_queue, executor)))

        listing_tasks = []
        for idx, prefix in enumerate(prefixes[:active_listers]):
            client = s3_client_pool[idx % len(s3_client_pool)]
            listing_tasks.append(asyncio.create_task(list_object_versions(client, marker_queue, version_queue, prefix=prefix)))

        await asyncio.gather(*listing_tasks)
        logger.info("Object listing completed, draining deletion queues")

        for _ in range(marker_workers):
            await marker_queue.put(None)
        for _ in range(version_workers):
            await version_queue.put(None)

        await asyncio.gather(*deletion_workers)
        await marker_queue.join()
        await version_queue.join()
    finally:
        executor.shutdown(wait=True)
        stop_event.set()

    end_time = time.time()
    snapshot = get_stats_snapshot()
    elapsed_time = end_time - snapshot['start_time']
    hours, remainder = divmod(elapsed_time, 3600)
    minutes, seconds = divmod(remainder, 60)

    avg_rate = snapshot['objects_deleted'] / elapsed_time if elapsed_time > 0 else 0

    logger.info(f"Bucket cleanup completed in {int(hours)}h {int(minutes)}m {int(seconds)}s")
    logger.info(f"Objects processed: {snapshot['objects_found']:,}, Deleted: {snapshot['objects_deleted']:,}, "
                f"Errors: {snapshot['delete_errors']:,}, Pages: {snapshot['pages_listed']:,}")
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
