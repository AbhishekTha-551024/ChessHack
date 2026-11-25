"""
chess_bot_screen.py - plays chess on your Android phone with Stockfish (FREE).
For apps where pieces are NOT readable from the UI tree (only pictures).

How it works:
  * The game must start from the NORMAL starting position (fresh game).
  * Board position on screen comes from the UI tree (the 'board_view' squares).
  * After each move it takes screenshots and finds which squares changed,
    then works out the opponent's move from the legal moves.
  * Stockfish picks our move, the script taps from-square and to-square.

Setup:
  pip install chess pillow
  Stockfish .exe path via --engine (or put stockfish*.exe next to this script)

Run (fresh game on the phone, board visible):
  python chess_bot_screen.py --engine "C:\\path\\stockfish.exe" --confirm
Options:
  --time 1.0     seconds Stockfish thinks per move
  --skill 10     0 (weak) .. 20 (strongest)
  --black        force 'I play Black' if auto-detect is wrong
  --threshold 4  change sensitivity in % of pixels (lower = more sensitive)
  --debug        print which squares changed

Use for games against the computer / practice. Using an engine in rated games
against real people on Chess.com / Lichess etc. breaks their rules.
Stop anytime: Ctrl+C
"""
import io, os, re, sys, glob, time, argparse, subprocess, statistics
import xml.etree.ElementTree as ET
import chess, chess.engine
from PIL import Image, ImageChops

BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


# ------------------------------------------------------------------ ADB
def adb_bytes(*a):
    return subprocess.run(["adb"] + list(a), capture_output=True).stdout


def adb(*a):
    return adb_bytes(*a).decode("utf-8", errors="ignore")


def tap(x, y):
    adb("shell", "input", "tap", str(int(x)), str(int(y)))


def screenshot():
    return Image.open(io.BytesIO(adb_bytes("exec-out", "screencap", "-p"))).convert("RGB")


def dump_root():
    adb("shell", "uiautomator", "dump", "/sdcard/ui.xml")
    xml = adb("shell", "cat", "/sdcard/ui.xml")
    s = xml.find("<?xml")
    if s == -1:
        s = xml.find("<hierarchy")
    if s == -1:
        return None
    try:
        return ET.fromstring(xml[s:])
    except ET.ParseError:
        return None


# ------------------------------------------------------------------ geometry
class Geometry:
    def __init__(self, x0, y0, s, normal):
        self.x0, self.y0, self.s, self.normal = x0, y0, s, normal

    def _cell(self, sq):
        f, r = chess.square_file(sq), chess.square_rank(sq)
        col, row = (f, 7 - r) if self.normal else (7 - f, r)
        return self.x0 + col * self.s, self.y0 + row * self.s

    def center(self, sq):
        left, top = self._cell(sq)
        return left + self.s / 2, top + self.s / 2

    def box(self, sq):
        left, top = self._cell(sq)
        m = 0.15 * self.s          # skip edges (coordinate labels, borders)
        return (int(left + m), int(top + m), int(left + self.s - m), int(top + self.s - m))


def read_geometry(force_black=False):
    root = dump_root()
    if root is None:
        return None
    boxes, labels = [], []
    for n in root.iter("node"):
        if not n.get("resource-id", "").endswith("board_view"):
            continue
        b = BOUNDS.match(n.get("bounds", ""))
        if not b:
            continue
        x1, y1, x2, y2 = map(int, b.groups())
        txt = n.get("text", "").strip()
        if n.get("class", "").endswith("FrameLayout") and x2 - x1 >= 60:
            boxes.append((x1, y1, x2 - x1))
        elif txt.isdigit():
            labels.append((y1, txt))
    if len(boxes) < 32:
        return None
    s = statistics.median(b[2] for b in boxes)
    x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
    normal = (min(labels)[1] == "8") if labels else True
    if force_black:
        normal = False
    return Geometry(x0, y0, s, normal)


# ------------------------------------------------------------------ change detection
def square_images(shot, geo):
    return {sq: shot.crop(geo.box(sq)) for sq in chess.SQUARES}


def diff_score(a, b):
    """Percent of pixels that differ clearly (robust for white pieces on light squares)."""
    r, g, bl = ImageChops.difference(a, b).split()
    m = ImageChops.lighter(ImageChops.lighter(r, g), bl)
    return 100.0 * sum(m.histogram()[41:]) / (a.width * a.height)


def changed_squares(base, imgs, thr):
    return {sq for sq in chess.SQUARES if diff_score(base[sq], imgs[sq]) > thr}


def looks_like_start(imgs):
    """Rough check: ranks 3-6 empty, ranks 1,2,7,8 occupied."""
    for sq, im in imgs.items():
        r = chess.square_rank(sq)
        has_piece = max(hi - lo for lo, hi in im.getextrema()) > 40
        if r in (2, 3, 4, 5) and has_piece:
            return False
        if r in (0, 1, 6, 7) and not has_piece:
            return False
    return True


def expected_squares(board, mv):
    sq = {mv.from_square, mv.to_square}
    rank = chess.square_rank(mv.from_square)
    if board.is_castling(mv):
        if chess.square_file(mv.to_square) == 6:
            sq |= {chess.square(7, rank), chess.square(5, rank)}
        else:
            sq |= {chess.square(0, rank), chess.square(3, rank)}
    if board.is_en_passant(mv):
        sq.add(chess.square(chess.square_file(mv.to_square), rank))
    return sq


