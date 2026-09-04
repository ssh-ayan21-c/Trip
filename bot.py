"""Chameleon - a minimal Telegram bot for playing the Chameleon party game.

Run:  python bot.py   (after filling in .env)

NOTE: the '!hint' feature needs the bot's group privacy turned OFF in BotFather
(/setprivacy -> Disable), otherwise the bot cannot see hint messages in groups.
"""
import html
import random
import threading
import os

from flask import Flask, abort, jsonify, request
import telebot
from telebot import types

import config
import db
from categories import CATEGORIES, SPECIAL
from logic import tally_votes, decide_accused

config.require_token()

bot = telebot.TeleBot(config.BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

# One lock guards all shared state below (polling can use multiple threads).
LOCK = threading.RLock()

GAMES = {}          # chat_id -> Game
SERIES = {}         # chat_id -> Series
ADMIN_CACHE = {}    # chat_id -> set(user_id)
NEXTGAME = {}       # chat_id -> set(user_id)
SETTINGS = {}       # chat_id -> {"min_players": int, "admins_only_start": bool}

# Game states
LOBBY, REVEAL, DESCRIBE, VOTING, GUESS = "LOBBY", "REVEAL", "DESCRIBE", "VOTING", "GUESS"

# --- Tunable settings (edit freely) ---
GAME_WORD_MIN = 12          # smallest word list shown for a game
GAME_WORD_MAX = 15          # largest word list shown for a game
CHAM_BLEND_POINTS = 3       # chameleon never gets voted out (best result)
CHAM_GUESS_POINTS = 1       # chameleon caught but guesses the word (smaller win)
PLAYER_WIN_POINTS = 1       # each normal player when the chameleon is beaten

# The first person in the turn order gives the very first hint with nothing to go
# on, which is brutal for the Chameleon. So the first speaker is LESS likely to be
# the Chameleon - but it can still happen (this is the relative weight, 1.0 = normal).
FIRST_SPEAKER_CHAM_WEIGHT = 0.35
# In a series we shrink the previous game's Chameleon weight so the role moves
# around instead of sticking to one person (still possible, just unlikely).
SERIES_REPEAT_CHAM_WEIGHT = 0.15

# Set at startup from getMe(). If Group Privacy is ON, the bot cannot see plain
# "!hint" messages - only commands like "/hint". We detect this and warn.
CAN_READ_MESSAGES = True


class Game:
    def __init__(self, chat_id, starter_id, series=None):
        self.chat_id = chat_id
        self.starter_id = starter_id
        self.series = series       # Series or None
        self.state = LOBBY
        self.players = {}          # user_id -> display name
        self.order = []            # turn / display order of user_ids
        self.category = None
        self.words = []
        self.secret = None
        self.chameleon_id = None
        self.hints = {}            # user_id -> hint text
        self.turn_index = 0
        self.votes = {}            # voter_id -> target_id
        self.msg_id = None         # lobby / reveal message id
        self.turn_msg_id = None    # hint-tracking message id
        self.vote_msg_id = None    # voting message id
        self.forced = None         # (name, [words]) for /profsp, /batchsp; else None


class Series:
    def __init__(self, chat_id, starter_id, total):
        self.chat_id = chat_id
        self.starter_id = starter_id
        self.total = total         # number of games in the series
        self.index = 0             # games started so far
        self.players = {}          # user_id -> display name (locked roster)
        self.order = []
        self.scores = {}           # user_id -> points
        self.state = LOBBY         # LOBBY -> PLAYING / BETWEEN -> done
        self.msg_id = None
        self.last_cham = None      # previous game's chameleon, to vary the role


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def disp_name(user):
    name = user.first_name or ""
    if getattr(user, "last_name", None):
        name += " " + user.last_name
    return name.strip() or "Player"


def esc(s):
    return html.escape(str(s))


def mention(uid, name):
    return f'<a href="tg://user?id={uid}">{esc(name)}</a>'


def is_group(message):
    return message.chat.type in ("group", "supergroup")


def get_settings(chat_id):
    return SETTINGS.setdefault(
        chat_id,
        {"min_players": config.DEFAULT_MIN_PLAYERS, "admins_only_start": False},
    )


def refresh_admins(chat_id):
    try:
        members = bot.get_chat_administrators(chat_id)
        ADMIN_CACHE[chat_id] = {m.user.id for m in members}
    except Exception:
        ADMIN_CACHE.setdefault(chat_id, set())
    return ADMIN_CACHE[chat_id]


def is_admin(chat_id, uid):
    if uid in config.SUPER_ADMINS:
        return True
    if chat_id not in ADMIN_CACHE:
        refresh_admins(chat_id)
    return uid in ADMIN_CACHE.get(chat_id, set())


def can_manage(chat_id, uid, starter_id):
    return uid == starter_id or is_admin(chat_id, uid)


def safe_edit(text, chat_id, message_id, markup=None):
    try:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=markup)
    except Exception:
        pass


def ack(call, text=None, alert=False):
    try:
        bot.answer_callback_query(call.id, text=text, show_alert=alert)
    except Exception:
        pass


def abort_all(chat_id):
    GAMES.pop(chat_id, None)
    SERIES.pop(chat_id, None)


