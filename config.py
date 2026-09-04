"""Configuration for the Chameleon bot. Reads settings from environment / .env file."""
import os

# Load variables from a local .env file if python-dotenv is installed.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# --- Required ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

# --- Optional ---
# Comma-separated Telegram user IDs who are "super admins" everywhere (e.g. you).
# Find your ID by sending /id to the bot.
SUPER_ADMINS = {
    int(x) for x in os.environ.get("SUPER_ADMINS", "").replace(" ", "").split(",") if x
}

# Leave empty to use a local SQLite file (default). Set a postgres:// URL to use
# a remote database later (no code changes needed).
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

# Path to the local SQLite stats file (used only when DATABASE_URL is empty).
STATS_DB = os.environ.get("STATS_DB", "chameleon_stats.db").strip()

# Minimum players needed to start a game (can be changed per-group via /settings).
DEFAULT_MIN_PLAYERS = int(os.environ.get("MIN_PLAYERS", "3"))


def require_token():
    """Fail early with a friendly message if the token is missing."""
    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN is not set.\n"
            "Copy .env.example to .env and put your bot token in it, then run again."
        )
