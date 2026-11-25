"""
chess_bot_v2.py - plays chess on your Android phone with Stockfish (FREE).
For apps where pieces are only pictures (not readable from the UI tree).

v2 idea: every time, read the WHOLE board from a screenshot as
"each square = empty / white piece / black piece" and compare it with the
position the bot expects. So mistakes do not pile up, and a tap that did not
register is detected and repeated automatically.

Setup:   pip install chess pillow
Run (FRESH game, board visible, your side at the bottom):
  python chess_bot_v2.py --engine "C:\\path\\stockfish.exe" --confirm
Options:  --time 1.0  --skill 10  --black  --debug
Use for games vs the computer / practice only (not rated games vs people).
Stop anytime: Ctrl+C
"""
import io, os, re, sys, glob, time, argparse, subprocess, statistics
import xml.etree.ElementTree as ET
import chess, chess.engine
from PIL import Image, ImageChops, ImageStat

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

    def cell(self, sq):
        f, r = chess.square_file(sq), chess.square_rank(sq)
        col, row = (f, 7 - r) if self.normal else (7 - f, r)
        return self.x0 + col * self.s, self.y0 + row * self.s

    def center(self, sq):
        left, top = self.cell(sq)
        return left + self.s / 2, top + self.s / 2

    def inner_box(self, sq):
        left, top = self.cell(sq)
        m = 0.15 * self.s
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
    return Geometry(x0, y0, s, normal and not force_black)


# ------------------------------------------------------------------ reading the board
class Reader:
    """Turns a screenshot into {square: color} (True = white piece, False = black piece)."""

    def __init__(self, geo):
        self.geo = geo
        self.lum_thr = 128.0   # white/black split (calibrated at start)
        self.occ_thr = 5.0     # % of pixels that must differ from the square background

    def _background(self, shot, sq):
        left, top = self.geo.cell(sq)
        s, p = self.geo.s, 8
        m = int(0.06 * s)
        corners = [(left + m, top + m), (left + s - m - p, top + m),
                   (left + m, top + s - m - p), (left + s - m - p, top + s - m - p)]
        cols = []
        for x, y in corners:
            cols.append(ImageStat.Stat(shot.crop((int(x), int(y), int(x + p), int(y + p)))).mean)
        best = max(cols, key=lambda c: sum(1 for o in cols if max(abs(c[i] - o[i]) for i in range(3)) < 30))
        group = [c for c in cols if max(abs(best[i] - c[i]) for i in range(3)) < 30]
        return tuple(int(sum(c[i] for c in group) / len(group)) for i in range(3))

    def stats(self, shot, sq):
        inner = shot.crop(self.geo.inner_box(sq))
        bg = self._background(shot, sq)
        d = ImageChops.difference(inner, Image.new("RGB", inner.size, bg))
        r, g, b = d.split()
        m = ImageChops.lighter(ImageChops.lighter(r, g), b)
        mask = m.point(lambda v: 255 if v > 40 else 0)
        frac = 100.0 * mask.histogram()[255] / (inner.width * inner.height)
        lum = ImageStat.Stat(inner.convert("L"), mask).mean[0] if frac > 0 else None
        return frac, lum

    def calibrate(self, shot):
        """Uses the starting position to learn white/black brightness. Returns True if it looks right."""
        w, bl, ok = [], [], True
        for sq in chess.SQUARES:
            frac, lum = self.stats(shot, sq)
            r = chess.square_rank(sq)
            if r in (2, 3, 4, 5):
                if frac > self.occ_thr:
                    ok = False
            else:
                if frac <= self.occ_thr or lum is None:
                    ok = False
                elif r in (0, 1):
                    w.append(lum)
                else:
                    bl.append(lum)
        if w and bl:
            self.lum_thr = (sum(w) / len(w) + sum(bl) / len(bl)) / 2
            if sum(w) / len(w) - sum(bl) / len(bl) < 25:
                ok = False
        return ok

    def read(self, shot):
        out = {}
        for sq in chess.SQUARES:
            frac, lum = self.stats(shot, sq)
            if frac > self.occ_thr and lum is not None:
                out[sq] = lum > self.lum_thr
        return out


def bmap(board):
    return {sq: p.color for sq, p in board.piece_map().items()}


def dist(a, b):
    return sum(1 for sq in chess.SQUARES if a.get(sq) != b.get(sq))