def busy(chat_id):
    """True if a game or series is currently occupying this chat."""
    return chat_id in GAMES or chat_id in SERIES


# --------------------------------------------------------------------------- #
# Role / word assignment
# --------------------------------------------------------------------------- #
def _choose_chameleon(order, avoid=None):
    """Pick the Chameleon from the (already shuffled) turn order.

    - order[0] speaks first, so it gets a reduced weight (hard to blend in with
      no earlier hints to lean on) - but it can still be chosen.
    - In a series, `avoid` is the previous game's Chameleon; we shrink its weight
      so the role rotates instead of sticking to one person.
    """
    n = len(order)
    weights = [FIRST_SPEAKER_CHAM_WEIGHT] + [1.0] * (n - 1)
    if avoid is not None and n > 1 and avoid in order:
        weights = [w * SERIES_REPEAT_CHAM_WEIGHT if order[i] == avoid else w
                   for i, w in enumerate(weights)]
    if sum(weights) <= 0:            # safety net, should never happen
        weights = [1.0] * n
    return random.choices(order, weights=weights, k=1)[0]


def assign_roles(game, forced=None, avoid=None):
    """Set up category, words, secret, turn order, and Chameleon for a game.

    `forced` is an optional (name, [words]) pair for special games (/profsp,
    /batchsp). `avoid` is a user_id to make less likely to be the Chameleon
    (used in a series to avoid repeating the previous Chameleon).
    """
    if forced is not None:
        cat, all_words = forced[0], list(forced[1])
    else:
        cat = random.choice(list(CATEGORIES.keys()))
        all_words = list(CATEGORIES[cat])
    k = min(len(all_words), random.randint(GAME_WORD_MIN, GAME_WORD_MAX))
    game.category = cat
    game.words = random.sample(all_words, k)
    game.secret = random.choice(game.words)
    # Fresh shuffle every game (also re-shuffled for each game in a series).
    order = list(game.players.keys())
    random.shuffle(order)
    game.order = order
    game.chameleon_id = _choose_chameleon(order, avoid=avoid)
    game.hints = {}
    game.turn_index = 0
    game.votes = {}
    game.state = REVEAL


# --------------------------------------------------------------------------- #
# Text / keyboard builders
# --------------------------------------------------------------------------- #
def lobby_text(game):
    s = get_settings(game.chat_id)
    title = f"new {game.forced[0]} game" if game.forced else "new game"
    lines = [f"<b>Chameleon</b> - {title}", ""]
    if game.players:
        lines.append("Players joined:")
        for i, uid in enumerate(game.order, 1):
            lines.append(f"{i}. {mention(uid, game.players[uid])}")
    else:
        lines.append("No players yet.")
    lines.append("")
    lines.append(f"Need at least {s['min_players']} players. Tap Join to play.")
    return "\n".join(lines)


def lobby_markup():
    m = types.InlineKeyboardMarkup()
    m.add(types.InlineKeyboardButton("Join / Leave", callback_data="join"))
    m.add(
        types.InlineKeyboardButton("Start Game", callback_data="start_game"),
        types.InlineKeyboardButton("Abort", callback_data="abort"),
    )
    return m


def reveal_text(game):
    header = ""
    if game.series:
        header = f"<b>Series - game {game.series.index}/{game.series.total}</b>\n\n"
    words = "\n".join(f"{i}. {esc(w)}" for i, w in enumerate(game.words, 1))
    return (
        f"{header}Game started.\n\n"
        f"Category: <b>{esc(game.category)}</b>\n"
        f"{words}\n\n"
        "Tap the button to see your secret word (only you can see it). "
        "One player is the Chameleon and will be told so instead.\n\n"
        "When everyone has seen their word, the starter taps Start Round."
    )


def reveal_markup():
    m = types.InlineKeyboardMarkup()
    m.add(types.InlineKeyboardButton("See my word", callback_data="see"))
    m.add(types.InlineKeyboardButton("Start Round", callback_data="start_round"))
    return m


def hints_complete(game):
    return game.turn_index >= len(game.order)


def turn_text(game):
    lines = ["Hints (turn order):"]
    for i, uid in enumerate(game.order):
        name = esc(game.players[uid])
        if uid in game.hints:
            lines.append(f"{i + 1}. {name}: {esc(game.hints[uid])}")
        elif i == game.turn_index:
            lines.append(f"{i + 1}. {name}: (your turn)")
        else:
            lines.append(f"{i + 1}. {name}: -")
    lines.append("")
    if hints_complete(game):
        lines.append("All hints in. Discuss, then the starter taps Start Voting.")
    else:
        cur = esc(game.players[game.order[game.turn_index]])
        lines.append(f"It's {cur}'s turn. Send your hint as:  !your hint")
        lines.append("(or  /hint your hint  if the bot can't read plain messages)")
        if not CAN_READ_MESSAGES:
            lines.append("")
            lines.append("Note: this bot's Group Privacy is ON, so plain !hints are "
                         "not seen - use  /hint your hint. To enable !hints, turn "
                         "Group Privacy OFF in BotFather and re-add the bot.")
    return "\n".join(lines)


