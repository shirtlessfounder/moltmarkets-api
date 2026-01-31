"""
MoltMarkets Storage — API key hashing utility.
"""

import hashlib


def hash_api_key(key: str) -> str:
    """Hash an API key for storage."""
    return hashlib.sha256(key.encode()).hexdigest()
