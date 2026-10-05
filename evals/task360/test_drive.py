"""The driver's trust-dialog answer, on hand-built screens.

    uv run --no-sync pytest evals/task360/test_drive.py -q
"""

import subprocess
from pathlib import Path

DRIVE = Path(__file__).parent / "drive.sh"


def _keys(screen: str) -> str:
    return subprocess.run(
        ["bash", "-c", f'source "{DRIVE}" && _trust_keys'],
        input=screen, capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_cursor_on_yes_confirms():
    assert _keys("Do you trust this folder?\n❯ 1. Yes, proceed\n  2. No, exit\n") == "enter"


def test_cursor_below_yes_moves_up():
    assert _keys("Trust this directory?\n  1. Yes, continue\n› 2. No, quit\n") == "up enter"


def test_cursor_above_yes_moves_down():
    screen = "Trust the files here?\n❯ 1. No, exit\n  2. Not now\n  3. Yes, I trust it\n"
    assert _keys(screen) == "down down enter"