def _advance_turn_msg(game):
    """Refresh the hint-tracker message; if editing fails, post a fresh one."""
    txt, markup = turn_text(game), turn_markup(game)
    try:
        bot.edit_message_text(txt, game.chat_id, game.turn_msg_id, reply_markup=markup)
    except Exception:
        try:
            sent = bot.send_message(game.chat_id, txt, reply_markup=markup)
            game.turn_msg_id = sent.message_id
        except Exception:
            pass


def turn_markup(game):
    m = types.InlineKeyboardMarkup()
    if hints_complete(game):
        m.add(types.InlineKeyboardButton("Start Voting", callback_data="start_vote"))
    else:
        m.add(types.InlineKeyboardButton("Skip current player",
                                         callback_data="skip_turn"))
    return m


def voting_text(game):
    return (
        "Time to vote. Who is the Chameleon?\n"
        "Tap a name (you can change your vote until voting ends).\n\n"
        f"Votes: {len(game.votes)}/{len(game.players)}"
    )


def voting_markup(game):
    m = types.InlineKeyboardMarkup()
    buttons = [
        types.InlineKeyboardButton(game.players[uid], callback_data=f"vote:{uid}")
        for uid in game.order
    ]
    for i in range(0, len(buttons), 2):
        m.row(*buttons[i:i + 2])
    m.add(types.InlineKeyboardButton("Finish Voting", callback_data="finish_vote"))
    return m


def guess_markup(game):
    m = types.InlineKeyboardMarkup()
    buttons = [
        types.InlineKeyboardButton(w, callback_data=f"guess:{i}")
        for i, w in enumerate(game.words)
    ]
    for i in range(0, len(buttons), 2):
        m.row(*buttons[i:i + 2])
    return m


# --------------------------------------------------------------------------- #
# Static text
# --------------------------------------------------------------------------- #
HELP_TEXT = (
    "<b>Chameleon</b> - commands\n\n"
    "/start - start a single game (in a group)\n"
    "/series - start a best-of-3 or best-of-5 series\n"
    "/profsp - start a game with the Professors special list\n"
    "/batchsp - start a game with the Batchmates special list\n"
    "/game_rules - how to play\n"
    "/nextgame - add or remove yourself from the next-game ping list\n"
    "/stats - leaderboard for this group\n"
    "/settings - change group settings (admins)\n"
    "/abort_game - abort the current game or series (starter or admin)\n"
    "/admins_reload - refresh the cached list of group admins\n"
    "/id - show the chat and your user id\n"
    "/help - show this message\n\n"
    "During the hint phase, give your clue by typing  !your hint  "
    "(or  /hint your hint  if the bot can't read plain messages)."
)

RULES_TEXT = (
    "<b>How to play Chameleon</b>\n\n"
    "One player is secretly the Chameleon; everyone else is a normal player.\n\n"
    "A category and a short word list are shown. Tap the button to see your "
    "secret word. Every normal player sees the SAME word. The Chameleon is only "
    "told that they are the Chameleon.\n\n"
    "In turn order, each person gives one clue by typing  !their clue  in the "
    "group (or  /hint their clue). Only the current player's clue counts, then "
    "the turn passes on.\n"
    "- Players: prove you know the word without making it obvious.\n"
    "- Chameleon: you don't know the word, so bluff and blend in.\n\n"
    "Then everyone votes. The most-voted person is unmasked:\n"
    f"- Chameleon is NOT caught (a player is voted out, or a tie): the Chameleon "
    f"blends in and wins big ({CHAM_BLEND_POINTS} points).\n"
    f"- Chameleon IS caught: they get one guess at the secret word. Correct guess "
    f"= smaller Chameleon win ({CHAM_GUESS_POINTS} point). Wrong guess = the "
    f"players win ({PLAYER_WIN_POINTS} point each).\n\n"
    "Use /series to play a multi-game series and crown a points winner."
)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
@bot.message_handler(commands=["help"])
def cmd_help(message):
    bot.reply_to(message, HELP_TEXT)


@bot.message_handler(commands=["game_rules"])
def cmd_rules(message):
    bot.reply_to(message, RULES_TEXT)


@bot.message_handler(commands=["id"])
def cmd_id(message):
    bot.reply_to(
        message,
        f"Chat ID: <code>{message.chat.id}</code>\n"
        f"Your user ID: <code>{message.from_user.id}</code>",
    )


@bot.message_handler(commands=["admins_reload"])
def cmd_admins_reload(message):
    if not is_group(message):
        return bot.reply_to(message, "Use this inside a group.")
    with LOCK:
        admins = refresh_admins(message.chat.id)
    bot.reply_to(message, f"Admins reloaded ({len(admins)} found).")


@bot.message_handler(commands=["nextgame"])
def cmd_nextgame(message):
    if not is_group(message):
        return bot.reply_to(message, "Use this inside a group.")
    uid = message.from_user.id
    with LOCK:
        s = NEXTGAME.setdefault(message.chat.id, set())
        if uid in s:
            s.discard(uid)
            txt = "Removed you from the next-game list."
        else:
            s.add(uid)
            txt = "Added you to the next-game list. You'll be pinged for the next game."
    bot.reply_to(message, txt)


