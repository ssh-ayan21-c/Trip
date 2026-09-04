"""Pure game logic with no Telegram dependencies, so it can be unit-tested."""


def tally_votes(votes):
    """votes: dict of voter_id -> target_id. Returns dict of target_id -> count."""
    counts = {}
    for target in votes.values():
        counts[target] = counts.get(target, 0) + 1
    return counts


def decide_accused(votes):
    """Return the single most-voted user_id, or None if nobody voted or there
    is a tie for the top spot (no clear accusation)."""
    counts = tally_votes(votes)
    if not counts:
        return None
    top = max(counts.values())
    leaders = [uid for uid, c in counts.items() if c == top]
    return leaders[0] if len(leaders) == 1 else None


def resolve_round(votes, chameleon_id, guess=None, secret=None):
    """Decide the outcome of a round.

    Returns a dict:
      accused: the accused user_id (or None)
      caught:  True if the accused is uniquely the chameleon
      needs_guess: True if the chameleon was caught and must now guess
      chameleon_won: final result once known (None while a guess is pending)

    If the chameleon was caught, call again with guess/secret to get the result.
    """
    accused = decide_accused(votes)
    caught = accused is not None and accused == chameleon_id

    if not caught:
        # Chameleon not identified -> chameleon wins.
        return {"accused": accused, "caught": False,
                "needs_guess": False, "chameleon_won": True}

    if guess is None:
        # Caught, but the guess has not happened yet.
        return {"accused": accused, "caught": True,
                "needs_guess": True, "chameleon_won": None}

    # Caught and guessed: chameleon wins only if the guess is correct.
    return {"accused": accused, "caught": True,
            "needs_guess": False, "chameleon_won": (guess == secret)}
