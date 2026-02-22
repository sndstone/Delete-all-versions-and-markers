import argparse
import boto3
import json
import logging
import threading
import time
import asyncio
import signal
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
from botocore.config import Config
from itertools import cycle

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
parser.add_argument('--max_connections', type=int, default=1000,
                    help='Maximum concurrent connections (default: 1000)')
parser.add_argument('--pipeline_size', type=int, default=50,
                    help='Maximum concurrent listing shards when LIST_PREFIXES env var is provided (default: 50)')
parser.add_argument('--list_max_keys', type=int, default=1000,
                    help='Maximum keys per list request (default: 1000)')
parser.add_argument('--immediate-deletion', dest='immediate_deletion', action='store_true',
                    help='Start deleting objects while listing (default)')
parser.add_argument('--no-immediate-deletion', dest='immediate_deletion', action='store_false',
                    help='List all objects first, then start deletion')
parser.set_defaults(immediate_deletion=True)
parser.add_argument('--deletion_delay', type=float, default=0,
                    help='Delay in seconds between deletion batches to avoid overwhelming the S3 service (default: 0)')
parser.add_argument('--bypass-governance-retention', action='store_true',
                    help='Set BypassGovernanceRetention on delete_objects requests (requires permissions/policy support)')
args = parser.parse_args()

if args.max_passes < 1:
    parser.error('--max_passes must be at least 1')
if args.stable_empty_passes < 1:
    parser.error('--stable_empty_passes must be at least 1')

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
    'objects_unresolved': 0,
    'list_requests': 0,
    'objects_found': 0,
    'list_backpressure_events': 0,
    'start_time': 0
}
stats_lock = threading.Lock()

failure_summary = defaultdict(lambda: {
    'count': 0,
    'samples': []
})
failure_summary_lock = threading.Lock()

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
                          f"({stats['delete_errors']} errors, {stats['objects_unresolved']} unresolved) - "
                          f"Current rate: {delete_rate:.1f}/s, Average: {avg_rate:.1f}/s"
                          f"{eta_str}")
            
        last_objects_deleted = stats['objects_deleted']
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
    finally:
        logger.info(f"Finished listing objects, found {stats['objects_found']} objects")

