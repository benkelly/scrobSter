"""One-time Last.fm authorization.

This produces a session key, so scrobSter never stores your password. You can
revoke the key at any time in your Last.fm account settings.

Step 1:  python -m scrobster.auth
Step 2:  python -m scrobster.auth <token>     (after you allow access)
"""
import sys

import pylast

from . import config


def main():
    if not (config.LASTFM_API_KEY and config.LASTFM_API_SECRET):
        sys.exit("Set LASTFM_API_KEY and LASTFM_API_SECRET first.")
    network = pylast.LastFMNetwork(api_key=config.LASTFM_API_KEY,
                                   api_secret=config.LASTFM_API_SECRET)
    generator = pylast.SessionKeyGenerator(network)

    if len(sys.argv) < 2:
        url = generator.get_web_auth_url()
        token = url.split("token=")[-1]
        print("Step 1. Open this page and choose 'Yes, allow access':\n")
        print(f"   {url}\n")
        print("Step 2. Then run:\n")
        print(f"   python -m scrobster.auth {token}\n")
        return

    key, username = generator.get_web_auth_session_key_username("", sys.argv[1])
    print("Authorized. Add these to the environment:\n")
    print(f"   LASTFM_SESSION_KEY={key}")
    print(f"   LASTFM_USERNAME={username}")


if __name__ == "__main__":
    main()
