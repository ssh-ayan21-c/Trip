"""Quick self-tests for the game logic, categories, and stats DB.

Run:  python test_logic.py
These do not touch Telegram - they only check the pure logic and the database.
"""
import os
import tempfile

# Configure environment BEFORE importing project modules.
os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ["DATABASE_URL"] = ""
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["STATS_DB"] = _tmp.name

import logic
import db
from categories import CATEGORIES, SPECIAL

# A game samples between these many words; special lists must have at least the
# minimum so a valid subset always exists.
GAME_WORD_MIN = 12


def test_categories():
    assert len(CATEGORIES) >= 10, "expected many categories"
    for name, words in CATEGORIES.items():
        assert isinstance(name, str) and name.strip(), f"bad category name: {name!r}"
        assert len(words) >= 8, f"'{name}' has too few words ({len(words)})"
        assert all(isinstance(w, str) and w.strip() for w in words), \
            f"'{name}' has an empty word"
        assert len(set(words)) == len(words), f"'{name}' has duplicate words"
    print(f"OK  categories: {len(CATEGORIES)} categories, all valid")


def test_special():
    assert set(SPECIAL) >= {"profsp", "batchsp"}, SPECIAL.keys()
    for cmd, entry in SPECIAL.items():
        assert isinstance(entry, tuple) and len(entry) == 2, f"{cmd}: bad entry"
        theme, words = entry
        assert isinstance(theme, str) and theme.strip(), f"{cmd}: bad theme name"
        assert len(words) >= GAME_WORD_MIN, \
            f"{cmd} needs >= {GAME_WORD_MIN} names (has {len(words)})"
        assert all(isinstance(w, str) and w.strip() for w in words), \
            f"{cmd} has an empty name"
        assert len(set(words)) == len(words), f"{cmd} has duplicate names"
        # Special themes must stay out of the random pool.
        assert theme not in CATEGORIES, f"{theme} must not be a random category"
    print(f"OK  special lists: {', '.join(sorted(SPECIAL))} valid and kept private")


def test_decide_accused():
    assert logic.decide_accused({}) is None
    # user 2 gets 2 votes, user 5 gets 1 -> 2 is accused
    assert logic.decide_accused({1: 2, 3: 2, 4: 5}) == 2
    # tie between 2 and 5 -> nobody accused
    assert logic.decide_accused({1: 2, 3: 5}) is None
    print("OK  decide_accused")


def test_resolve_round():
    cham = 2

    # Chameleon is uniquely voted out -> caught, must guess.
    r = logic.resolve_round({1: 2, 3: 2, 4: 5}, cham)
    assert r["caught"] and r["needs_guess"] and r["chameleon_won"] is None

    # Correct guess -> chameleon wins.
    r = logic.resolve_round({1: 2, 3: 2}, cham, guess="Tiger", secret="Tiger")
    assert r["caught"] and r["chameleon_won"] is True

    # Wrong guess -> players win.
    r = logic.resolve_round({1: 2, 3: 2}, cham, guess="Lion", secret="Tiger")
    assert r["caught"] and r["chameleon_won"] is False

    # A normal player is voted out -> chameleon wins, no guess.
    r = logic.resolve_round({1: 3, 4: 3}, cham)
    assert not r["caught"] and r["chameleon_won"] is True and not r["needs_guess"]

    # Tie -> chameleon escapes and wins.
    r = logic.resolve_round({1: 2, 3: 5}, cham)
    assert r["accused"] is None and r["chameleon_won"] is True
    print("OK  resolve_round")


def _round(names_pts, cham, cham_won):
    """Build a rows list for db.record_round.

    names_pts: list of (uid, name, points). A player 'won' if points > 0.
    cham: the chameleon's uid. cham_won: whether the chameleon won this round.
    """
    return [
        {"uid": uid, "name": name, "points": pts,
         "won": pts > 0, "was_cham": uid == cham,
         "cham_win": uid == cham and cham_won}
        for uid, name, pts in names_pts
    ]


def test_db():
    db.init_db()
    chat = -1001234567890
    cham = 2

    # Round 1: players win (chameleon caught, guessed wrong) -> players +1 each.
    db.record_round(chat, _round(
        [(1, "Ayan", 1), (2, "Riya", 0), (3, "Sam", 1)], cham, cham_won=False))
    # Round 2: chameleon blends in and wins big -> Riya +3.
    db.record_round(chat, _round(
        [(1, "Ayan", 0), (2, "Riya", 3), (3, "Sam", 0)], cham, cham_won=True))

    riya = db.get_user_stats(chat, 2)
    assert riya["games"] == 2, riya
    assert riya["cham_count"] == 2, riya
    assert riya["cham_wins"] == 1, riya
    assert riya["wins"] == 1, riya       # scored only in round 2
    assert riya["points"] == 3, riya     # 0 + 3

    ayan = db.get_user_stats(chat, 1)
    assert ayan["games"] == 2, ayan
    assert ayan["cham_count"] == 0, ayan
    assert ayan["wins"] == 1, ayan       # scored in round 1
    assert ayan["points"] == 1, ayan     # 1 + 0

    board = db.get_leaderboard(chat)
    assert len(board) == 3, board
    assert board[0]["username"] == "Riya", board  # most points on top
    assert board[0]["points"] == 3, board
    print("OK  db: recording, points, and leaderboard")


if __name__ == "__main__":
    test_categories()
    test_special()
    test_decide_accused()
    test_resolve_round()
    test_db()
    os.unlink(_tmp.name)
    print("\nAll tests passed.")