@bot.message_handler(commands=["start"])
def cmd_start(message):
    if not is_group(message):
        return bot.reply_to(
            message,
            "Add me to a group and send /start there to play Chameleon.\n"
            "Send /game_rules to learn how, or /help for commands.",
        )
    _open_game(message, forced=None)


@bot.message_handler(commands=["profsp"])
def cmd_profsp(message):
    if not is_group(message):
        return bot.reply_to(message, "Use /profsp inside a group.")
    _open_game(message, forced=SPECIAL["profsp"])


@bot.message_handler(commands=["batchsp"])
def cmd_batchsp(message):
    if not is_group(message):
        return bot.reply_to(message, "Use /batchsp inside a group.")
    _open_game(message, forced=SPECIAL["batchsp"])


def _open_game(message, forced=None):
    """Open a single-game lobby, optionally forced to a special word list."""
    chat_id = message.chat.id
    with LOCK:
        if busy(chat_id):
            return bot.reply_to(
                message, "A game is already running here. Use /abort_game to stop it."
            )
        game = Game(chat_id, message.from_user.id)
        game.forced = forced
        GAMES[chat_id] = game
        sent = bot.send_message(chat_id, lobby_text(game), reply_markup=lobby_markup())
        game.msg_id = sent.message_id
        waiting = NEXTGAME.get(chat_id)
        pings = ""
        if waiting:
            pings = " ".join(mention(u, "player") for u in waiting)
            NEXTGAME[chat_id] = set()
    if pings:
        bot.send_message(chat_id, f"New game starting. {pings}")


@bot.message_handler(commands=["series"])
def cmd_series(message):
    if not is_group(message):
        return bot.reply_to(message, "Use /series inside a group.")
    with LOCK:
        if busy(message.chat.id):
            return bot.reply_to(
                message, "A game is already running here. Use /abort_game first."
            )
    m = types.InlineKeyboardMarkup()
    m.add(
        types.InlineKeyboardButton("Best of 3", callback_data="series_len:3"),
        types.InlineKeyboardButton("Best of 5", callback_data="series_len:5"),
    )
    m.add(types.InlineKeyboardButton("Cancel", callback_data="series_cancel"))
    bot.send_message(
        message.chat.id,
        "Start a series. How many games?\n"
        "The same players play every game and points add up.",
        reply_markup=m,
    )


@bot.message_handler(commands=["abort_game"])
def cmd_abort(message):
    if not is_group(message):
        return
    chat_id = message.chat.id
    with LOCK:
        if not busy(chat_id):
            return bot.reply_to(message, "No game is running here.")
        starter = (GAMES.get(chat_id) or SERIES.get(chat_id)).starter_id
        if not can_manage(chat_id, message.from_user.id, starter):
            return bot.reply_to(message, "Only the starter or an admin can abort.")
        abort_all(chat_id)
    bot.reply_to(message, "Game aborted.")


@bot.message_handler(commands=["settings"])
def cmd_settings(message):
    if not is_group(message):
        return bot.reply_to(message, "Use this inside a group.")
    if not is_admin(message.chat.id, message.from_user.id):
        return bot.reply_to(message, "Only group admins can change settings.")
    bot.send_message(
        message.chat.id, settings_text(message.chat.id),
        reply_markup=settings_markup(message.chat.id),
    )


@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    if not is_group(message):
        return bot.reply_to(
            message, "Send /stats inside a game group to see that group's leaderboard."
        )
    chat_id = message.chat.id
    board = db.get_leaderboard(chat_id, limit=10)
    if not board:
        return bot.reply_to(message, "No games have been played here yet.")
    lines = ["<b>Leaderboard</b>", ""]
    for i, r in enumerate(board, 1):
        lines.append(
            f"{i}. {esc(r['username'])} - {r['points']} pts "
            f"({r['wins']} wins / {r['games']} games, "
            f"chameleon {r['cham_wins']}/{r['cham_count']})"
        )
    me = db.get_user_stats(chat_id, message.from_user.id)
    if me:
        lines.append("")
        lines.append(
            f"You: {me['points']} pts ({me['wins']} wins / {me['games']} games)"
        )
    bot.reply_to(message, "\n".join(lines))


def settings_text(chat_id):
    s = get_settings(chat_id)
    return (
        "<b>Group settings</b>\n\n"
        f"Minimum players: {s['min_players']}\n"
        f"Only admins can start: {'ON' if s['admins_only_start'] else 'OFF'}"
    )


def settings_markup(chat_id):
    m = types.InlineKeyboardMarkup()
    m.row(
        types.InlineKeyboardButton("- min", callback_data="set:min:dec"),
        types.InlineKeyboardButton("+ min", callback_data="set:min:inc"),
    )
    m.add(types.InlineKeyboardButton(
        "Toggle admins-only start", callback_data="set:toggle"))
    return m


