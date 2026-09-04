"""Stats storage for the Chameleon bot.

Uses a local SQLite file by default. If DATABASE_URL is set (a postgres:// URL),
it uses that remote Postgres database instead - no other code changes needed.

Stats are tracked per (chat, user), so each group has its own leaderboard.
"""
import threading
from contextlib import contextmanager

import config

_lock = threading.Lock()
_USE_PG = config.DATABASE_URL.startswith(("postgres://", "postgresql://"))

if _USE_PG:
    import psycopg2  # requires: pip install psycopg2-binary
    _PH = "%s"  # Postgres parameter placeholder
else:
    import sqlite3
    _PH = "?"   # SQLite parameter placeholder


@contextmanager
def _cursor():
    """Open a connection, yield a cursor, commit, and always close."""
    if _USE_PG:
        conn = psycopg2.connect(config.DATABASE_URL)
    else:
        conn = sqlite3.connect(config.STATS_DB)
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Create the stats table if it does not exist, and add newer columns."""
    with _lock, _cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS stats (
                chat_id    BIGINT   NOT NULL,
                user_id    BIGINT   NOT NULL,
                username   TEXT     NOT NULL,
                games      INTEGER  NOT NULL DEFAULT 0,
                wins       INTEGER  NOT NULL DEFAULT 0,
                cham_count INTEGER  NOT NULL DEFAULT 0,
                cham_wins  INTEGER  NOT NULL DEFAULT 0,
                points     INTEGER  NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            )
            """
        )
        # Migration for databases created before the points column existed.
        try:
            cur.execute("ALTER TABLE stats ADD COLUMN points INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass


def _upsert(cur, chat_id, row):
    q = f"""
        INSERT INTO stats
            (chat_id, user_id, username, games, wins, cham_count, cham_wins, points)
        VALUES ({_PH}, {_PH}, {_PH}, 1, {_PH}, {_PH}, {_PH}, {_PH})
        ON CONFLICT (chat_id, user_id) DO UPDATE SET
            username   = EXCLUDED.username,
            games      = stats.games + 1,
            wins       = stats.wins + {_PH},
            cham_count = stats.cham_count + {_PH},
            cham_wins  = stats.cham_wins + {_PH},
            points     = stats.points + {_PH}
    """
    won = int(bool(row["won"]))
    cham = int(bool(row["was_cham"]))
    cwin = int(bool(row["cham_win"]))
    pts = int(row["points"])
    cur.execute(
        q,
        (chat_id, row["uid"], row["name"], won, cham, cwin, pts,
         won, cham, cwin, pts),
    )


def record_round(chat_id, rows):
    """Record one finished round.

    rows: list of dicts, one per participant, each with keys:
        uid, name, points (int), won (bool), was_cham (bool), cham_win (bool)
    """
    with _lock, _cursor() as cur:
        for row in rows:
            _upsert(cur, chat_id, row)


def get_user_stats(chat_id, user_id):
    """Return a dict of one user's stats in a chat, or None if they have none."""
    with _lock, _cursor() as cur:
        cur.execute(
            f"SELECT username, games, wins, cham_count, cham_wins, points "
            f"FROM stats WHERE chat_id={_PH} AND user_id={_PH}",
            (chat_id, user_id),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "username": row[0], "games": row[1], "wins": row[2],
        "cham_count": row[3], "cham_wins": row[4], "points": row[5],
    }


def get_leaderboard(chat_id, limit=10):
    """Return top players in a chat ordered by points, then wins."""
    with _lock, _cursor() as cur:
        cur.execute(
            f"SELECT username, games, wins, cham_count, cham_wins, points "
            f"FROM stats WHERE chat_id={_PH} "
            f"ORDER BY points DESC, wins DESC, games DESC LIMIT {_PH}",
            (chat_id, limit),
        )
        rows = cur.fetchall()
    return [
        {"username": r[0], "games": r[1], "wins": r[2],
         "cham_count": r[3], "cham_wins": r[4], "points": r[5]}
        for r in rows
    ]
