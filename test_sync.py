"""End-to-end test of beholder-sync.html against a mock datalab.

The page is served from one port and the mock API from another, so every
request crosses origins exactly as it would against the real server. The
watched folder is built in the browser's origin-private file system, whose
handles have the same interface as a folder the user picks.

    uv run --with playwright python test_sync.py

Run `playwright install chromium` once first, or set CHROMIUM_PATH.
"""

import hashlib
import json
import os
import re
import threading
from email.parser import BytesParser
from email.policy import HTTP
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler, BaseHTTPRequestHandler
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent


class MockDatalab:
    def __init__(self):
        self.reset()

    def reset(self):
        self.items = {}
        self.collections = {}
        self.calls = []
        self.down = False
        self.fail_uploads = False
        self.n = 0

    def new_id(self, prefix):
        self.n += 1
        return f"{prefix}{self.n:04d}"


MOCK = MockDatalab()


class ApiHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, DATALAB-API-KEY")

    def reply(self, status, body=None):
        self.send_response(status)
        self.cors()
        data = b"" if body is None else json.dumps(body).encode()
        if data:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.reply(200)

    def handle_any(self, method):
        if MOCK.down:
            self.close_connection = True
            self.connection.close()
            return
        # Like the real deployment: the UI is served at the root with no CORS
        # headers, and the API lives under /api.
        if not self.path.startswith("/api/"):
            body = b"<!doctype html><title>datalab</title>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        path = self.path.removeprefix("/api")
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        MOCK.calls.append((method, path))
        # /info answers without a key, as on the real server.
        if method == "GET" and path == "/info":
            return self.reply(200, {"data": {}})
        if self.headers.get("DATALAB-API-KEY") != "test-key":
            return self.reply(401, {"status": "error", "message": "bad key"})

        if m := re.fullmatch(r"/get-item-data/(.+)", path):
            item = MOCK.items.get(m[1])
            if not item:
                return self.reply(404, {"status": "error", "message": "not found"})
            return self.reply(200, {"item_data": item})

        if m := re.fullmatch(r"/collections/(.+)", path):
            c = MOCK.collections.get(m[1])
            if not c:
                return self.reply(404, {"status": "error"})
            return self.reply(200, {"data": c, "child_items": []})

        if method == "PUT" and path == "/collections":
            cid = json.loads(raw)["data"]["collection_id"]
            MOCK.collections[cid] = {"collection_id": cid, "immutable_id": MOCK.new_id("col")}
            return self.reply(201, {"data": MOCK.collections[cid]})

        if path == "/new-sample/":
            body = json.loads(raw)
            s = body["new_sample_data"]
            if s["item_id"] in MOCK.items:
                return self.reply(409, {"status": "error", "message": "duplicate"})
            MOCK.items[s["item_id"]] = {
                "item_id": s["item_id"], "type": s["type"],
                "collections": s.get("collections", []),
                "files": [], "file_ObjectIds": [], "blocks_obj": {}, "display_order": [],
            }
            return self.reply(201, {"sample_list_entry": {"item_id": s["item_id"]}})

        if path == "/upload-file/":
            if MOCK.fail_uploads:
                return self.reply(500, {"status": "error", "message": "disk full"})
            msg = BytesParser(policy=HTTP).parsebytes(
                f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode() + raw)
            fields = {p.get_param("name", header="content-disposition"): p for p in msg.iter_parts()}
            item = MOCK.items[fields["item_id"].get_content().strip()]
            content = fields["file"].get_payload(decode=True)
            digest = hashlib.sha256(content).hexdigest()
            name = fields["file"].get_filename()
            replace = fields["replace_file"].get_content().strip() if "replace_file" in fields else None
            if replace:
                f = next(f for f in item["files"] if f["immutable_id"] == replace)
                if f["hash"] == digest:
                    return self.reply(304)
                f.update(hash=digest, size=len(content))
                return self.reply(201, {"file_id": replace, "is_update": True})
            fid = MOCK.new_id("file")
            item["files"].append({"immutable_id": fid, "original_name": name,
                                  "name": name.replace(" ", "_"), "hash": digest, "size": len(content)})
            item["file_ObjectIds"].append(fid)
            return self.reply(201, {"file_id": fid, "is_update": False})

        if path == "/add-data-block/":
            body = json.loads(raw)
            item = MOCK.items[body["item_id"]]
            bid = MOCK.new_id("blk")
            block = {"block_id": bid, "blocktype": body["block_type"], "item_id": body["item_id"]}
            item["blocks_obj"][bid] = block
            item["display_order"].append(bid)
            return self.reply(200, {"new_block_obj": block})

        if path == "/update-block/":
            body = json.loads(raw)
            b = body["block_data"]
            MOCK.items[b["item_id"]]["blocks_obj"][b["block_id"]].update(file_id=b["file_id"])
            return self.reply(200, {"new_block_data": b})

        self.reply(404, {"status": "error", "message": f"no route {method} {path}"})

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def do_PUT(self):
        self.handle_any("PUT")