# --------------------------------------------------------------------------- #
# Hint submission - via "!clue" (needs privacy OFF) or "/hint clue" (always works)
# --------------------------------------------------------------------------- #
def _submit_hint(message, hint):
    """Record the current player's hint and advance the turn.

    Shared by the '!clue' text handler and the '/hint clue' command so both
    behave identically. Enforces turn order and ignores non-players.
    """
    if not is_group(message):
        return
    with LOCK:
        game = GAMES.get(message.chat.id)
        if not game or game.state != DESCRIBE or hints_complete(game):
            return
        uid = message.from_user.id
        if uid not in game.players:
            return
        current = game.order[game.turn_index]
        if uid != current:
            bot.reply_to(
                message,
                f"Wait for your turn - it's {esc(game.players[current])}'s turn.",
            )
            return
        hint = (hint or "").strip()
        if not hint:
            bot.reply_to(message, "Add your hint after it, e.g.  !stripes  or  /hint stripes")
            return
        game.hints[uid] = hint
        game.turn_index += 1
        _advance_turn_msg(game)


@bot.message_handler(func=lambda m: bool(getattr(m, "text", None))
                     and m.text.startswith("!"))
def on_hint(message):
    _submit_hint(message, message.text[1:])


@bot.message_handler(commands=["hint", "h"])
def cmd_hint(message):
    parts = (message.text or "").split(maxsplit=1)
    _submit_hint(message, parts[1] if len(parts) > 1 else "")


# --------------------------------------------------------------------------- #
# Callback dispatch
# --------------------------------------------------------------------------- #
@bot.callback_query_handler(func=lambda c: True)
def on_callback(call):
    data = call.data or ""
    with LOCK:
        try:
            if data.startswith("set:"):
                return handle_settings_cb(call, data)
            if data.startswith("series_len:"):
                return handle_series_len(call, int(data.split(":")[1]))
            if data == "series_cancel":
                safe_edit("Series cancelled.", call.message.chat.id,
                          call.message.message_id)
                return ack(call)
            if data == "sjoin":
                return handle_sjoin(call)
            if data == "sstart":
                return handle_sstart(call)
            if data == "series_next":
                return handle_series_next(call)

            chat_id = call.message.chat.id
            game = GAMES.get(chat_id)
            if data == "join":
                return handle_join(call, game)
            if data == "start_game":
                return handle_start_game(call, game)
            if data == "see":
                return handle_see(call, game)
            if data == "start_round":
                return handle_start_round(call, game)
            if data == "skip_turn":
                return handle_skip(call, game)
            if data == "start_vote":
                return handle_start_vote(call, game)
            if data.startswith("vote:"):
                return handle_vote(call, game, int(data.split(":")[1]))
            if data == "finish_vote":
                return handle_finish_vote(call, game)
            if data.startswith("guess:"):
                return handle_guess(call, game, int(data.split(":")[1]))
            if data == "abort":
                return handle_abort_button(call, game)
            ack(call)
        except Exception:
            ack(call, "Something went wrong.", alert=True)


def handle_settings_cb(call, data):
    chat_id = call.message.chat.id
    if not is_admin(chat_id, call.from_user.id):
        return ack(call, "Only admins can change settings.", alert=True)
    s = get_settings(chat_id)
    if data == "set:min:dec":
        s["min_players"] = max(3, s["min_players"] - 1)
    elif data == "set:min:inc":
        s["min_players"] = min(15, s["min_players"] + 1)
    elif data == "set:toggle":
        s["admins_only_start"] = not s["admins_only_start"]
    safe_edit(settings_text(chat_id), chat_id, call.message.message_id,
              settings_markup(chat_id))
    ack(call, "Updated.")


# --------- single-game lobby ---------
def handle_join(call, game):
    if not game or game.state != LOBBY:
        return ack(call, "No game is open to join.", alert=True)
    uid = call.from_user.id
    if uid in game.players:
        game.players.pop(uid)
        game.order.remove(uid)
        ack(call, "You left the game.")
    else:
        game.players[uid] = disp_name(call.from_user)
        game.order.append(uid)
        ack(call, "You joined the game.")
    safe_edit(lobby_text(game), game.chat_id, game.msg_id, lobby_markup())


def handle_start_game(call, game):
    if not game or game.state != LOBBY:
        return ack(call, "No game to start.", alert=True)
    chat_id, uid = game.chat_id, call.from_user.id
    s = get_settings(chat_id)
    if s["admins_only_start"] and not is_admin(chat_id, uid):
        return ack(call, "Only admins can start games here.", alert=True)
    if not can_manage(chat_id, uid, game.starter_id):
        return ack(call, "Only the starter or an admin can start.", alert=True)
    if len(game.players) < s["min_players"]:
        return ack(call, f"Need at least {s['min_players']} players.", alert=True)
    assign_roles(game, forced=game.forced)
    safe_edit(reveal_text(game), chat_id, game.msg_id, reveal_markup())
    ack(call, "Game started.")


# --------- shared game phases ---------
def handle_see(call, game):
    if not game or game.state not in (REVEAL, DESCRIBE, VOTING):
        return ack(call, "Nothing to reveal right now.", alert=True)
    uid = call.from_user.id
    if uid not in game.players:
        return ack(call, "You are not in this game.", alert=True)
    if uid == game.chameleon_id:
        ack(call, "You are the CHAMELEON. You don't know the word - blend in!",
            alert=True)
    else:
        ack(call, f"Secret word: {game.secret}", alert=True)


