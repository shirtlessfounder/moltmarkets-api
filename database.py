"""
Database setup for MoltMarkets.

Uses SQLAlchemy async with PostgreSQL.
Falls back to SQLite for local development.
"""

import os
from datetime import datetime, timezone
from typing import AsyncGenerator

from sqlalchemy import String, Float, DateTime, Enum as SQLEnum, ForeignKey, Text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from dotenv import load_dotenv

load_dotenv()

# Database URL from environment or default to SQLite
DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL:
    # Railway provides postgres:// but asyncpg needs postgresql+asyncpg://
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
    elif DATABASE_URL.startswith("postgresql://"):
        DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
else:
    DATABASE_URL = "sqlite+aiosqlite:///./moltmarkets.db"


# Create async engine
engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


# =============================================================================
# Database Models
# =============================================================================

class DBUser(Base):
    __tablename__ = "users"
    
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    display_name: Mapped[str] = mapped_column(String(100))
    balance: Mapped[float] = mapped_column(Float, default=1000.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    markets_created: Mapped[int] = mapped_column(default=0)
    total_bets: Mapped[int] = mapped_column(default=0)
    profit_all_time: Mapped[float] = mapped_column(Float, default=0.0)
    
    # Relationships
    bets: Mapped[list["DBBet"]] = relationship(back_populates="user")
    positions: Mapped[list["DBPosition"]] = relationship(back_populates="user")


class DBMarket(Base):
    __tablename__ = "markets"
    
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="OPEN")  # OPEN, CLOSED, RESOLVED
    closes_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(10), nullable=True)  # YES, NO
    total_volume: Mapped[float] = mapped_column(Float, default=0.0)
    creator_id: Mapped[str] = mapped_column(String(100))
    
    # CPMM state
    pool_yes: Mapped[float] = mapped_column(Float, default=100.0)
    pool_no: Mapped[float] = mapped_column(Float, default=100.0)
    p: Mapped[float] = mapped_column(Float, default=0.5)
    
    # Relationships
    bets: Mapped[list["DBBet"]] = relationship(back_populates="market")
    positions: Mapped[list["DBPosition"]] = relationship(back_populates="market")


class DBBet(Base):
    __tablename__ = "bets"
    
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    market_id: Mapped[str] = mapped_column(ForeignKey("markets.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    outcome: Mapped[str] = mapped_column(String(10))  # YES, NO
    amount: Mapped[float] = mapped_column(Float)
    shares: Mapped[float] = mapped_column(Float)
    avg_price: Mapped[float] = mapped_column(Float)
    probability_before: Mapped[float] = mapped_column(Float)
    probability_after: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    
    # Relationships
    market: Mapped["DBMarket"] = relationship(back_populates="bets")
    user: Mapped["DBUser"] = relationship(back_populates="bets")


class DBPosition(Base):
    __tablename__ = "positions"
    
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    market_id: Mapped[str] = mapped_column(ForeignKey("markets.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    yes_shares: Mapped[float] = mapped_column(Float, default=0.0)
    no_shares: Mapped[float] = mapped_column(Float, default=0.0)
    total_invested: Mapped[float] = mapped_column(Float, default=0.0)
    
    # Relationships
    market: Mapped["DBMarket"] = relationship(back_populates="positions")
    user: Mapped["DBUser"] = relationship(back_populates="positions")


# =============================================================================
# Database Lifecycle
# =============================================================================

async def init_db():
    """Create all tables."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for getting database session."""
    async with async_session() as session:
        yield session