def serve(handler, port=0):
    srv = HTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


CONFIG_YAML = """\
version: 1
datalabs:
  - name: "mock"
    url: "{base}"
    api_key: "test-key"
watched_paths:
  - kind: "local"
    path: "C://Users/Cycler/Desktop/data"
    name: "sycamore"
    datalab: "mock"
    item_type: "cells"
    include_patterns:
      - "*.ndax"
    exclude_patterns: []
    id_patterns:
      - ".*(?P<item_id>CEL-[0-9]{{3,5}}).*"
    item_id_template: "{{item_id}}"
    collection_id_template: "APC-P2"
    block_patterns:
       "*.ndax": "cycle"
  - kind: "local"
    path: "D:/other"
    name: "other"
    datalab: "mock"
    include_patterns: ["*.mpr"]
sync:
  metadata_interval: 1200  # seconds (20 minutes)
"""

WRITE_FILES = """async ([files, remove]) => {
  const root = await navigator.storage.getDirectory();
  for (const p of remove) {
    const parts = p.split('/'); let d = root;
    for (const part of parts.slice(0, -1)) d = await d.getDirectoryHandle(part);
    await d.removeEntry(parts.at(-1));
  }
  for (const [p, text] of Object.entries(files)) {
    const parts = p.split('/'); let d = root;
    for (const part of parts.slice(0, -1)) d = await d.getDirectoryHandle(part, {create: true});
    const fh = await d.getFileHandle(parts.at(-1), {create: true});
    const w = await fh.createWritable(); await w.write(text); await w.close();
  }
}"""


