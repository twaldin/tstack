#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

root = Path(os.environ["SEAT_WAITS_FAKE"])
name = Path(sys.argv[0]).name
args = sys.argv[1:]
record = {"pid": os.getpid(), "command": name, "args": args,
          "cwd": os.getcwd(), "seat": os.environ.get("TSTACK_SEAT")}
fd = os.open(root / "calls.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
os.write(fd, (json.dumps(record) + "\n").encode())
os.close(fd)
if name == "easl" and args[:2] == ["ask", "list"]:
    failure = root / "ask-list-error.json"
    if failure.exists():
        result = json.loads(failure.read_text())
        sys.stderr.write(result["stderr"])
        sys.exit(result["code"])
    print((root / "asks.json").read_text())
    sys.exit(0)
if name == "watch-pr" and "--once" in args:
    snapshots = json.loads((root / "prs.json").read_text())
    print(json.dumps(snapshots.get(args[0], {"state": "OPEN"})))
    sys.exit(0)
while True:
    response = root / f"{os.getpid()}.json"
    if response.exists():
        result = json.loads(response.read_text())
        sys.stdout.write(result.get("stdout", ""))
        sys.stderr.write(result.get("stderr", ""))
        sys.exit(result.get("code", 0))
    if name == "text":
        messages = json.loads((root / "messages.json").read_text())
        cursor = root / "text.cursor"
        offset = int(cursor.read_text()) if cursor.exists() else 0
        if len(messages) > offset:
            cursor.write_text(str(len(messages)))
            print("\n".join(messages[offset:]))
            sys.exit(0)
    time.sleep(0.01)
