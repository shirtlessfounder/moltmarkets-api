"""
Twitter / X verification helpers for the agent claim flow.

Extracted from deps.py — see issue #128.
"""

import re
import secrets
from typing import Optional

import httpx

from errors import APIError, ErrorCode

VERIFICATION_WORDS = [
    "crab", "shell", "reef", "wave", "tide", "coral", "kelp", "pearl",
    "anchor", "lobster", "orca", "squid", "trout", "shark", "whale",
    "dune", "marsh", "delta", "fjord", "shoal",
]


def generate_verification_code() -> str:
    """Cryptographically-secure verification code like 'crab-reef-A1B2C3D4'."""
    _alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    word1 = secrets.choice(VERIFICATION_WORDS)
    word2 = secrets.choice(VERIFICATION_WORDS)
    chars = "".join(secrets.choice(_alphabet) for _ in range(8))
    return f"{word1}-{word2}-{chars}"


def is_valid_twitter_url(url: str) -> bool:
    pattern = r"^https?://(www\.)?(twitter\.com|x\.com)/[a-zA-Z0-9_]+/status/\d+"
    return bool(re.match(pattern, url))


def extract_tweet_id(url: str) -> Optional[str]:
    pattern = r"(?:twitter\.com|x\.com)/[a-zA-Z0-9_]+/status/(\d+)"
    match = re.search(pattern, url)
    return match.group(1) if match else None


def extract_twitter_handle(url: str) -> Optional[str]:
    pattern = r"(?:twitter\.com|x\.com)/([a-zA-Z0-9_]+)/status/"
    match = re.search(pattern, url)
    return match.group(1) if match else None


async def fetch_tweet(tweet_id: str) -> dict:
    """Fetch tweet via Twitter syndication API (no auth required)."""
    url = f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&token=x"
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(url)
            if response.status_code == 404:
                raise APIError(status_code=400, message="Tweet not found. It may be deleted or private.", code=ErrorCode.INVALID_INPUT)
            if response.status_code != 200:
                raise APIError(status_code=502, message=f"Failed to fetch tweet (Twitter returned {response.status_code})", code=ErrorCode.BAD_GATEWAY)
            data = response.json()
            if not data or "text" not in data:
                raise APIError(status_code=400, message="Tweet not accessible. It may be from a private or suspended account.", code=ErrorCode.INVALID_INPUT)
            return data
        except httpx.TimeoutException:
            raise APIError(status_code=504, message="Timeout while fetching tweet. Please try again.", code=ErrorCode.GATEWAY_TIMEOUT)
        except httpx.RequestError as e:
            raise APIError(status_code=502, message=f"Network error while fetching tweet: {str(e)}", code=ErrorCode.BAD_GATEWAY)


def verify_tweet_contains_code(tweet_text: str, code: str) -> bool:
    return code.lower() in tweet_text.lower()
