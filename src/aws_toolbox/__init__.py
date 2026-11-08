"""Functions and configuration to streamline AWS CLI and boto3 use."""

# Must run before botocore/urllib3 are imported anywhere: they bind
# ssl.SSLContext at import time, and injecting afterwards recurses.
import truststore

truststore.inject_into_ssl()

__version__ = "0.1.0"
