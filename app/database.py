"""
Central database setup.

Switch from SQLite to PostgreSQL by changing DATABASE_URL to
"postgresql+psycopg2://user:pass@host/dbname" and removing connect_args.
"""
import json
import os
import shutil

from sqlalchemy import (
    Boolean, Column, Float, Integer, MetaData, Table, Text, create_engine, text
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
    Column("salt",              Text),   # per-user random salt; NULL = legacy global salt
    Column("role",              Text,    nullable=False, default="user"),
    Column("enabled",           Boolean, nullable=False, default=True),
    Column("created_at",        Text,    nullable=False),
    Column("claude_mode",        Text,    nullable=False, default="api_key"),
    Column("claude_api_key",     Text),
    Column("claude_oauth_token", Text),
    Column("binance_api_key",    Text),
    Column("binance_api_secret", Text),
    Column("owner",              Text),   # admin username who created this user
    Column("email",              Text),   # groups accounts belonging to the same person
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
    Column("current_capital",    Float),  # dynamic: grows/shrinks with each trade
    Column("position_qty",       Float),  # exact crypto qty bought by the bot (not full wallet)
    Column("compounding_mode",       Text),   # "fixed" | "compound" | "compound_wins"
    Column("analysis_weight",        Integer, default=70),
    Column("calibrated_thresholds",  Text),   # JSON dict {regime: threshold}
    Column("portfolio_free_usdc",    Float,   default=0.0),
    Column("portfolio_positions",    Text),   # JSON dict
    Column("updated_at",             Text),
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
    _migrate_add_columns()
    _migrate_json_to_sqlite()
    _migrate_knowledge_to_tiered()


def _migrate_add_columns() -> None:
    """Add columns that were introduced after initial schema creation."""
    with engine.connect() as conn:
        for col in ("binance_api_key", "binance_api_secret", "salt", "owner", "email"):
            try:
                conn.execute(text(f"ALTER TABLE users ADD COLUMN {col} TEXT"))
            except Exception:
                pass  # column already exists
        for live_col in ("current_capital REAL", "position_qty REAL", "compounding_mode TEXT",
                         "analysis_weight INTEGER", "calibrated_thresholds TEXT",
                         "buy_price REAL", "min_confidence INTEGER", "min_confidence_sell INTEGER",
                         "sl_atr_mult REAL", "tp_atr_mult REAL",
                         "portfolio_free_usdc REAL", "portfolio_positions TEXT"):
            try:
                conn.execute(text(f"ALTER TABLE live_states ADD COLUMN {live_col}"))
            except Exception:
                pass  # column already exists
        conn.commit()


def _migrate_json_to_sqlite() -> None:
    """One-time migration: import old JSON-based users + simulations into SQLite."""
    from sqlalchemy import insert, select

    data_dir = "/app/data"

    # ── Users ──────────────────────────────────────────────────────────────────
    users_json = f"{data_dir}/users.json"
    migrated_flag = f"{data_dir}/users.json.migrated"

    if os.path.exists(users_json) and not os.path.exists(migrated_flag):
        try:
            with open(users_json) as f:
                old_users: dict = json.load(f)
            with engine.connect() as conn:
                for uname, udata in old_users.items():
                    exists = conn.execute(
                        select(users.c.username).where(users.c.username == uname)
                    ).fetchone()
                    if not exists:
                        conn.execute(insert(users).values(
                            username=uname,
                            password_hash=udata.get("password_hash", ""),
                            role=udata.get("role", "user"),
                            enabled=udata.get("enabled", True),
                            created_at=udata.get("created_at", ""),
                            claude_mode=udata.get("claude_mode", "api_key"),
                            claude_api_key=udata.get("claude_api_key"),
                            claude_oauth_token=udata.get("claude_oauth_token"),
                        ))
                conn.commit()
            os.rename(users_json, migrated_flag)
        except Exception:
            pass

    # ── Simulations ────────────────────────────────────────────────────────────
    try:
        with engine.connect() as conn:
            known_users = [
                r._mapping["username"]
                for r in conn.execute(select(users.c.username)).fetchall()
            ]
        for uname in known_users:
            sim_json = f"{data_dir}/users/{uname}/simulations.json"
            sim_flag = f"{data_dir}/users/{uname}/simulations.json.migrated"
            sims_dir = f"{data_dir}/users/{uname}/sims"
            if not os.path.exists(sim_json) or os.path.exists(sim_flag):
                continue
            with open(sim_json) as f:
                old_sims: list = json.load(f)
            with engine.connect() as conn:
                for s in old_sims:
                    sim_id = s.get("id", "")
                    if not sim_id:
                        continue
                    exists = conn.execute(
                        select(simulations.c.sim_id).where(simulations.c.sim_id == sim_id)
                    ).fetchone()
                    if not exists:
                        conn.execute(insert(simulations).values(
                            sim_id=sim_id,
                            username=uname,
                            created_at=s.get("created_at", ""),
                            symbol=s.get("symbol", ""),
                            interval=s.get("interval", ""),
                            days=s.get("days", 30),
                            capital=s.get("capital", 1000.0),
                            fee_tier=s.get("fee_tier", "standard"),
                            total_return_pct=s.get("total_return_pct", 0),
                            win_rate=s.get("win_rate", 0),
                            num_trades=s.get("num_trades", 0),
                            max_drawdown=s.get("max_drawdown", 0),
                            total_fees_usdt=s.get("total_fees_usdt", 0),
                            fee_drag_pct=s.get("fee_drag_pct", 0),
                            strategy_name=s.get("strategy_name", ""),
                            strategy_analysis=s.get("strategy_analysis", ""),
                            strategy_patterns=json.dumps(s.get("strategy_patterns", [])),
                            profitable=s.get("profitable", False),
                            iterations=s.get("iterations", 1),
                        ))
                        detail_file = f"{sims_dir}/{sim_id}.json"
                        if os.path.exists(detail_file):
                            with open(detail_file) as df:
                                detail_data = df.read()
                        else:
                            detail_data = json.dumps(s)
                        conn.execute(insert(simulation_details).values(
                            sim_id=sim_id, username=uname, full_data=detail_data,
                        ))
                conn.commit()
            os.rename(sim_json, sim_flag)
    except Exception:
        pass


def _migrate_knowledge_to_tiered() -> None:
    """One-time migration: move old flat knowledge files into users/admin/ structure."""

    knowledge_dir = "/app/knowledge"
    old_patterns  = f"{knowledge_dir}/patterns.json"
    old_sim_log   = f"{knowledge_dir}/sim_log.json"
    old_global    = f"{knowledge_dir}/global_insights.json"

    admin_dir = f"{knowledge_dir}/users/admin"
    os.makedirs(admin_dir, exist_ok=True)

    new_patterns = f"{admin_dir}/patterns.json"
    new_sim_log  = f"{admin_dir}/sim_log.json"

    # Migrate patterns.json → users/admin/patterns.json
    if os.path.exists(old_patterns) and not os.path.exists(f"{old_patterns}.migrated"):
        try:
            with open(old_patterns) as f:
                data = json.load(f)
            # Only migrate if target doesn't already have real data
            target_exists = os.path.exists(new_patterns)
            if not target_exists:
                # Wrap old flat format into per-user format
                migrated = {
                    "version": 1,
                    "username": "admin",
                    "symbols": data.get("symbols", {}),
                    "symbol_performance": data.get("symbol_performance", {}),
                    "updated_at": data.get("updated_at", ""),
                }
                with open(new_patterns, "w") as f:
                    json.dump(migrated, f, indent=2)
            os.rename(old_patterns, f"{old_patterns}.migrated")
        except Exception:
            pass

    # Migrate sim_log.json → users/admin/sim_log.json
    if os.path.exists(old_sim_log) and not os.path.exists(f"{old_sim_log}.migrated"):
        try:
            if not os.path.exists(new_sim_log):
                shutil.copy2(old_sim_log, new_sim_log)
            os.rename(old_sim_log, f"{old_sim_log}.migrated")
        except Exception:
            pass

    # global_insights.json → core/patterns.json global_rules
    core_patterns = f"{knowledge_dir}/core/patterns.json"
    if os.path.exists(old_global) and not os.path.exists(f"{old_global}.migrated"):
        try:
            with open(old_global) as f:
                g = json.load(f)
            if os.path.exists(core_patterns):
                with open(core_patterns) as f:
                    core = json.load(f)
                # Only merge if core still has only seed rules (no promoted ones)
                if all(r.get("confidence") == "seed" for r in core.get("global_rules", [])):
                    core["global_rules"]    = g.get("rules", core["global_rules"])
                    core["interval_notes"]  = g.get("interval_notes", core.get("interval_notes", {}))
                    with open(core_patterns, "w") as f:
                        json.dump(core, f, indent=2)
            os.rename(old_global, f"{old_global}.migrated")
        except Exception:
            pass
