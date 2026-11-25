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
Options:  --time 1.0  --skill 10  --black  --debug  --board x0,y0,size
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


# ------------------------------------------------------------------ finding the board (screenshot)

def detect_board(shot):
    """Finds the board in a screenshot: 8 columns across the full screen width with the
    light/dark squares alternating. Returns (x0, y0, square_size) or None."""
    W, H = shot.size
    s = W / 8.0
    px = shot.load()

    def close(a, b, t):
        return max(abs(a[i] - b[i]) for i in range(3)) <= t

    ok = []
    for y in range(H):
        pts = [px[int(s * k + 0.07 * s), y][:3] for k in range(8)]
        ev = [pts[k] for k in (0, 2, 4, 6)]
        od = [pts[k] for k in (1, 3, 5, 7)]
        A = tuple(sorted(c[i] for c in ev)[1] for i in range(3))
        B = tuple(sorted(c[i] for c in od)[1] for i in range(3))
        match = sum(close(c, A, 14) for c in ev) + sum(close(c, B, 14) for c in od)
        ok.append(match >= 6 and not close(A, B, 25))
    best, run_start, gap = None, None, 0
    for y in range(H + 1):
        good = y < H and ok[y]
        if good:
            if run_start is None:
                run_start = y
            gap = 0
        elif run_start is not None:
            gap += 1
            if gap > 8 or y >= H:
                end = y - gap + 1
                if best is None or end - run_start > best[1] - best[0]:
                    best = (run_start, end)
                run_start, gap = None, 0
    if best and abs((best[1] - best[0]) - 8 * s) <= 0.08 * 8 * s:
        return 0.0, float(best[0]), s
    return None



def _longest_run(values, thr, gap=10):
    best, start, miss = None, None, 0
    n = len(values)
    for i in range(n + 1):
        good = i < n and values[i] > thr
        if good:
            if start is None:
                start = i
            miss = 0
        elif start is not None:
            miss += 1
            if miss > gap or i >= n:
                end = i - miss + 1
                if best is None or end - start > best[1] - best[0]:
                    best = (start, end)
                start, miss = None, 0
    return best


def detect_board_flex(shot):
    """Finds a board of any size / position: looks for the two most common flat colors
    that fill a square region. Returns (x0, y0, square_size) or None."""
    W, H = shot.size
    small = shot.resize((max(1, W // 4), max(1, H // 4)), Image.NEAREST)
    counted = sorted(small.getcolors(maxcolors=2000000) or [], reverse=True)[:8]
    cols = [c[:3] for _, c in counted]
    masks = []
    for C in cols:
        d = ImageChops.difference(shot, Image.new("RGB", shot.size, C))
        r, g, b = d.split()
        m = ImageChops.lighter(ImageChops.lighter(r, g), b).point(lambda v: 255 if v <= 12 else 0)
        masks.append(m)
    best = None
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            if max(abs(cols[i][k] - cols[j][k]) for k in range(3)) < 20:
                continue
            mask = ImageChops.lighter(masks[i], masks[j])
            rows = list(mask.resize((1, H), Image.BOX).getdata())
            ry = _longest_run(rows, 0.25 * 255)
            if not ry:
                continue
            y0, y1 = ry
            band = mask.crop((0, y0, W, y1))
            colsd = list(band.resize((W, 1), Image.BOX).getdata())
            rx = _longest_run(colsd, 0.35 * 255)
            if not rx:
                continue
            x0, x1 = rx
            L, Lx = y1 - y0, x1 - x0
            if L >= 0.4 * W and abs(L - Lx) <= 0.06 * L:
                if best is None or L > best[0]:
                    best = (L, x0, y0)
    if best is None:
        return None
    L, x0, y0 = best
    return float(x0), float(y0), L / 8.0


def detect_orientation(shot, x0, y0, s):
    """True if White is at the bottom (white pieces are brighter than black pieces)."""
    reader = Reader(Geometry(x0, y0, s, True))
    bottom, top = [], []
    for sq in chess.SQUARES:
        r = chess.square_rank(sq)
        if r in (0, 1, 6, 7):
            frac, lum = reader.stats(shot, sq)
            if frac > reader.occ_thr and lum is not None:
                (bottom if r in (0, 1) else top).append(lum)
    if not bottom or not top:
        return True
    return sum(bottom) / len(bottom) > sum(top) / len(top)


def save_overlay(shot, geo, name="board_check.png"):
    from PIL import ImageDraw
    im = shot.copy()
    d = ImageDraw.Draw(im)
    for sq in chess.SQUARES:
        left, top = geo.cell(sq)
        d.rectangle([left, top, left + geo.s, top + geo.s], outline=(255, 0, 0), width=2)
        d.text((left + 6, top + geo.s - 18), chess.square_name(sq), fill=(255, 0, 0))
    im.save(name)


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
    ap.add_argument("--board", help="x0,y0,size of the board in pixels (if auto-detect fails)")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    path = find_engine(args.engine)
    if not path:
        print("Stockfish not found. Use --engine \"C:\\path\\stockfish.exe\""); sys.exit(1)
    geo = read_geometry(args.black)
    if geo is None:                                   # board not in the UI tree: use the screenshot
        shot = screenshot()
        if args.board:
            x0, y0, sz = [float(v) for v in args.board.split(",")]
            det = (x0, y0, sz)
        else:
            det = detect_board(shot) or detect_board_flex(shot)
        if det is None:
            print("Could not find the chess board on the screen. Open the game board (fresh game),")
            print("or give it by hand: --board x0,y0,size   (top-left corner and square size in pixels).")
            shot.save("stuck.png"); print("(saved the current screen to stuck.png)"); sys.exit(1)
        normal = detect_orientation(shot, *det) and not args.black
        geo = Geometry(det[0], det[1], det[2], normal)
        save_overlay(shot, geo)
        print("Board found from the screenshot; grid saved to board_check.png")
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