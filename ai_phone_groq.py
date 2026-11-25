"""
AI Phone Agent (Groq + ADB) - FREE, no vision model needed.

How it works:
  1. Dumps the phone's screen as an XML UI tree (uiautomator)
  2. Sends a numbered list of on-screen elements to a Groq text model
  3. The model picks an action (tap element #12, type text, scroll, ...)
  4. The script runs it through ADB and repeats until the task is done

Setup (PowerShell):
  pip install groq
  setx GROQ_API_KEY "gsk_your_key_here"     (then reopen PowerShell)
  adb devices                               (phone must show 'device')

Run:
  python ai_phone_groq.py
  python ai_phone_groq.py "Open Chrome and search weather in Indore"
  python ai_phone_groq.py --confirm         (asks before every action)

Stop anytime: Ctrl+C
"""
import os, sys, re, json, time, argparse, subprocess
import xml.etree.ElementTree as ET
from groq import Groq

MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
MAX_STEPS = 40
MAX_ELEMENTS = 70
SERIAL = os.environ.get("ANDROID_SERIAL")  # set if multiple devices

client = Groq(api_key=os.environ["GROQ_API_KEY"])


# ---------------------------------------------------------------- ADB helpers
def adb(*args, timeout=30):
    cmd = ["adb"] + (["-s", SERIAL] if SERIAL else []) + list(args)
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return r.stdout.decode("utf-8", errors="ignore")


def check_device():
    out = adb("devices")
    lines = [l for l in out.splitlines()[1:] if l.strip()]
    ok = [l for l in lines if l.endswith("device")]
    if not ok:
        print("No phone found. Output of 'adb devices':\n", out)
        print("Fix: enable USB debugging and accept the 'Allow' popup on the phone.")
        sys.exit(1)


def screen_size():
    out = adb("shell", "wm", "size").strip().splitlines()[-1]
    w, h = out.split(":")[-1].strip().split("x")
    return int(w), int(h)


def installed_apps():
    out = adb("shell", "pm", "list", "packages", "-3")
    return [l.replace("package:", "").strip() for l in out.splitlines() if l.strip()]


# ------------------------------------------------------------------- UI tree
BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def dump_ui():
    adb("shell", "uiautomator", "dump", "/sdcard/ui.xml")
    xml_text = adb("shell", "cat", "/sdcard/ui.xml")
    start = xml_text.find("<?xml")
    if start == -1:
        start = xml_text.find("<hierarchy")
    if start == -1:
        return None
    return xml_text[start:]


