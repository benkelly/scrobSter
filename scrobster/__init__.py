"""Load .env before anything else.

Third-party libraries read the environment while they import. pylast builds its
HTTPS client, and that client reads SSL_CERT_FILE once. Importing config here
means .env is applied whatever a caller imports first.
"""
from . import config  # noqa: F401

# The release number. CI reads this on a merge to main: a value with no tag yet
# is published as that image tag and tagged v<version>, so a bump here is what
# cuts a release.
__version__ = "0.2.3"