def handle_start_round(call, game):
    if not game or game.state != REVEAL:
        return ack(call, "Can't start the round now.", alert=True)
    if not can_manage(game.chat_id, call.from_user.id, game.starter_id):
        return ack(call, "Only the starter or an admin can do this.", alert=True)
    game.state = DESCRIBE
    game.turn_index = 0
    game.hints = {}
    sent = bot.send_message(game.chat_id, turn_text(game), reply_markup=turn_markup(game))
    game.turn_msg_id = sent.message_id
    ack(call)


def handle_skip(call, game):
    if not game or game.state != DESCRIBE or hints_complete(game):
        return ack(call, "Nothing to skip.", alert=True)
    if not can_manage(game.chat_id, call.from_user.id, game.starter_id):
        return ack(call, "Only the starter or an admin can skip.", alert=True)
    current = game.order[game.turn_index]
    game.hints[current] = "(skipped)"
    game.turn_index += 1
    _advance_turn_msg(game)
    ack(call, "Skipped.")


def handle_start_vote(call, game):
    if not game or game.state != DESCRIBE:
        return ack(call, "Can't start voting now.", alert=True)
    if not hints_complete(game):
        return ack(call, "Finish all hints first.", alert=True)
    if not can_manage(game.chat_id, call.from_user.id, game.starter_id):
        return ack(call, "Only the starter or an admin can do this.", alert=True)
    game.state = VOTING
    game.votes = {}
    sent = bot.send_message(game.chat_id, voting_text(game),
                            reply_markup=voting_markup(game))
    game.vote_msg_id = sent.message_id
    ack(call)


def handle_vote(call, game, target_id):
    if not game or game.state != VOTING:
        return ack(call, "Voting is not open.", alert=True)
    uid = call.from_user.id
    if uid not in game.players:
        return ack(call, "Only players can vote.", alert=True)
    if target_id == uid:
        return ack(call, "You can't vote for yourself.", alert=True)
    game.votes[uid] = target_id
    ack(call, "Vote recorded.")
    safe_edit(voting_text(game), game.chat_id, game.vote_msg_id, voting_markup(game))
    if len(game.votes) >= len(game.players):
        finish_voting(game)


def handle_finish_vote(call, game):
    if not game or game.state != VOTING:
        return ack(call, "Voting is not open.", alert=True)
    if not can_manage(game.chat_id, call.from_user.id, game.starter_id):
        return ack(call, "Only the starter or an admin can do this.", alert=True)
    ack(call)
    finish_voting(game)


def finish_voting(game):
    """Tally the votes and either end the game or move to the guess phase."""
    counts = tally_votes(game.votes)
    tally = "\n".join(f"{game.players[uid]}: {counts.get(uid, 0)}" for uid in game.order)
    accused = decide_accused(game.votes)
    header = "Voting results:\n" + tally + "\n\n"

    caught = accused is not None and accused == game.chameleon_id
    if not caught:
        if accused is None:
            header += "No clear majority - the Chameleon slips away."
        else:
            header += (f"{mention(accused, game.players[accused])} was voted out, "
                       "but was not the Chameleon.")
        bot.send_message(game.chat_id, header)
        end_game(game, "blend")
        return

    header += f"{mention(accused, game.players[accused])} IS the Chameleon!"
    game.state = GUESS
    bot.send_message(game.chat_id, header)
    bot.send_message(
        game.chat_id,
        f"{mention(game.chameleon_id, game.players[game.chameleon_id])}, "
        "tap the secret word to make your guess:",
        reply_markup=guess_markup(game),
    )


def handle_guess(call, game, index):
    if not game or game.state != GUESS:
        return ack(call, "It's not guessing time.", alert=True)
    if call.from_user.id != game.chameleon_id:
        return ack(call, "Only the Chameleon guesses now.", alert=True)
    guess = game.words[index]
    ack(call, f"You guessed: {guess}")
    bot.send_message(game.chat_id, f"The Chameleon guessed: <b>{esc(guess)}</b>")
    end_game(game, "guess_correct" if guess == game.secret else "players_win")


def handle_abort_button(call, game):
    chat_id = call.message.chat.id
    if not busy(chat_id):
        return ack(call, "No game running.", alert=True)
    starter = (GAMES.get(chat_id) or SERIES.get(chat_id)).starter_id
    if not can_manage(chat_id, call.from_user.id, starter):
        return ack(call, "Only the starter or an admin can abort.", alert=True)
    abort_all(chat_id)
    safe_edit("Game aborted.", chat_id, call.message.message_id)
    ack(call, "Aborted.")


# --------------------------------------------------------------------------- #
# Scoring and end of a game
# --------------------------------------------------------------------------- #
def compute_points(game, kind):
    """Return (points_by_uid, chameleon_won, outcome_text)."""
    cham = game.chameleon_id
    points = {uid: 0 for uid in game.players}
    if kind == "blend":
        points[cham] = CHAM_BLEND_POINTS
        return points, True, "The Chameleon blended in and escaped the vote!"
    if kind == "guess_correct":
        points[cham] = CHAM_GUESS_POINTS
        return points, True, "The Chameleon was caught but guessed the word!"
    # players_win
    for uid in game.players:
        if uid != cham:
            points[uid] = PLAYER_WIN_POINTS
    return points, False, "Players win! The Chameleon was caught and guessed wrong."


