"""Guess which game a recording shows from its name (job id / source file / stream title).

Used to give the local LLM the game when it writes hooks and titles, so a Flight Simulator or NHL 2001 clip
is not titled like a shooter. Keywords are matched as whole words on a lower-cased, underscore-free name.
"""
import re
from typing import Tuple

# (keywords, display name, is_shooter)
GAMES = [
    (("forza", "fh6"), "Forza Horizon 6", False),
    (("battlefield", "bf6", "bf1", "conquest"), "Battlefield", True),
    (("call of duty", "callofduty", "black ops", "bo7", "warzone", "cod", "nuketown", "modern warfare"), "Call of Duty", True),
    (("beamng",), "BeamNG.drive", False),
    (("wardogs",), "WARDOGS", True),
    (("hell let loose", "hll"), "Hell Let Loose", True),
    (("bodycam",), "Bodycam", True),
    (("fortnite",), "Fortnite", True),
    (("chivalry",), "Chivalry 2", False),
    (("need for speed", "nfs", "nfsu2", "underground"), "Need for Speed Underground 2", False),
    (("worldofwarships", "world of warships", "warships"), "World of Warships", False),
    (("flightsimulator", "flight simulator", "msfs"), "Microsoft Flight Simulator", False),
    (("granturismo", "gran turismo", "granturismo2"), "Gran Turismo 2", False),
    (("nhl", "nhl2001"), "NHL 2001", False),
    (("tripleplay", "tripleplaybaseball", "baseball"), "Triple Play Baseball", False),
    (("bmx", "mathoffman", "mathoffmanbmx"), "Mat Hoffman's Pro BMX", False),
    (("ai coding", "coding"), "an AI coding session", False),
]


def guess_game(name: str) -> Tuple[str, bool]:
    """Returns (game name or "", is_shooter). Unknown games count as shooters (most streams are)."""
    hay = re.sub(r"[_\-]+", " ", str(name or "")).lower()
    for keys, game, shooter in GAMES:
        if any(re.search(rf"\b{re.escape(k)}\b", hay) for k in keys):
            return game, shooter
    return "", True
