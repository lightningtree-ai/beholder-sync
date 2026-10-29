"""Try beholder-sync.html by hand against a mock datalab, with no real server.

    python3 demo.py

It serves the page at http://localhost:8932/beholder-sync.html, runs the
test's mock datalab at http://127.0.0.1:8931, and fills ~/Desktop/beholder-demo
with cycler and EIS files. Some file names are awkward on purpose. The config
to paste is written next to them. Uploads only reach the mock, which forgets
them when the script stops.

While it runs, type a letter and Enter:
    f   uploads fail with HTTP 500, or work again (the "stuck" state)
    d   datalab stops answering, or answers again
    q   quit
"""

import subprocess
from functools import partial
from http.server import SimpleHTTPRequestHandler
from pathlib import Path

from test_sync import HERE, MOCK, ApiHandler, serve

API_PORT, PAGE_PORT = 8931, 8932
FOLDER = Path.home() / "Desktop" / "beholder-demo"

# The first four are real names from a cycler PC. The rest each break a
# naive item ID rule in one way.
FILES = {
    "cycler/CEL-9997_127.0.0.1-BTS85-253-1-3-12.ndax": "ID first",
    "cycler/253_CEL-9999-1_4_9.ndax": "ID after the channel number",
    "cycler/CEL-081-discharge-rate-test-4.4V_127.0.0.1-BTS85-253-1-1-13.ndax": "ID before a label",
    "cycler/CEL-9998_127.0.0.1-BTS85-253-1-3-11.ndax": "ID first",
    "cycler/2026-10/CEL-102_formation.ndax": "in a subfolder",
    "cycler/cel-082_rerun.ndax": "lower case",
    "cycler/CEL083_nodash.ndax": "no dash",
    "cycler/NMC811-003_a.ndax": "another naming scheme",
    "cycler/notes.txt": "not a cycler file",
    "eis/CEL-081_eis_25C.mpr": "EIS of a cycler cell",
    "eis/CEL-9997_eis.mpr": "EIS of a cycler cell",
    "eis/LFP-004_eis.mpr": "another naming scheme",
}

CONFIG = f"""\
version: 1
datalabs:
  - name: "demo"
    url: "http://127.0.0.1:{API_PORT}"
    api_key: "test-key"
watched_paths:
  - name: "cycler"
    path: "{(FOLDER / 'cycler').as_posix()}"
    datalab: "demo"
    item_type: "cells"
    include_patterns: ["*.ndax"]
    id_patterns: [".*(?P<item_id>CEL-[0-9]{{3,5}}).*"]
    collection_id_template: "DEMO"
    block_patterns: {{"*.ndax": "cycle"}}
  - name: "eis"
    path: "{(FOLDER / 'eis').as_posix()}"
    datalab: "demo"
    item_type: "cells"
    include_patterns: ["*.mpr"]
    id_patterns: [".*(?P<item_id>CEL-[0-9]{{3,5}}).*"]
    block_patterns: {{"*.mpr": "eis"}}
sync:
  metadata_interval: 1200
"""


class QuietPage(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


class DemoApi(ApiHandler):
    """The test's mock, printing each request that changes something."""

    def handle_any(self, method):
        before = set(MOCK.items)
        super().handle_any(method)
        if method == "GET" or MOCK.down:
            return
        path = self.path.removeprefix("/api")
        new = sorted(set(MOCK.items) - before)
        print(f"  datalab: {method} {path}" + (f"  created {', '.join(new)}" if new else ""))


def main():
    for rel, text in FILES.items():
        f = FOLDER / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"demo file: {text}\n")
    (FOLDER / "config.yaml").write_text(CONFIG)
    serve(DemoApi, API_PORT)
    serve(partial(QuietPage, directory=str(HERE)), PAGE_PORT)
    # On a Mac the config goes on the clipboard, so step 2 is a paste.
    try:
        subprocess.run(["pbcopy"], input=CONFIG, text=True, check=True)
        where = "It is on your clipboard."
    except (OSError, subprocess.CalledProcessError):
        where = "Copy it from below, or from " + str(FOLDER / "config.yaml") + "."
    print(f"""
--- config.yaml for the demo
{CONFIG}---
Demo files are in {FOLDER}.
1. Open http://localhost:{PAGE_PORT}/beholder-sync.html in Chrome or Edge.
2. Open Datalab settings and paste the text of the config above into the top box.
   {where} Then click Load config.
3. Click Show details on each row, then Choose folder: beholder-demo/cycler and beholder-demo/eis.
4. Open Edit patterns on the cycler row and try the example box and the preview.

While this runs, type a letter here and press Enter:
  f   datalab refuses every upload, or accepts them again
  d   datalab stops answering, or answers again
  q   quit
""")
    while True:
        try:
            key = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        if key == "f":
            MOCK.fail_uploads = not MOCK.fail_uploads
            print("uploads now " + ("fail with HTTP 500" if MOCK.fail_uploads else "work"))
        elif key == "d":
            MOCK.down = not MOCK.down
            print("datalab now " + ("down" if MOCK.down else "up"))
        elif key == "q":
            break


if __name__ == "__main__":
    main()
