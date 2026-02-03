"""
MoltMarkets Storage package.

Backward-compatible: ``from storage import Storage`` and
``from storage import hash_api_key`` continue to work exactly
as before the split.
"""

from storage._hash import hash_api_key
from storage.base import BaseStorage
from storage.converters import ConverterMixin
from storage.users import UserStorageMixin
from storage.markets import MarketStorageMixin
from storage.bets import BetStorageMixin
from storage.positions import PositionStorageMixin
from storage.social import SocialStorageMixin
from storage.transactions import TransactionStorageMixin
from storage.committee import CommitteeStorageMixin
from storage.types import (
    BetDict,
    BetWithUsernameDict,
    ChatMessageDict,
    CommentDict,
    CommentWithUsernameDict,
    CommitteeVoteDict,
    LeaderboardEntryDict,
    MarketDict,
    MarketWithCreatorDict,
    PoolDict,
    PositionDict,
    ReputationDataDict,
    ResolutionVoteDict,
    UserDict,
)


class Storage(
    TransactionStorageMixin,
    CommitteeStorageMixin,
    SocialStorageMixin,
    PositionStorageMixin,
    BetStorageMixin,
    MarketStorageMixin,
    UserStorageMixin,
    ConverterMixin,
    BaseStorage,
):
    """PostgreSQL storage backend — composed from domain mixins.

    Inherits all methods from the individual mixin modules.
    The public API is identical to the original monolithic Storage class.
    """
    pass


__all__ = [
    "Storage",
    "hash_api_key",
    # TypedDicts for storage return types (issue #72)
    "BetDict",
    "BetWithUsernameDict",
    "ChatMessageDict",
    "CommentDict",
    "CommentWithUsernameDict",
    "CommitteeVoteDict",
    "LeaderboardEntryDict",
    "MarketDict",
    "MarketWithCreatorDict",
    "PoolDict",
    "PositionDict",
    "ReputationDataDict",
    "ResolutionVoteDict",
    "UserDict",
]
