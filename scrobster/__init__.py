"""Load .env before anything else.

Third-party libraries read the environment while they import. pylast builds its
HTTPS client, and that client reads SSL_CERT_FILE once. Importing config here
means .env is applied whatever a caller imports first.
"""
from . import config  # noqa: F401