# ------------------------------------------------------------------ playing
def play_taps(mv, geo):
    fx, fy = geo.center(mv.from_square)
    tx, ty = geo.center(mv.to_square)
    tap(fx, fy)
    time.sleep(0.8)
    tap(tx, ty)


def wait_for_screen(board, reader, before_map, my_move, geo, debug, timeout=300):
    """board = expected position now (after our move, or the start if the opponent moves first).
    Waits until the screen shows the opponent's reply. Returns the opponent's move or None."""
    noop = bmap(board)
    cands = []
    for mv in board.legal_moves:
        if mv.promotion and mv.promotion != chess.QUEEN:
            continue
        board.push(mv); cands.append((mv, bmap(board))); board.pop()

    t0, retries, bad, last = time.time(), 0, 0, None
    while time.time() - t0 < timeout:
        time.sleep(1.2)
        scr = reader.read(screenshot())
        d_noop = dist(scr, noop)
        d_best, best = min(((dist(scr, m), mv) for mv, m in cands), key=lambda t: t[0])
        if debug:
            print(f"   dist(no reply yet)={d_noop}  best reply {best.uci()} dist={d_best}")

        if d_noop <= 1 and d_noop <= d_best:               # opponent has not replied yet
            bad, last = 0, None
            continue
        if d_best <= 1:                                     # a reply is on the screen
            if last == best:
                return best
            last = best
            time.sleep(0.8)                                 # confirm once the animation is over
            continue
        last = None
        if my_move is not None and dist(scr, before_map) <= 1:   # our move never happened
            if retries < 2 and time.time() - t0 > 3:
                retries += 1
                print("  My move did not register, tapping again...")
                play_taps(my_move, geo)
                t0 = time.time()
            elif retries >= 2:
                print(f"  Please make my move yourself on the phone: {my_move.uci()}")
                time.sleep(4)
            continue
        bad += 1
        if bad >= 5:
            try:
                screenshot().save("stuck.png")
                print("  (saved screenshot to stuck.png)")
            except Exception:
                pass
            print("  I cannot recognise the board. Look at the phone: what did the computer play?")
            txt = input("  Type it like g8e7 or Nxe7 (Enter = look again): ").strip()
            bad = 0
            if txt:
                for parse in (board.parse_san, board.parse_uci):
                    try:
                        mv = parse(txt)
                        if mv in board.legal_moves:
                            return mv
                    except ValueError:
                        pass
                print("  That is not a legal move here.")
    return None


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
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    path = find_engine(args.engine)
    if not path:
        print("Stockfish not found. Use --engine \"C:\\path\\stockfish.exe\""); sys.exit(1)
    geo = read_geometry(args.black)
    if geo is None:
        print("Could not find the chess board on the phone screen. Open the game board first."); sys.exit(1)
    our = chess.WHITE if geo.normal else chess.BLACK
    print(f"Board at x={geo.x0:.0f}, y={geo.y0:.0f}, square={geo.s:.0f}px | "
          f"You play {'White' if our else 'Black'} (bottom of screen)")

    reader = Reader(geo)
    if not reader.calibrate(screenshot()):
        print("WARNING: the board does not look like a fresh starting position.")
        print("Press the restart button in the game first.")
        if input("Continue anyway? (y/n): ").strip().lower() != "y":
            sys.exit(0)
    elif args.debug:
        print(f"Calibrated: white/black brightness split at {reader.lum_thr:.0f}")

    board = chess.Board()
    engine = chess.engine.SimpleEngine.popen_uci(path)
    try:
        engine.configure({"Skill Level": max(0, min(20, args.skill))})
    except Exception:
        pass

    try:
        while not board.is_game_over():
            before_map, mv = bmap(board), None
            if board.turn == our:
                mv = engine.play(board, chess.engine.Limit(time=args.time)).move
                print(f"My move: {board.san(mv)} ({mv.uci()})")
                if args.confirm and input("  Play it? (y/n): ").strip().lower() != "y":
                    print("Stopped."); break
                play_taps(mv, geo)
                board.push(mv)
                if mv.promotion:
                    input("  Promotion: choose the Queen on the phone, then press Enter...")
                if board.is_game_over():
                    break
            omv = wait_for_screen(board, reader, before_map, mv, geo, args.debug)
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