def parse_elements(xml_text):
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return [], ""
    elements, seen, pkg = [], set(), ""
    for n in root.iter("node"):
        pkg = pkg or n.get("package", "")
        text = n.get("text", "").strip()
        desc = n.get("content-desc", "").strip()
        rid = n.get("resource-id", "").split("/")[-1]
        cls = n.get("class", "").split(".")[-1]
        clickable = n.get("clickable") == "true" or n.get("long-clickable") == "true"
        editable = "EditText" in cls
        scrollable = n.get("scrollable") == "true"
        if not (text or desc or clickable or editable):
            continue
        m = BOUNDS.match(n.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        if x2 <= x1 or y2 <= y1:
            continue
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        key = (text, desc, cx, cy)
        if key in seen:
            continue
        seen.add(key)
        flags = []
        if clickable: flags.append("clickable")
        if editable: flags.append("input")
        if scrollable: flags.append("scrollable")
        elements.append({"text": text, "desc": desc, "id": rid, "cls": cls,
                         "flags": flags, "cx": cx, "cy": cy})
    return elements[:MAX_ELEMENTS], pkg


def describe(elements):
    lines = []
    for i, e in enumerate(elements):
        label = e["text"] or e["desc"] or e["id"] or e["cls"]
        extra = f" (desc: {e['desc']})" if e["text"] and e["desc"] else ""
        lines.append(f"[{i}] {label[:80]}{extra} | {e['cls']} | {','.join(e['flags'])}")
    return "\n".join(lines) if lines else "(no elements found)"


# ------------------------------------------------------------------ actions
KEYS = {"home": 3, "back": 4, "enter": 66, "recents": 187, "delete": 67,
        "power": 26, "volume_up": 24, "volume_down": 25, "search": 84}


def adb_text(text):
    out = text.replace(" ", "%s")
    out = re.sub(r"([\\\"'`&|;<>()$*?!#~\[\]{}])", r"\\\1", out)
    adb("shell", "input", "text", out)


def execute(cmd, elements, W, H):
    a = cmd.get("action")

    def el():
        i = int(cmd["id"])
        return elements[i]

    if a == "tap":
        e = el(); adb("shell", "input", "tap", str(e["cx"]), str(e["cy"]))
    elif a == "long_press":
        e = el(); adb("shell", "input", "swipe", *[str(v) for v in (e["cx"], e["cy"], e["cx"], e["cy"], 800)])
    elif a == "type":
        if "id" in cmd and cmd["id"] is not None:
            e = el(); adb("shell", "input", "tap", str(e["cx"]), str(e["cy"])); time.sleep(0.6)
        adb_text(str(cmd["text"]))
        if cmd.get("submit"):
            time.sleep(0.4); adb("shell", "input", "keyevent", "66")
    elif a == "scroll_down":   # content moves up, see lower content
        adb("shell", "input", "swipe", str(W // 2), str(int(H * .7)), str(W // 2), str(int(H * .3)), "350")
    elif a == "scroll_up":
        adb("shell", "input", "swipe", str(W // 2), str(int(H * .3)), str(W // 2), str(int(H * .7)), "350")
    elif a == "swipe_left":
        adb("shell", "input", "swipe", str(int(W * .8)), str(H // 2), str(int(W * .2)), str(H // 2), "300")
    elif a == "swipe_right":
        adb("shell", "input", "swipe", str(int(W * .2)), str(H // 2), str(int(W * .8)), str(H // 2), "300")
    elif a == "key":
        adb("shell", "input", "keyevent", str(KEYS.get(cmd.get("name", "enter"), 66)))
    elif a == "open_app":
        adb("shell", "monkey", "-p", cmd["package"], "-c", "android.intent.category.LAUNCHER", "1")
    elif a == "wait":
        time.sleep(min(float(cmd.get("seconds", 2)), 10))
    else:
        print("Unknown action:", cmd)


# --------------------------------------------------------------------- model
SYSTEM = """You control an Android phone. Each turn you get the user's task, the
current screen as a numbered element list, and your recent actions.
Reply with ONLY one JSON object (single next action):

{"action":"tap","id":3}
{"action":"long_press","id":3}
{"action":"type","text":"hello","id":5,"submit":true}   (id = input field to tap first; submit presses Enter)
{"action":"scroll_down"}  {"action":"scroll_up"}  {"action":"swipe_left"}  {"action":"swipe_right"}
{"action":"key","name":"back"}     (home, back, enter, recents, delete, search)
{"action":"open_app","package":"com.android.chrome"}
{"action":"wait","seconds":2}
{"action":"done","reason":"short summary"}

Rules:
- Use element ids from the CURRENT list only. Ids change every screen.
- Add a short "thought" field explaining your choice.
- Do not repeat a failed action; try another approach (scroll, back, open_app).
- If the screen list is empty or useless, try wait, back, or open_app.
- Return done as soon as the task is complete."""


def ask(messages):
    for attempt in range(6):
        try:
            r = client.chat.completions.create(
                model=MODEL, messages=messages, temperature=0.2,
                response_format={"type": "json_object"})
            return json.loads(r.choices[0].message.content)
        except Exception as e:
            msg = str(e)
            wait = 8 * (attempt + 1) if ("429" in msg or "rate" in msg.lower()) else 2
            print(f"  (model error, retrying in {wait}s: {msg[:100]})")
            time.sleep(wait)
    raise RuntimeError("Groq kept failing. Check API key / model name / rate limit.")


def run_task(task, W, H, apps, confirm):
    history = []
    for step in range(1, MAX_STEPS + 1):
        xml_text = dump_ui()
        if not xml_text:
            print("  UI dump failed, waiting..."); time.sleep(2); continue
        elements, pkg = parse_elements(xml_text)

        user = (f"TASK: {task}\n\nCurrent app package: {pkg}\n"
                f"Installed user apps (package names): {', '.join(apps[:60])}\n\n"
                f"SCREEN ELEMENTS:\n{describe(elements)}\n\n"
                f"RECENT ACTIONS: {json.dumps(history[-8:], ensure_ascii=False)}")
        cmd = ask([{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}])
        print(f"[{step}] {cmd}")

        if cmd.get("action") == "done":
            print("Finished:", cmd.get("reason", "")); return
        if confirm and input("   Run this? (y/n): ").strip().lower() != "y":
            print("Stopped."); return
        try:
            execute(cmd, elements, W, H)
        except (KeyError, IndexError, ValueError) as e:
            print("  Bad action from model:", e)
            history.append({"failed": cmd, "error": str(e)})
            continue
        history.append(cmd)
        time.sleep(1.5)
    print("Max steps reached.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task", nargs="?")
    ap.add_argument("--confirm", action="store_true", help="ask before every action")
    args = ap.parse_args()

    check_device()
    W, H = screen_size()
    apps = installed_apps()
    print(f"Phone {W}x{H} | model: {MODEL} | apps found: {len(apps)}")

    if args.task:
        run_task(args.task, W, H, apps, args.confirm); return
    while True:
        t = input("\nTask for phone (or 'exit'): ").strip()
        if t.lower() == "exit": break
        if t: run_task(t, W, H, apps, args.confirm)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user.")