def end_game(game, kind):
    chat_id = game.chat_id
    cham = game.chameleon_id
    points, cham_won, outcome = compute_points(game, kind)

    msg = (
        f"The Chameleon was {mention(cham, game.players[cham])}.\n"
        f"The secret word was <b>{esc(game.secret)}</b>.\n\n"
        f"<b>{outcome}</b>"
    )
    gained = [f"{game.players[uid]} +{points[uid]}"
              for uid in game.order if points[uid] > 0]
    if gained:
        msg += "\n\nPoints this round:\n" + "\n".join(gained)
    if not game.series:
        msg += "\n\nSend /start to play again, or /series for a series."
    bot.send_message(chat_id, msg)

    rows = [
        {"uid": uid, "name": game.players[uid], "points": points[uid],
         "won": points[uid] > 0, "was_cham": uid == cham,
         "cham_win": uid == cham and cham_won}
        for uid in game.players
    ]
    try:
        db.record_round(chat_id, rows)
    except Exception:
        pass

    GAMES.pop(chat_id, None)

    if game.series:
        s = game.series
        for uid, p in points.items():
            s.scores[uid] = s.scores.get(uid, 0) + p
        if s.index < s.total:
            s.state = "BETWEEN"
            m = types.InlineKeyboardMarkup()
            m.add(types.InlineKeyboardButton(
                f"Start game {s.index + 1}/{s.total}", callback_data="series_next"))
            bot.send_message(chat_id, series_standings_text(s), reply_markup=m)
        else:
            finish_series(s)


# --------------------------------------------------------------------------- #
# Series
# --------------------------------------------------------------------------- #
def series_lobby_text(series):
    s = get_settings(series.chat_id)
    lines = [f"<b>Chameleon series - best of {series.total}</b>", ""]
    if series.players:
        lines.append("Players joined:")
        for i, uid in enumerate(series.order, 1):
            lines.append(f"{i}. {mention(uid, series.players[uid])}")
    else:
        lines.append("No players yet.")
    lines.append("")
    lines.append(f"Need at least {s['min_players']} players. "
                 "Tap Join, then the starter taps Start Series.")
    return "\n".join(lines)


def series_lobby_markup():
    m = types.InlineKeyboardMarkup()
    m.add(types.InlineKeyboardButton("Join / Leave", callback_data="sjoin"))
    m.add(
        types.InlineKeyboardButton("Start Series", callback_data="sstart"),
        types.InlineKeyboardButton("Abort", callback_data="abort"),
    )
    return m


def series_standings_text(series):
    ranked = sorted(series.players.keys(), key=lambda u: -series.scores.get(u, 0))
    lines = [f"<b>Series standings (after game {series.index}/{series.total})</b>", ""]
    for i, uid in enumerate(ranked, 1):
        lines.append(f"{i}. {mention(uid, series.players[uid])} - "
                     f"{series.scores.get(uid, 0)} pts")
    return "\n".join(lines)


def handle_series_len(call, n):
    chat_id = call.message.chat.id
    if busy(chat_id):
        return ack(call, "A game is already running here.", alert=True)
    series = Series(chat_id, call.from_user.id, n)
    SERIES[chat_id] = series
    safe_edit(series_lobby_text(series), chat_id, call.message.message_id,
              series_lobby_markup())
    series.msg_id = call.message.message_id
    ack(call, f"Best of {n} selected.")


def handle_sjoin(call):
    series = SERIES.get(call.message.chat.id)
    if not series or series.state != LOBBY:
        return ack(call, "No series is open to join.", alert=True)
    uid = call.from_user.id
    if uid in series.players:
        series.players.pop(uid)
        series.order.remove(uid)
        ack(call, "You left the series.")
    else:
        series.players[uid] = disp_name(call.from_user)
        series.order.append(uid)
        ack(call, "You joined the series.")
    safe_edit(series_lobby_text(series), series.chat_id, series.msg_id,
              series_lobby_markup())


def handle_sstart(call):
    series = SERIES.get(call.message.chat.id)
    if not series or series.state != LOBBY:
        return ack(call, "No series to start.", alert=True)
    chat_id, uid = series.chat_id, call.from_user.id
    s = get_settings(chat_id)
    if s["admins_only_start"] and not is_admin(chat_id, uid):
        return ack(call, "Only admins can start here.", alert=True)
    if not can_manage(chat_id, uid, series.starter_id):
        return ack(call, "Only the starter or an admin can start.", alert=True)
    if len(series.players) < s["min_players"]:
        return ack(call, f"Need at least {s['min_players']} players.", alert=True)
    series.scores = {u: 0 for u in series.players}
    safe_edit(f"Series started - best of {series.total}.", chat_id, series.msg_id)
    ack(call, "Series started.")
    start_series_game(series)


def start_series_game(series):
    series.index += 1
    series.state = "PLAYING"
    game = Game(series.chat_id, series.starter_id, series=series)
    game.players = dict(series.players)
    GAMES[series.chat_id] = game
    # Fresh shuffle + weighted Chameleon each game, avoiding last game's Chameleon.
    assign_roles(game, avoid=series.last_cham)
    series.last_cham = game.chameleon_id
    sent = bot.send_message(series.chat_id, reveal_text(game),
                            reply_markup=reveal_markup())
    game.msg_id = sent.message_id


