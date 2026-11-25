"""
chess_probe.py - dumps EVERY element on the phone screen into chess_dump.txt
Open the chess game board on the phone first, then run:
    python chess_probe.py
Then paste the first 40-50 lines of chess_dump.txt in the chat.
"""
import re, subprocess
import xml.etree.ElementTree as ET


def adb(*args):
    r = subprocess.run(["adb"] + list(args), capture_output=True)
    return r.stdout.decode("utf-8", errors="ignore")


adb("shell", "uiautomator", "dump", "/sdcard/ui.xml")
xml_text = adb("shell", "cat", "/sdcard/ui.xml")
start = xml_text.find("<?xml")
if start == -1:
    start = xml_text.find("<hierarchy")
if start == -1:
    print("UI dump failed. Is the phone connected and unlocked?")
    raise SystemExit(1)

root = ET.fromstring(xml_text[start:])
lines = []
for n in root.iter("node"):
    text = n.get("text", "")
    desc = n.get("content-desc", "")
    rid = n.get("resource-id", "").split("/")[-1]
    cls = n.get("class", "").split(".")[-1]
    bounds = n.get("bounds", "")
    click = "C" if n.get("clickable") == "true" else "-"
    # keep everything that has any label, or is clickable
    if text or desc or click == "C":
        lines.append(f"{bounds} | {click} | text={text!r} | desc={desc!r} | id={rid} | {cls}")

with open("chess_dump.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(lines))

print(f"Saved {len(lines)} elements to chess_dump.txt")
print("\n".join(lines[:15]))