# Function to delete objects in batch
def delete_object_batch(batch):
    """Delete a batch of objects"""
    if not batch:
        return 0, 0
        
    # Get a client from the pool
    client = get_s3_client()
    pending_objects = list(batch)
    total_deleted = 0
    
    try:
        for retry_attempt in range(args.max_retries + 1):
            with request_semaphore:
                result = client.delete_objects(
                    Bucket=BUCKET_NAME,
                    Delete={
                        'Objects': pending_objects,
                        'Quiet': False
                    }
                )

            stats['delete_requests_sent'] += 1

            deleted_items = result.get('Deleted', [])
            deleted_count = len(deleted_items)
            total_deleted += deleted_count
            stats['objects_deleted'] += deleted_count

            errors = result.get('Errors', [])
            if not errors:
                return total_deleted, 0

            pending_by_id = {
                (obj.get('Key'), obj.get('VersionId')): obj
                for obj in pending_objects
            }

            failed_objects = []
            for error in errors:
                failed_object = pending_by_id.get((error.get('Key'), error.get('VersionId')))
                if failed_object is None:
                    failed_object = {
                        'Key': error.get('Key'),
                        'VersionId': error.get('VersionId')
                    }
                failed_objects.append(failed_object)

            if retry_attempt >= args.max_retries:
                final_error_count = len(failed_objects)
                stats['delete_errors'] += final_error_count
                for error, failed_object in zip(errors, failed_objects):
                    logger.error(
                        "Unresolved delete failure: key=%s version=%s code=%s message=%s",
                        failed_object.get('Key'),
                        failed_object.get('VersionId'),
                        error.get('Code', 'Unknown'),
                        error.get('Message', '')
                    )
                return total_deleted, final_error_count

            sleep_seconds = min(5, 0.1 * (2 ** retry_attempt))
            logger.warning(
                "Retrying %s failed object deletions (attempt %s/%s) in %.2fs",
                len(failed_objects),
                retry_attempt + 1,
                args.max_retries,
                sleep_seconds
            )
            time.sleep(sleep_seconds)
            pending_objects = failed_objects

    except Exception as e:
        logger.error(f"Batch deletion error: {e}")
        unresolved_errors = len(pending_objects)
        stats['delete_errors'] += unresolved_errors
        for failed_object in pending_objects:
            logger.error(
                "Unresolved delete failure after exception: key=%s version=%s code=%s",
                failed_object.get('Key'),
                failed_object.get('VersionId'),
                'Exception'
            )
        return total_deleted, unresolved_errors

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

    executor = ThreadPoolExecutor(max_workers=args.max_workers)
    try:
        if args.immediate_deletion:
            version_worker_count = int(args.max_workers * 0.7)
        else:
            version_worker_count = args.max_workers - (args.max_workers // 2)
        marker_worker_count = args.max_workers - version_worker_count

        logger.info(
            "Worker assignment: version_workers=%s, marker_workers=%s",
            version_worker_count,
            marker_worker_count,
        )

        async def wait_for_queue_drain(marker_sentinels_sent, version_sentinels_sent):
            logger.info(
                "Shutdown: sent sentinel signals (marker=%s, version=%s)",
                marker_sentinels_sent,
                version_sentinels_sent,
            )
            logger.info(
                "Shutdown: queue sizes before join (marker=%s, version=%s)",
                marker_queue.qsize(),
                version_queue.qsize(),
            )
            await marker_queue.join()
            await version_queue.join()
            logger.info(
                "Shutdown: queue sizes after join (marker=%s, version=%s)",
                marker_queue.qsize(),
                version_queue.qsize(),
            )

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
                queue_to_use = version_queue if i < version_worker_count else marker_queue
                worker = asyncio.create_task(deletion_worker(i, queue_to_use, executor))
                deletion_workers.append(worker)

            # Wait for listing to complete
            await listing_task
            logger.info("Object listing completed, waiting for deletion to finish")

            # Send termination signals to workers
            for _ in range(marker_worker_count):
                await marker_queue.put(None)
            for _ in range(version_worker_count):
                await version_queue.put(None)

            # Wait for all tasks to complete
            await asyncio.gather(*deletion_workers)

            # Wait for queues to be fully processed
            await wait_for_queue_drain(marker_worker_count, version_worker_count)
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
                worker = asyncio.create_task(
                    deletion_worker(i, marker_queue if i < marker_worker_count else version_queue, executor)
                )
                deletion_workers.append(worker)

            # Send termination signals to workers when they're done
            for _ in range(marker_worker_count):
                await marker_queue.put(None)
            for _ in range(version_worker_count):
                await version_queue.put(None)

    prefixes = get_listing_prefixes()
    active_listers = min(len(prefixes), max(1, args.pipeline_size))
    logger.info(f"Listing shards: {active_listers} (LIST_PREFIXES={'enabled' if prefixes != [None] else 'disabled'})")

            # Wait for queues to be fully processed
            await wait_for_queue_drain(marker_worker_count, version_worker_count)
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
    logger.info(f"Objects processed: {stats['objects_found']:,}, Deleted: {stats['objects_deleted']:,}, "
              f"Errors: {stats['delete_errors']:,}, Unresolved: {stats['objects_unresolved']:,}")
    logger.info(f"Average deletion rate: {avg_rate:.1f} objects/second")

    if args.bypass_governance_retention:
        logger.info("Governance retention bypass mode was enabled (--bypass-governance-retention)")

    log_failure_summary()

    if stats['objects_unresolved'] > 0:
        logger.error("Cleanup completed with unresolved object versions/delete markers")
        return 2
    
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
