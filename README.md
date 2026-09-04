# Chameleon Bot

A minimal Telegram bot for playing the **Chameleon** party game with friends,
with Indian-flavoured categories (street food, Bollywood, cricket, desi life, and
more). Built with [pyTelegramBotAPI](https://github.com/eternnoir/pyTelegramBotAPI).

One player is secretly the Chameleon. Everyone else sees the same secret word from
a shown category (12-15 words are picked from the category each game). Players give
one clue each by typing `!their clue` (or `/hint their clue` if the bot can't read
plain messages), then vote for the Chameleon. See `/game_rules` in the bot, or the
"How to play" section below.

---

## 1. Security first (do this now)

The token that was used while building this was shared in a chat, so treat it as
**compromised**. Before you play with friends:

1. Open Telegram and message **@BotFather**.
2. Send `/revoke`, pick your bot, and then `/token` to get a fresh token.
3. Open the `.env` file in this folder and replace the value of `BOT_TOKEN` with
   the new token.

Never commit `.env` to GitHub - it is already listed in `.gitignore`.

---

## 2. Run it locally

You need Python 3.9 or newer.

```bash
# 1. (recommended) create a virtual environment
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 2. install dependencies
pip install -r requirements.txt

# 3. make sure .env has your token (see step 1 above)
#    if .env is missing, copy the example:  cp .env.example .env

# 4. run the bot
python bot.py
```

You should see `Chameleon bot is running.` Leave this terminal open - the bot
works only while this process is running.

---

## 3. Deploy free on Render

The bot automatically uses webhook mode when `RENDER_EXTERNAL_URL` is present.
This allows it to run as a Render free Web Service instead of a paid background
worker. Render free services can sleep, so Telegram updates may be delayed while
the service wakes up.

1. Create a **Web Service** on Render and connect this GitHub repository.
2. Use these settings:

   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn --bind 0.0.0.0:$PORT bot:app`

3. Add these environment variables in Render:

   - `BOT_TOKEN` - the token from BotFather.
   - `DATABASE_URL` - the Neon PostgreSQL connection string.
   - `SUPER_ADMINS` - comma-separated Telegram user IDs.
   - `MIN_PLAYERS` - usually `3`.
   - `WEBHOOK_SECRET` - a random value containing only letters, numbers, `_`, or `-`.

Render automatically supplies `RENDER_EXTERNAL_URL`. On startup, the bot registers
`https://your-service.onrender.com/telegram/webhook` with Telegram and exposes
`/healthz` for the Render health check.

---

## 4. Set up the bot in Telegram

1. In **@BotFather**, turn **Group Privacy OFF** so the bot can read the `!clue`
   hint messages players type during a round:
   BotFather -> your bot -> *Bot Settings* -> *Group Privacy* -> **Turn off**.
   **Important:** this setting only takes effect after you **remove and re-add the
   bot** to the group. Simply toggling it is not enough for existing groups.
2. Still in *Bot Settings*, make sure the bot is allowed to be added to groups.
3. Create a Telegram group with your friends and **add the bot** to it.
4. In the group, send `/start` to open a single-game lobby, or `/series` to play a
   best-of-3 or best-of-5 series.
5. To make yourself the "super admin" everywhere, send `/id` to the bot, copy your
   user ID, put it in `.env` as `SUPER_ADMINS=<your id>`, and restart the bot.

> **If `!clue` hints do nothing:** Group Privacy is still ON, so the bot never sees
> plain messages. Either remove and re-add the bot after turning privacy off (step
> 1), or just use the `/hint your clue` command instead - commands are always
> delivered, so `/hint` works no matter what. The bot checks this at startup and
> prints a warning if privacy is on.

---

## 5. How to play

1. Someone sends `/start` in the group (or `/series` for a multi-game series).
2. Everyone taps **Join / Leave** to join the lobby (default minimum 3 players).
3. The starter (or an admin) taps **Start Game**.
4. A category and a list of 12-15 words appear. Everyone taps **See my word**:
   - Normal players see the same secret word in a private popup.
   - The Chameleon is told they are the Chameleon.
5. The starter taps **Start Round**. Following the on-screen turn order, each player
   types their clue as `!their clue` in the group (for example: `!striped`), or as
   `/hint their clue` if the bot can't read plain messages. The bot records it and
   moves to the next player. The starter can **Skip current player** if someone is
   away.
6. When all hints are in, the starter taps **Start Voting**. Everyone taps the name
   of who they suspect. Voting ends when all players have voted, or the starter taps
   **Finish Voting**.
7. The most-voted person is unmasked:
   - **A normal player is voted out (or it's a tie):** the Chameleon blended in and
     wins big (**3 points**).
   - **The Chameleon is caught:** they get one guess at the secret word from the
     list. A correct guess is still a Chameleon win, but a smaller one (**1 point**).
     A wrong guess means **the players win** (**1 point each**).
8. Results and points are saved. See the group leaderboard with `/stats`.

> **Fair Chameleon selection:** the Chameleon is chosen randomly each game, but the
> person who speaks first is *less* likely to get it (going first with no clues to
> lean on is the hardest spot) - it can still happen, just not often. In a series the
> role also tries to move around instead of sticking to the same person two games in
> a row.

### Special word lists

Two commands start a game using a fixed, custom list instead of a random category:

- `/profsp` - a **Professors** list.
- `/batchsp` - a **Batchmates** list.

They play exactly like `/start` (join, start, hints, vote) but the words come only
from that list, and the lobby/round screens show the theme name. Edit the names in
`categories.py` under the `SPECIAL` dictionary. These lists never appear in normal
random games.

### Playing a series

Send `/series` and choose **Best of 3** or **Best of 5**. The same players join once,
then every game uses that locked roster. Points add up across all games, standings
are shown between games, and the bot announces the points winner at the end.

### Commands

- `/start` - start a single game (in a group)
- `/series` - start a best-of-3 or best-of-5 series
- `/profsp` - start a game with the **Professors** special list
- `/batchsp` - start a game with the **Batchmates** special list
- `/hint your clue` - give your hint by command (works even if privacy is on)
- `/game_rules` - how to play
- `/nextgame` - add/remove yourself from the next-game ping list
- `/stats` - group leaderboard (by points)
- `/settings` - change min players / admins-only-start (admins only)
- `/abort_game` - abort the current game or series (starter or admin)
- `/admins_reload` - refresh the cached list of group admins
- `/id` - show the chat id and your user id
- `/help` - list commands

During a round, give your clue by typing `!your clue` (or `/hint your clue`).

---

## 6. Editing categories and words

Open `categories.py`. It is a plain dictionary of `"Category name": [list of words]`.
Add, remove, or edit freely. Each game randomly picks one category and shows 12-15
words from it, so keep **at least 15 words** per category for the full effect (if a
category has fewer, the whole list is used). Restart the bot after editing.

The same file also has a `SPECIAL` dictionary at the bottom for the `/profsp` and
`/batchsp` lists. Keep **at least 12 entries** in each so a valid round can be drawn,
and note these are deliberately kept out of the random category pool.

---

## 7. Database notes

The bot uses a local SQLite file (`chameleon_stats.db`) when `DATABASE_URL` is
empty. For persistent hosted stats, set `DATABASE_URL` to a Neon PostgreSQL
connection string. The `psycopg2-binary` dependency is already enabled, and the
`stats` table is created automatically on startup.

For local development, leave `DATABASE_URL` empty and SQLite will be used instead.

---

## Files

- `bot.py` - the bot and all game logic / commands
- `logic.py` - pure voting/outcome logic (unit-tested)
- `categories.py` - categories and word lists
- `db.py` - stats storage (SQLite locally, Postgres if `DATABASE_URL` is set)
- `config.py` - loads settings from `.env`
- `test_logic.py` - quick self-tests (`python test_logic.py`)
- `requirements.txt`, `.env.example`, `.gitignore`