def handle_series_next(call):
    chat_id = call.message.chat.id
    series = SERIES.get(chat_id)
    if not series or series.state != "BETWEEN" or chat_id in GAMES:
        return ack(call, "Not ready for the next game.", alert=True)
    if not can_manage(chat_id, call.from_user.id, series.starter_id):
        return ack(call, "Only the starter or an admin can do this.", alert=True)
    ack(call)
    start_series_game(series)


def finish_series(series):
    chat_id = series.chat_id
    ranked = sorted(series.players.keys(), key=lambda u: -series.scores.get(u, 0))
    top = series.scores.get(ranked[0], 0) if ranked else 0
    winners = [u for u in ranked if series.scores.get(u, 0) == top]
    lines = [f"<b>Series over - best of {series.total}</b>", ""]
    for i, uid in enumerate(ranked, 1):
        lines.append(f"{i}. {mention(uid, series.players[uid])} - "
                     f"{series.scores.get(uid, 0)} pts")
    lines.append("")
    if top == 0:
        lines.append("No points scored - call it a draw!")
    elif len(winners) == 1:
        lines.append(f"Winner: {mention(winners[0], series.players[winners[0]])} "
                     f"with {top} points!")
    else:
        names = ", ".join(mention(u, series.players[u]) for u in winners)
        lines.append(f"It's a tie between {names} with {top} points each!")
    lines.append("\nSend /series to play again.")
    SERIES.pop(chat_id, None)
    bot.send_message(chat_id, "\n".join(lines))


# --------------------------------------------------------------------------- #
# Startup
# --------------------------------------------------------------------------- #
def setup_commands():
    general = [
        types.BotCommand("start", "start a single game"),
        types.BotCommand("series", "start a game series"),
        types.BotCommand("profsp", "special game: Professors"),
        types.BotCommand("batchsp", "special game: Batchmates"),
        types.BotCommand("hint", "give your hint: /hint your clue"),
        types.BotCommand("game_rules", "how to play"),
        types.BotCommand("stats", "group leaderboard"),
        types.BotCommand("help", "show help"),
    ]
    group = general + [
        types.BotCommand("nextgame", "join the next-game ping list"),
        types.BotCommand("settings", "group settings (admins)"),
        types.BotCommand("abort_game", "abort the current game"),
        types.BotCommand("admins_reload", "reload admins cache"),
        types.BotCommand("id", "show chat id"),
    ]
    try:
        bot.set_my_commands(general, scope=types.BotCommandScopeDefault())
        bot.set_my_commands(group, scope=types.BotCommandScopeAllGroupChats())
    except Exception:
        pass


def detect_privacy():
    """Read the bot's privacy setting so we can warn if plain !hints won't work."""
    global CAN_READ_MESSAGES
    try:
        me = bot.get_me()
        val = getattr(me, "can_read_all_group_messages", None)
        if val is not None:
            CAN_READ_MESSAGES = bool(val)
    except Exception:
        pass
    if not CAN_READ_MESSAGES:
        print(
            "\n*** WARNING: Group Privacy appears to be ON. ***\n"
            "The bot will NOT receive plain '!hint' messages, so those turns will\n"
            "seem stuck. Two options:\n"
            "  1) Players use  /hint your clue  (a command - always works), or\n"
            "  2) In BotFather: Bot Settings -> Group Privacy -> Turn OFF, then\n"
            "     REMOVE and RE-ADD the bot to your group (required to take effect).\n"
        )


@app.get("/healthz")
def healthz():
    return jsonify(status="ok")


@app.get("/")
def root():
    return jsonify(service="chameleon-bot", status="ok")


@app.post("/telegram/webhook")
def telegram_webhook():
    expected_secret = config.WEBHOOK_SECRET
    if expected_secret and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != expected_secret:
        abort(403)
    update = request.get_json(silent=True)
    if update:
        update_id = update.get("update_id", "unknown")
        print(f"Received Telegram update {update_id}", flush=True)
        try:
            bot.process_new_updates([types.Update.de_json(update)])
        except Exception:
            app.logger.exception("Failed to process Telegram update %s", update_id)
            raise
    return "", 200


def configure_webhook():
    if not config.RENDER_EXTERNAL_URL:
        return False
    if not config.WEBHOOK_SECRET:
        raise SystemExit("WEBHOOK_SECRET is required when RENDER_EXTERNAL_URL is set.")
    db.init_db()
    detect_privacy()
    setup_commands()
    bot.set_webhook(
        url=f"{config.RENDER_EXTERNAL_URL}/telegram/webhook",
        secret_token=config.WEBHOOK_SECRET,
        drop_pending_updates=True,
    )
    print("Chameleon bot webhook is configured.")
    return True


WEBHOOK_CONFIGURED = configure_webhook() if config.RENDER_EXTERNAL_URL else False


def main():
    if WEBHOOK_CONFIGURED:
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
        return
    db.init_db()
    detect_privacy()
    setup_commands()
    print("Chameleon bot is running. Press Ctrl+C to stop.")
    bot.infinity_polling(skip_pending=True, timeout=30)


if __name__ == "__main__":
    main()