def main():
    api = serve(ApiHandler)
    page_srv = serve(partial(SimpleHTTPRequestHandler, directory=str(HERE)))
    api_url = f"http://127.0.0.1:{api.server_port}/api"
    page_url = f"http://localhost:{page_srv.server_port}/beholder-sync.html"
    # PAGE_FROM_DISK=1 opens the page as a file, the way a collaborator would after a download.
    if os.environ.get("PAGE_FROM_DISK"):
        page_url = (HERE / "beholder-sync.html").resolve().as_uri()
    failures = []

    def check(name, cond, detail=""):
        print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
        if not cond:
            failures.append(name)

    def uploads():
        return [c for c in MOCK.calls if c[1] == "/upload-file/"]

    with sync_playwright() as p:
        # CHROMIUM_PATH reuses a browser already on disk instead of a fresh download.
        browser = p.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH"))
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(page_url)

        # 0. Settings come from a pasted config.yaml. Its url is the UI
        # address, as in a real config, so the page has to find /api itself.
        # The second watched path proves the picker loads the one chosen.
        page.fill("#yaml", CONFIG_YAML.format(base=api_url.removesuffix("/api")))
        page.click("#loadYaml")
        page.wait_for_function("document.querySelector('#apiUrl').value.endsWith('/api')")
        check("picker lists both watched paths", page.locator("#pathName option").count() == 2)
        page.select_option("#pathName", "other")
        page.click("#loadYaml")
        page.wait_for_function("document.querySelector('#include').value === '*.mpr'")
        page.select_option("#pathName", "sycamore")
        page.click("#loadYaml")
        page.wait_for_function("document.querySelector('#include').value === '*.ndax'")
        got = {k: page.input_value(f"#{k}") for k in
               ["apiUrl", "apiKey", "itemType", "intervalMin", "idPattern", "itemTemplate",
                "collectionTemplate", "blockPatterns", "maxDepth"]}
        check("config fills every box", got == {
            "apiUrl": api_url, "apiKey": "test-key", "itemType": "cells", "intervalMin": "20",
            "idPattern": ".*(?P<item_id>CEL-[0-9]{3,5}).*", "itemTemplate": "{item_id}",
            "collectionTemplate": "APC-P2", "blockPatterns": "*.ndax=cycle", "maxDepth": "10",
        }, got)

        # BEHOLDER_CONFIG=path/to/config.yaml checks that a real config maps
        # too. Only the mapping runs, so no request reaches its server.
        if real := os.environ.get("BEHOLDER_CONFIG"):
            mapped = page.evaluate(
                "(t) => beholder.settingsFromConfig(jsyaml.load(t)).s",
                Path(real).read_text(errors="replace"))
            check("BEHOLDER_CONFIG maps", bool(mapped["apiUrl"]) and bool(mapped["idPattern"]),
                  {k: v for k, v in mapped.items() if k != "apiKey"})

        def write(files, remove=()):
            page.evaluate(WRITE_FILES, [files, list(remove)])

        def sync():
            MOCK.calls.clear()
            page.evaluate("async () => { await beholder.useFolder(await navigator.storage.getDirectory()) }")

        def tiles():
            return dict(zip(page.locator(".tile .l").all_text_contents()[:4],
                            page.locator(".tile .n").all_inner_texts()[:4]))

        def pill():
            return page.inner_text("#state")

        def rows():
            return {r["path"]: r for r in page.evaluate("beholder.filesAll()")}

        # 1. First sync: create items and collection, upload, add blocks.
        write({
            "CEL-001 run a.ndax": "a1",
            "sub/CEL-001_b.ndax": "b1",
            "deep/er/CEL-0002/test.ndax": "c1",
            "notes.txt": "ignored, wrong extension",
            "no-id.ndax": "ignored, no item id",
        })
        sync()
        check("creates both items", set(MOCK.items) == {"CEL-001", "CEL-0002"}, list(MOCK.items))
        check("creates the collection once", list(MOCK.collections) == ["APC-P2"])
        check("links items to the collection",
              all(i["collections"] for i in MOCK.items.values()))
        check("item type is cells", all(i["type"] == "cells" for i in MOCK.items.values()))
        check("uploads three files", len(uploads()) == 3, len(uploads()))
        check("CEL-001 has two files", len(MOCK.items["CEL-001"]["files"]) == 2)
        blocks = [b for i in MOCK.items.values() for b in i["blocks_obj"].values()]
        check("one cycle block per file, each wired to its file",
              len(blocks) == 3 and all(b["blocktype"] == "cycle" and b.get("file_id") for b in blocks),
              blocks)
        check("ignores non-matching files", set(rows()) == {
            "CEL-001 run a.ndax", "sub/CEL-001_b.ndax", "deep/er/CEL-0002/test.ndax"}, list(rows()))
        check("all rows synced", all(r["syncedSize"] == r["size"] for r in rows().values()))
        check("status: tiles count synced, pending, stuck, no item_id",
              tiles() == {"synced": "3", "pending": "0", "stuck": "0", "no item_id": "1"}, tiles())
        check("status: pill says running", pill() == "running", pill())
        check("status: recently uploaded lists three files",
              page.locator("text=Recently uploaded").count() == 1
              and page.locator("#body table").last.locator("tr").count() == 4)
        check("status: no-item_id note explains itself", "never upload" in page.inner_text("#body"))

        # 2. Nothing changed: no uploads, no server lookups beyond nothing.
        sync()
        check("unchanged folder uploads nothing", uploads() == [], uploads())
        check("unchanged folder makes no requests", MOCK.calls == [], MOCK.calls)

        # 3. A file grows: replace in place, keep its id, no second block.
        def file_a():
            return next(f for f in MOCK.items["CEL-001"]["files"]
                        if f["original_name"] == "CEL-001 run a.ndax")
        before_id = file_a()["immutable_id"]
        write({"CEL-001 run a.ndax": "a1 plus more cycles"})
        sync()
        check("modified file is uploaded once", len(uploads()) == 1, uploads())
        check("modified file replaces in place",
              len(MOCK.items["CEL-001"]["files"]) == 2 and file_a()["immutable_id"] == before_id
              and file_a()["size"] == len("a1 plus more cycles"))
        check("no duplicate block after replace",
              len(MOCK.items["CEL-001"]["blocks_obj"]) == 2)

        # 4. Touched but identical content: the server answers 304, row ends synced.
        write({"sub/CEL-001_b.ndax": "b1"})
        sync()
        r = rows()["sub/CEL-001_b.ndax"]
        check("304 counts as synced", r["syncedMtime"] == r["mtime"] and not r.get("lastError"), r)

        # 5. Server down: file stays pending without spending an attempt.
        write({"deep/er/CEL-0002/test.ndax": "c2"})
        MOCK.down = True
        sync()
        MOCK.down = False
        r = rows()["deep/er/CEL-0002/test.ndax"]
        check("outage leaves file pending", r["syncedMtime"] != r["mtime"], r)
        check("outage costs no attempt", r["attempts"] == 0, r)
        check("status: outage shows a banner and a red pill",
              pill() == "datalab unreachable" and "did not answer" in page.inner_text("#banners"),
              (pill(), page.inner_text("#banners")))

        # 6. Upload error: attempt counted, backoff set, retried only after it.
        MOCK.fail_uploads = True
        sync()
        r = rows()["deep/er/CEL-0002/test.ndax"]
        check("upload error counts an attempt", r["attempts"] == 1 and "500" in r["lastError"], r)
        MOCK.fail_uploads = False
        sync()
        check("backoff holds the retry", uploads() == [], uploads())
        check("status: stuck file is listed with its error",
              pill() == "1 stuck" and tiles()["stuck"] == "1"
              and "deep/er/CEL-0002/test.ndax" in page.inner_text("text=Not uploading >> xpath=following-sibling::div[1]")
              and "HTTP 500" in page.inner_text("#body") and page.inner_text("#banners") == "",
              (pill(), tiles(), page.inner_text("#body")))
        if os.environ.get("SHOT"):  # a picture of the status card with one file stuck
            page.screenshot(path=os.environ["SHOT"], full_page=True)
        # Sync now clears the backoff, as the dashboard's button does.
        MOCK.calls.clear()
        page.click("#syncNow")
        # The click returns before the sync starts, so wait for the upload
        # to reach the mock, then for the button to come back.
        for _ in range(100):
            if uploads():
                break
            page.wait_for_timeout(50)
        page.wait_for_function("document.querySelector('#syncNow').textContent === 'Sync now'"
                               " && !document.querySelector('#syncNow').disabled")
        r = rows()["deep/er/CEL-0002/test.ndax"]
        check("Sync now retries a file in backoff", len(uploads()) == 1 and r["syncedSize"] == r["size"], r)
        check("status: stuck clears after the retry", pill() == "running" and tiles()["stuck"] == "0",
              (pill(), tiles()))

        # 7. Deleted file drops out of the table.
        write({}, remove=["sub/CEL-001_b.ndax"])
        sync()
        check("deleted file is forgotten", "sub/CEL-001_b.ndax" not in rows())

        # 8. Matching semantics shared with the Python beholder.
        cases = page.evaluate("""() => {
          const cfg0 = {include: '*.ndax', exclude: '', idPattern: '.*(?P<item_id>CEL-[0-9]{3,5}).*',
            itemTemplate: '{item_id}', collectionTemplate: '', blockPatterns: '', maxDepth: 10, apiUrl: ''};
          const cfg = beholder.compileSettings(cfg0);
          return {
            upper: beholder.globToRegex('*.ndax').test('X.NDAX'),
            bracket: beholder.globToRegex('[!a]*.ndax').test('b.ndax') && !beholder.globToRegex('[!a]*.ndax').test('a.ndax'),
            lastRun: beholder.idsFor(cfg, 'CEL-001/CEL-0042.ndax')?.item_id,
            none: beholder.idsFor(cfg, 'CEL-1.ndax'),
            second: beholder.idsFor(beholder.compileSettings({...cfg0, idPattern: 'XX(?P<item_id>Z)\\n(?P<item_id>BAT-[0-9]+)'}), 'BAT-77.ndax')?.item_id,
            unlimited: beholder.compileSettings({...cfg0, maxDepth: ''}).maxDepth,
          };
        }""")
        check("globs ignore case", cases["upper"])
        check("glob [!a] negates", cases["bracket"])
        check("greedy regex takes the last ID in the path", cases["lastRun"] == "CEL-0042", cases)
        check("no match gives no item", cases["none"] is None, cases)
        check("second regex line is tried", cases["second"] == "BAT-77", cases)
        check("blank max depth is unlimited", cases["unlimited"] is None or cases["unlimited"] == float("inf"), cases)

        check("no page errors", not errors, errors)
        browser.close()

    print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED'}")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
