"""A small arrow-key menu for the terminal, standard library only: ↑/↓ (or j/k) and Enter, a number to jump straight
to an option, q or Esc to quit."""
import os
import sys

UP, DOWN, ENTER, QUIT = "up", "down", "enter", "quit"


def step(key: str, index: int, count: int) -> tuple[int, bool | None]:
    """One key press: the new highlighted index, and True when picked, False when quit, None to keep going."""
    if key == UP:
        return (index - 1) % count, None
    if key == DOWN:
        return (index + 1) % count, None
    if key == ENTER:
        return index, True
    if key == QUIT:
        return index, False
    if key.isdigit() and 1 <= int(key) <= min(count, 9):
        return int(key) - 1, True
    return index, None


def _read_key() -> str:
    if os.name == "nt":
        import msvcrt

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            return {"H": UP, "P": DOWN}.get(msvcrt.getwch(), "")
        return _plain(ch)

    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        ch = os.read(fd, 1).decode(errors="ignore")
        if ch == "\x1b":
            # An arrow key sends ESC [ A; a lone ESC means quit
            if not select.select([fd], [], [], 0.05)[0]:
                return QUIT
            seq = os.read(fd, 2).decode(errors="ignore")
            return {"[A": UP, "[B": DOWN, "OA": UP, "OB": DOWN}.get(seq, "")
        return _plain(ch)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def _plain(ch: str) -> str:
    if ch in ("\r", "\n"):
        return ENTER
    if ch in ("q", "Q", "\x03", "\x04", "\x1b"):  # q, Ctrl-C, Ctrl-D, Esc
        return QUIT
    if ch in ("k", "K"):
        return UP
    if ch in ("j", "J"):
        return DOWN
    return ch


def pick(options: list[str], hint: str, start: int = 0, out=sys.stderr) -> int | None:
    """Shows the options and returns the index chosen, or None if they quit."""
    if os.name == "nt":
        os.system("")  # turns on ANSI escape codes in the Windows console
    index = max(0, min(start, len(options) - 1))
    lines = len(options) + 2

    def draw(first: bool) -> None:
        if not first:
            out.write(f"\x1b[{lines}F")  # back to the top of the menu
        for i, option in enumerate(options):
            marker = "\x1b[36m❯\x1b[0m" if i == index else " "
            text = f"\x1b[1m{option}\x1b[0m" if i == index else option
            out.write(f"\x1b[2K{marker} {i + 1}. {text}\n")
        out.write(f"\x1b[2K\n\x1b[2K\x1b[2m{hint}\x1b[0m\n")
        out.flush()

    out.write("\x1b[?25l")  # hide the cursor while the menu is up
    try:
        draw(True)
        while True:
            index, done = step(_read_key(), index, len(options))
            draw(False)
            if done is not None:
                return index if done else None
    except KeyboardInterrupt:
        return None
    finally:
        out.write("\x1b[?25h")
        out.flush()