def match_move(board, changed, ignore):
    """Legal move whose squares all changed, with the fewest unexplained changes."""
    best, best_key = None, None
    for mv in board.legal_moves:
        if mv.promotion and mv.promotion != chess.QUEEN:
            continue
        exp = expected_squares(board, mv)
        if not exp <= changed:
            continue
        key = (-len(changed - exp - ignore), len(exp))
        if best_key is None or key > best_key:
            best, best_key = mv, key
    return best


# ------------------------------------------------------------------ playing
def play_taps(mv, geo):
    fx, fy = geo.center(mv.from_square)
    tx, ty = geo.center(mv.to_square)
    tap(fx, fy)
    time.sleep(0.6)
    tap(tx, ty)


def wait_opponent(board, geo, base, exp_ours, thr, debug, retry_move=None, timeout=240):
    """board = position AFTER our move (or start position if opponent moves first).
    base = square images from BEFORE our move. Returns (move, new_base_images)."""
    t0, retried, fails = time.time(), False, 0
    while time.time() - t0 < timeout:
        time.sleep(1.2)
        imgs = square_images(screenshot(), geo)
        ch = changed_squares(base, imgs, thr)
        if debug:
            print("   changed:", sorted(chess.square_name(s) for s in ch))
        if retry_move is not None and not retried and time.time() - t0 > 4 and not exp_ours <= ch:
            print("  My move did not register, tapping again...")
            play_taps(retry_move, geo)
            retried = True
            continue
        if len(ch - exp_ours) < 1:
            continue                       # opponent has not moved yet
        time.sleep(1.0)                    # let animations finish
        imgs = square_images(screenshot(), geo)
        ch = changed_squares(base, imgs, thr)
        mv = match_move(board, ch, exp_ours)
        if mv:
            return mv, imgs
        fails += 1
        names = sorted(chess.square_name(s) for s in ch)
        if fails <= 3:
            print(f"  Not clear yet (changed: {names}), looking again...")
            time.sleep(1.5)
            continue
        print("  Could not work out the opponent's move. Changed squares:", names)
        try:
            screenshot().save("stuck.png")
            print("  (saved screenshot to stuck.png)")
        except Exception:
            pass
        print("  Look at the phone: what did the computer just play?")
        txt = input("  Type it like g8e7 or Nxe7 (Enter = look again): ").strip()
        if txt:
            mv = None
            for parse in (board.parse_san, board.parse_uci):
                try:
                    mv = parse(txt); break
                except ValueError:
                    pass
            if mv is not None and mv in board.legal_moves:
                return mv, imgs
            print("  That is not a legal move in this position.")
        fails = 0
    return None, None


def find_engine(arg):
    for p in (arg, os.environ.get("STOCKFISH_PATH")):
        if p and os.path.exists(p):
            return p
    here = os.path.dirname(os.path.abspath(__file__))
    for pat in ("stockfish*.exe", "**/stockfish*.exe"):
        hits = glob.glob(os.path.join(here, pat), recursive=True)
        if hits:
            return hits[0]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine")
    ap.add_argument("--time", type=float, default=0.7)
    ap.add_argument("--skill", type=int, default=20)
    ap.add_argument("--black", action="store_true")
    ap.add_argument("--threshold", type=float, default=4)
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    path = find_engine(args.engine)
    if not path:
        print("Stockfish not found. Use --engine \"C:\\path\\stockfish.exe\"")
        sys.exit(1)
    geo = read_geometry(args.black)
    if geo is None:
        print("Could not find the chess board on the phone screen. Open the game board first.")
        sys.exit(1)
    our = chess.WHITE if geo.normal else chess.BLACK
    print(f"Board at x={geo.x0:.0f}, y={geo.y0:.0f}, square={geo.s:.0f}px | "
          f"You play {'White' if our else 'Black'} (bottom of screen)")
    print("Make sure this is a FRESH game from the starting position.")

    if not looks_like_start(square_images(screenshot(), geo)):
        print("WARNING: the board does not look like a fresh starting position.")
        print("Press the restart button in the game first.")
        if input("Continue anyway? (y/n): ").strip().lower() != "y":
            sys.exit(0)

    board = chess.Board()
    engine = chess.engine.SimpleEngine.popen_uci(path)
    try:
        engine.configure({"Skill Level": max(0, min(20, args.skill))})
    except Exception:
        pass

    try:
        while not board.is_game_over():
            base = square_images(screenshot(), geo)
            mv, exp_ours = None, set()
            if board.turn == our:
                mv = engine.play(board, chess.engine.Limit(time=args.time)).move
                print(f"My move: {board.san(mv)} ({mv.uci()})")
                if args.confirm and input("  Play it? (y/n): ").strip().lower() != "y":
                    print("Stopped."); break
                exp_ours = expected_squares(board, mv)
                play_taps(mv, geo)
                board.push(mv)
                if mv.promotion:
                    input("  Promotion: choose the Queen on the phone, then press Enter...")
                if board.is_game_over():
                    break
            omv, imgs = wait_opponent(board, geo, base, exp_ours, args.threshold,
                                      args.debug, retry_move=mv)
            if omv is None:
                print("Opponent did not move for a long time. Stopping."); break
            print(f"  Opponent played: {board.san(omv)} ({omv.uci()})")
            board.push(omv)
        if board.is_game_over():
            print("Game over:", board.result(), "-", board.outcome().termination.name)
    finally:
        engine.quit()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user.")