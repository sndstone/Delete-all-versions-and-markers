# Delete-all-versions-and-markers

⚠️ **DANGER: This script permanently deletes _all_ object versions and delete markers in the target S3 bucket.**

## Overview
`delete-all.py` is a high-performance cleanup tool for **versioned S3 buckets**. It lists every object version and delete marker, then deletes them in batches using concurrent workers. It supports immediate deletion while listing, optional checksum configuration, and throttling to avoid overwhelming S3-compatible endpoints.

## Requirements
- Python 3
- `boto3`
- Network access to the target S3 endpoint
- Credentials with permission to list object versions and delete them

## Configuration
The script can read configuration from a JSON file or prompt for it interactively.

### JSON configuration fields
Provide a JSON file with the following keys:
```json
{
  "bucket_name": "my-bucket",
  "s3_endpoint_url": "https://s3.example.com",
  "aws_access_key_id": "AKIA...",
  "aws_secret_access_key": "..."
}
```

## Usage
```bash
python3 delete-all.py [options]
```

### Command-line options
| Option | Type | Default | Description |
| --- | --- | --- | --- |
| `--debug` | flag | off | Enable debug logging to `debug.log`. |
| `--json_file_path` | string | none | Path to JSON configuration file. If omitted, the script prompts for configuration. |
| `--checksum` | string | none | Checksum algorithm for S3 operations. Choices: `CRC32`, `CRC32C`, `SHA1`, `SHA256`, `MD5`. |
| `--batch_size` | int | `1000` | Batch size for delete operations. |
| `--max_workers` | int | `50` | Maximum number of concurrent deletion worker threads. |
| `--max_retries` | int | `5` | Maximum retry attempts for failed API calls. |
| `--retry_mode` | string | `adaptive` | Retry mode for AWS API calls. Choices: `standard`, `adaptive`. |
| `--max_requests_per_second` | int | `10000` | Maximum S3 API requests per second (currently unused in throttling logic). |
| `--max_connections` | int | `1000` | Maximum concurrent connections in the connection pool. |
| `--pipeline_size` | int | `50` | Number of simultaneous listing operations (currently unused in the listing logic). |
| `--list_max_keys` | int | `1000` | Maximum keys per list request. |
| `--immediate_deletion` | flag | on | Start deleting objects immediately while listing (enabled by default). |
| `--deletion_delay` | float | `0` | Delay in seconds between deletion batches. |

## Examples
### 1) Use interactive prompts
```bash
python3 delete-all.py
```

### 2) Use a JSON config file
```bash
python3 delete-all.py --json_file_path ./credentials.json
```

### 3) Enable debug logging and throttle deletion batches
```bash
python3 delete-all.py --debug --deletion_delay 0.2
```

### 4) Tune for a smaller environment
```bash
python3 delete-all.py \
  --json_file_path ./credentials.json \
  --batch_size 500 \
  --max_workers 20 \
  --max_connections 100
```

## Notes
- The tool deletes **every version** and **every delete marker** in the bucket, so it is destructive and irreversible.
- `--max_requests_per_second` and `--pipeline_size` are parsed but currently not applied in the control flow.
- If `--immediate_deletion` is enabled (default), deletion begins while listing is still in progress.
