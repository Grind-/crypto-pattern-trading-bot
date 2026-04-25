"""
Central database setup.

Switch from SQLite to PostgreSQL by changing DATABASE_URL to
"postgresql+psycopg2://user:pass@host/dbname" and removing connect_args.
"""
import os

from sqlalchemy import (
    Boolean, Column, Float, Integer, MetaData, Table, Text, create_engine
)
from sqlalchemy.pool import StaticPool

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:////app/data/crypto_pattern_ai.db")

_is_sqlite = DATABASE_URL.startswith("sqlite")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    poolclass=StaticPool if _is_sqlite else None,
)

metadata = MetaData()

users = Table(
    "users", metadata,
    Column("username",          Text,    primary_key=True),
    Column("password_hash",     Text,    nullable=False),
    Column("role",              Text,    nullable=False, default="user"),
    Column("enabled",           Boolean, nullable=False, default=True),
    Column("created_at",        Text,    nullable=False),
    Column("claude_mode",       Text,    nullable=False, default="api_key"),
    Column("claude_api_key",    Text),
    Column("claude_oauth_token", Text),
)

live_states = Table(
    "live_states", metadata,
    Column("username",           Text,    primary_key=True),
    Column("was_running",        Boolean, default=False),
    Column("api_key",            Text),
    Column("api_secret",         Text),
    Column("symbol",             Text),
    Column("interval",           Text),
    Column("trade_amount",       Float),
    Column("position",           Text),
    Column("buy_price",          Float),
    Column("strategy_name",      Text),
    Column("strategy_analysis",  Text),
    Column("strategy_patterns",  Text),   # JSON list
    Column("trade_history",      Text),   # JSON list
    Column("updated_at",         Text),
)

simulations = Table(
    "simulations", metadata,
    Column("sim_id",            Text,    primary_key=True),
    Column("username",          Text,    nullable=False, index=True),
    Column("created_at",        Text),
    Column("symbol",            Text),
    Column("interval",          Text),
    Column("days",              Integer),
    Column("capital",           Float),
    Column("fee_tier",          Text),
    Column("total_return_pct",  Float),
    Column("win_rate",          Float),
    Column("num_trades",        Integer),
    Column("max_drawdown",      Float),
    Column("total_fees_usdt",   Float),
    Column("fee_drag_pct",      Float),
    Column("strategy_name",     Text),
    Column("strategy_analysis", Text),
    Column("strategy_patterns", Text),   # JSON list
    Column("profitable",        Boolean),
    Column("iterations",        Integer),
)

simulation_details = Table(
    "simulation_details", metadata,
    Column("sim_id",    Text, primary_key=True),
    Column("username",  Text, nullable=False),
    Column("full_data", Text),  # complete JSON blob
)


def init_db() -> None:
    os.makedirs("/app/data", exist_ok=True)
    metadata.create_all(engine)
