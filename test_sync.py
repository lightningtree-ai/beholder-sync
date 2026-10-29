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
            # Like datalab, which reads request.form["replace_file"] and
            # answers Werkzeug's default 400 when it is missing.
            if "replace_file" not in fields:
                return self.reply(400, {"message": "The browser (or proxy) sent a request "
                                                   "that this server could not understand."})
            replace = fields["replace_file"].get_content().strip()
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
  - name: "theirs"
    url: "{base}/theirs"
    api_key: "their-key"
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
    path: "D:/eis"
    name: "eis"
    datalab: "mock"
    item_type: "cells"
    include_patterns: ["*.mpr"]
    id_patterns: [".*(?P<item_id>CEL-[0-9]{{3,5}}).*"]
    item_id_template: "{{item_id}}"
    block_patterns: {{"*.mpr": "eis"}}
  - kind: "local"
    path: "E:/theirs"
    name: "their-path"
    datalab: "theirs"
    include_patterns: ["*.txt"]
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


# The database as version 1 of the page left it: flat settings and one
# synced file. It also held the folder handle, which this leaves out:
# headless Chromium closes the page that reads back a stored handle to the
# origin-private file system.
V1_SETUP = """async (apiUrl) => {
  const root = await navigator.storage.getDirectory();
  const fh = await root.getFileHandle('CEL-0009.ndax', {create: true});
  const w = await fh.createWritable(); await w.write('old'); await w.close();
  const f = await fh.getFile();
  const db = await new Promise((res, rej) => {
    const r = indexedDB.open('beholder-sync', 1);
    r.onupgradeneeded = () => {
      r.result.createObjectStore('kv');
      r.result.createObjectStore('files', {keyPath: 'path'});
    };
    r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
  });
  const t = db.transaction(['kv', 'files'], 'readwrite');
  t.objectStore('kv').put({apiUrl, apiKey: 'test-key', itemType: 'cells', intervalMin: 10,
    include: '*.ndax', exclude: '', idPattern: '.*(?P<item_id>CEL-[0-9]{3,5}).*',
    itemTemplate: '{item_id}', collectionTemplate: '', blockPatterns: '', maxDepth: 10}, 'settings');
  t.objectStore('kv').put('C:/x/data', 'configPath');
  t.objectStore('files').put({path: 'CEL-0009.ndax', size: f.size, mtime: f.lastModified,
    syncedSize: f.size, syncedMtime: f.lastModified, itemId: 'CEL-0009', collectionId: null,
    attempts: 0, nextTry: 0, lastSynced: Date.now()});
  await new Promise((res) => { t.oncomplete = res; });
  db.close();
}"""


def main():
    # Imported here so that demo.py can reuse the mock without Playwright.
    from playwright.sync_api import sync_playwright

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

        def card(name):
            return page.locator(f'.watch[data-name="{name}"]')

        def cards():
            return page.evaluate("[...document.querySelectorAll('.watch')].map((c) => c.dataset.name)")

        def wait_cards(names):
            page.wait_for_function(
                "(n) => [...document.querySelectorAll('.watch')].map((c) => c.dataset.name).join() === n",
                arg=",".join(names))

        def boxes():
            out = {k: page.input_value(f"#{k}") for k in ["apiUrl", "apiKey", "intervalMin"]}
            for k in ["itemType", "include", "idPattern", "itemTemplate", "collectionTemplate",
                      "blockPatterns", "maxDepth"]:
                out[k] = card("sycamore").locator(f"[name={k}]").input_value()
            return out

        # 0. Settings come from a pasted config.yaml. Its url is the UI
        # address, as in a real config, so the page has to find /api itself.
        # Every local path for the chosen datalab gets a card of its own.
        page.fill("#yaml", CONFIG_YAML.format(base=api_url.removesuffix("/api")))
        page.click("#loadYaml")
        page.wait_for_function("document.querySelector('#apiUrl').value.endsWith('/api')")
        check("datalab dropdown lists both datalabs",
              page.locator("#datalabName option").count() == 2 and page.is_visible("#datalabChoice"))
        wait_cards(["sycamore", "eis"])
        check("one card per watched path of the chosen datalab", cards() == ["sycamore", "eis"], cards())
        page.select_option("#datalabName", "theirs")
        page.click("#loadYaml")
        wait_cards(["their-path"])
        check("choosing their datalab loads its path and key",
              page.input_value("#apiKey") == "their-key", page.input_value("#apiKey"))
        page.select_option("#datalabName", "mock")
        page.click("#loadYaml")
        wait_cards(["sycamore", "eis"])
        check("switching back restores our key", page.input_value("#apiKey") == "test-key")
        got = boxes()
        check("config fills every box", got == {
            "apiUrl": api_url, "apiKey": "test-key", "itemType": "cells", "intervalMin": "20",
            "include": "*.ndax",
            "idPattern": ".*(?P<item_id>CEL-[0-9]{3,5}).*", "itemTemplate": "{item_id}",
            "collectionTemplate": "APC-P2", "blockPatterns": "*.ndax=cycle", "maxDepth": "10",
        }, got)
        page.fill("#apiUrl", "datalab.example.org")
        page.click("#save")
        page.wait_for_function("document.querySelector('#banners').innerText.includes('not saved')")
        check("Save settings refuses a URL without a scheme",
              page.evaluate("beholder.settings().apiUrl") == api_url, page.inner_text("#banners"))
        page.fill("#apiUrl", api_url)
        eis_rules = card("eis").locator(".rules").inner_text()
        check("each card shows its own rules", "*.mpr" in eis_rules and "*.mpr → eis" in eis_rules
              and "*.ndax" not in eis_rules, eis_rules)

        # A Windows path pasted into double quotes breaks the YAML. The rest
        # of the config still loads, and the banner names the line and the fix.
        good_yaml = CONFIG_YAML.format(base=api_url.removesuffix("/api"))
        bad_path = r'    path: "C:\\Users\SamuelAffleck\Documents"'
        bad_yaml = good_yaml.replace('    path: "C://Users/Cycler/Desktop/data"', bad_path)
        bad_line = bad_yaml.splitlines().index(bad_path) + 1
        page.fill("#yaml", bad_yaml)
        page.click("#loadYaml")
        page.wait_for_function("document.querySelector('#banners').innerText.includes('single quotes')")
        banner = page.inner_text("#banners")
        check("bad line names itself in the banner", f"line {bad_line} (unknown escape sequence)" in banner, banner)
        check("bad line leaves the other boxes filled", boxes() == got, boxes())
        lenient = page.evaluate("(t) => beholder.loadYamlLenient(t)",
                                good_yaml.replace("C://Users/Cycler", r"C:\Users\Cycler"))
        check("bad \\U escape skips only its line",
              [k["reason"] for k in lenient["skipped"]] == ["expected hexadecimal character"]
              and lenient["cfg"]["watched_paths"][0]["path"] is None
              and lenient["cfg"]["watched_paths"][0]["name"] == "sycamore", lenient)
        page.fill("#yaml", good_yaml)
        page.click("#loadYaml")
        page.wait_for_function("document.querySelector('#banners').innerText === ''")

        # A path pasted in place of the file's text says so.
        page.fill("#yaml", "/Users/me/Desktop/config.yaml")
        page.click("#loadYaml")
        page.wait_for_function("document.querySelector('#banners').innerText.includes('not its file name')")
        check("a pasted path is named as the mistake", cards() == ["sycamore", "eis"], cards())
        page.fill("#yaml", good_yaml)
        page.click("#loadYaml")
        page.wait_for_function("document.querySelector('#banners').innerText === ''")

        # A regex the browser cannot compile stops the load before anything changes.
        page.fill("#yaml", good_yaml.replace('id_patterns: [".*(?P<item_id>CEL-[0-9]{3,5}).*"]',
                                             'id_patterns: ["(?i)(?P<item_id>CEL-[0-9]+)"]'))
        page.click("#loadYaml")
        page.wait_for_function("document.querySelector('#banners').innerText.includes('not loaded')")
        check("config with an unusable regex is refused and names its path",
              "watched path eis" in page.inner_text("#banners") and cards() == ["sycamore", "eis"],
              page.inner_text("#banners"))
        page.fill("#yaml", good_yaml)
        page.click("#loadYaml")
        page.wait_for_function("document.querySelector('#banners').innerText === ''")

        card("sycamore").locator("[data-act=edit]").click()
        card("sycamore").locator("[name=name]").fill("")
        card("sycamore").locator("[data-act=save]").click()
        page.wait_for_function("""document.querySelector('.watch[data-name="sycamore"] .dlgerr')
                                  .textContent.includes('needs a name')""")
        check("a path cannot be saved without a name", cards() == ["sycamore", "eis"], cards())
        card("sycamore").locator("[data-act=cancel]").click()
        card("sycamore").locator("[data-act=edit]").click()
        check("Cancel drops unsaved edits", card("sycamore").locator("[name=name]").input_value() == "sycamore")
        card("sycamore").locator("[data-act=cancel]").click()

        # BEHOLDER_CONFIG=path/to/config.yaml checks that a real config maps
        # too. Only the mapping runs, so no request reaches its server.
        if real := os.environ.get("BEHOLDER_CONFIG"):
            mapped = page.evaluate(
                "(t) => beholder.settingsFromConfig(jsyaml.load(t))",
                Path(real).read_text(errors="replace"))
            check("BEHOLDER_CONFIG maps", bool(mapped["s"]["apiUrl"])
                  and all(w["idPattern"] for w in mapped["watches"]), mapped["watches"])

        def write(files, remove=()):
            page.evaluate(WRITE_FILES, [files, list(remove)])

        # sycamore watches the whole test folder, eis its subfolder "eis".
        def sync():
            MOCK.calls.clear()
            page.evaluate("""async () => { await beholder.useFolder(beholder.watchId('sycamore'),
                                                                 await navigator.storage.getDirectory()) }""")

        def tiles(name="sycamore"):
            c = card(name)
            return dict(zip(c.locator(".tile .l").all_text_contents()[:4],
                            c.locator(".tile .n").all_inner_texts()[:4]))

        def pill():
            return page.inner_text("#state")

        def rows(name="sycamore"):
            wid = page.evaluate("(n) => beholder.watchId(n)", name)
            return {r["path"]: r for r in page.evaluate("beholder.filesAll()") if r["watch"] == wid}

        # 1. First sync: create items and collection, upload, add blocks.
        write({"eis/CEL-0003 eis.mpr": "e1"})
        page.evaluate("""async () => { const root = await navigator.storage.getDirectory();
          await beholder.useFolder(beholder.watchId('eis'), await root.getDirectoryHandle('eis'), {sync: false}) }""")
        check("a path without a folder shows in the header pill", pill() == "1 folder not syncing", pill())
        write({
            "CEL-001 run a.ndax": "a1",
            "sub/CEL-001_b.ndax": "b1",
            "deep/er/CEL-0002/test.ndax": "c1",
            "notes.txt": "ignored, wrong extension",
            "no-id.ndax": "ignored, no item id",
        })
        sync()
        check("creates the items of both paths", set(MOCK.items) == {"CEL-001", "CEL-0002", "CEL-0003"},
              list(MOCK.items))
        check("creates the collection once", list(MOCK.collections) == ["APC-P2"])
        check("links sycamore's items to its collection, and eis's to none",
              MOCK.items["CEL-001"]["collections"] and MOCK.items["CEL-0002"]["collections"]
              and not MOCK.items["CEL-0003"]["collections"])
        check("item type is cells", all(i["type"] == "cells" for i in MOCK.items.values()))
        check("one pass uploads both paths' files", len(uploads()) == 4, len(uploads()))
        check("one /info check for the whole pass", MOCK.calls.count(("GET", "/info")) == 1, MOCK.calls)
        check("CEL-001 has two files", len(MOCK.items["CEL-001"]["files"]) == 2)
        blocks = [b for i in MOCK.items.values() for b in i["blocks_obj"].values()]
        check("one block per file of its path's type, each wired to its file",
              sorted(b["blocktype"] for b in blocks) == ["cycle"] * 3 + ["eis"]
              and all(b.get("file_id") for b in blocks), blocks)
        check("each path keeps its own rows", set(rows("eis")) == {"CEL-0003 eis.mpr"}, rows("eis"))
        check("each card counts its own files",
              tiles("eis") == {"synced": "1", "pending": "0", "stuck": "0", "no item_id": "0"}, tiles("eis"))
        check("ignores non-matching files", set(rows()) == {
            "CEL-001 run a.ndax", "sub/CEL-001_b.ndax", "deep/er/CEL-0002/test.ndax"}, list(rows()))
        check("all rows synced", all(r["syncedSize"] == r["size"] for r in rows().values()))
        check("status: tiles count synced, pending, stuck, no item_id",
              tiles() == {"synced": "3", "pending": "0", "stuck": "0", "no item_id": "1"}, tiles())
        check("status: pill says running", pill() == "running", pill())
        check("status: recently uploaded lists three files",
              card("sycamore").locator("table.recent tr").count() == 4)
        check("status: no-item_id note explains itself", "never upload" in card("sycamore").inner_text())

        # 1b. An example ID writes the regex, and the preview shows what each
        # file in the folder would get, the file with no ID first.
        c = card("sycamore")
        c.locator("[data-act=edit]").click()
        saved_regex = c.locator("[name=idPattern]").input_value()
        c.locator("[name=example]").fill("0001")
        c.locator("[data-act=example]").click()
        check("an example of digits alone is refused and leaves the regex",
              "needs letters and a number" in c.locator(".dlgerr").inner_text()
              and c.locator("[name=idPattern]").input_value() == saved_regex,
              c.locator(".dlgerr").inner_text())
        c.locator("[name=example]").fill("CEL")
        c.locator("[data-act=example]").click()
        check("an example of letters alone is refused too",
              "needs letters and a number" in c.locator(".dlgerr").inner_text()
              and c.locator("[name=idPattern]").input_value() == saved_regex)
        c.locator("[name=example]").fill("CEL-0001")
        c.locator("[data-act=example]").click()
        page.wait_for_function("""document.querySelector('.watch[data-name="sycamore"] .preview')
                                  .innerText.includes('match the include')""")
        prev = c.locator(".preview").inner_text()
        check("an example ID writes a regex beholder accepts",
              c.locator("[name=idPattern]").input_value() == "(?P<item_id>CEL-[0-9]+)",
              c.locator("[name=idPattern]").input_value())
        check("the preview counts files and flags the one with no ID first",
              "4 file(s) match" in prev and "1 gets no item ID" in prev
              and c.locator(".preview tr").nth(1).inner_text().startswith("no-id.ndax")
              and "CEL-0002" in prev, prev)
        check("the preview lists the distinct IDs in order, with their lengths",
              "2 distinct item IDs: CEL-001, CEL-0002. 7 to 8 characters long." in prev, prev)
        c.locator("[name=idPattern]").fill("(?P<item_id>[")
        page.wait_for_function("""document.querySelector('.watch[data-name="sycamore"] .preview')
                                  .innerText.includes('cannot be used')""")
        check("the preview follows typing and names a broken regex", True)
        c.locator("[data-act=cancel]").click()

        # 2. Nothing changed: no uploads, no server lookups beyond nothing.
        sync()
        check("unchanged folder uploads nothing", uploads() == [], uploads())
        check("unchanged folder only asks datalab whether it is up",
              MOCK.calls == [("GET", "/info")], MOCK.calls)
        MOCK.down = True
        sync()
        down = pill()
        MOCK.down = False
        sync()
        check("an outage shows with nothing to upload, and clears", (down, pill()) == ("datalab unreachable", "running"),
              (down, pill()))

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
              and "HTTP 500" in card("sycamore").inner_text() and page.inner_text("#banners") == ""
              and card("eis").locator(".pill").inner_text() == "watching",
              (pill(), tiles(), card("sycamore").inner_text()))
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

        # 7a. A config that renames a path and moves its folder path still
        # keeps its folder and rows, because the folder chosen has the name
        # the new path ends in. This is how a path set up by hand carries over.
        page.fill("#yaml", good_yaml.replace('name: "eis"', 'name: "potentiostat"')
                  .replace('path: "D:/eis"', 'path: "F:/lab/eis"'))
        page.click("#loadYaml")
        wait_cards(["sycamore", "potentiostat"])
        page.wait_for_function("""document.querySelector('.watch[data-name="potentiostat"] .pill')
                                  ?.textContent === 'watching'""")
        check("a renamed path keeps its folder and rows by the folder's name",
              set(rows("potentiostat")) == {"CEL-0003 eis.mpr"}, rows("potentiostat"))

        # 7b. Removing a path forgets its rows and card and leaves the others.
        page.once("dialog", lambda d: d.accept())
        card("potentiostat").locator("[data-act=edit]").click()
        card("potentiostat").locator("[data-act=remove]").click()
        wait_cards(["sycamore"])
        check("removed path loses its rows, others keep theirs",
              not any(r["watch"] != page.evaluate("beholder.watchId('sycamore')")
                      for r in page.evaluate("beholder.filesAll()")) and len(rows()) == 2, rows())

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
        hints = page.evaluate(r"""() => ({
          sycamore: beholder.folderHints('C://Users/Cycler/Desktop/data'),
          backslash: beholder.folderHints(String.raw`C:\Users\x\Documents\runs\2026`),
          plain: beholder.folderHints('D:/cycler'),
          right: beholder.folderWarning('DATA', 'C://Users/Cycler/Desktop/data'),
          wrong: beholder.folderWarning('Desktop', 'C://Users/Cycler/Desktop/data'),
          noPath: beholder.folderWarning('anything', undefined),
        })""")
        check("picker starts on the Desktop for the sycamore path",
              hints["sycamore"] == {"startIn": "desktop", "expected": "data"}, hints)
        check("Windows backslash paths parse",
              hints["backslash"] == {"startIn": "documents", "expected": "2026"}, hints)
        check("no well-known folder gives no startIn", "startIn" not in hints["plain"]
              or hints["plain"]["startIn"] is None, hints)
        check("matching folder name, any case, gives no warning", hints["right"] is None, hints)
        check("wrong folder name warns", "Desktop" in (hints["wrong"] or "") and "data" in hints["wrong"], hints)
        check("no config path gives no warning", hints["noPath"] is None, hints)
        picks = page.evaluate("""() => {
          const cfg = {datalabs: [
              {name: 'collab', url: 'https://datalab.collab.ac.uk', api_key: 'theirs'},
              {name: 'lt', url: 'https://datalab.lightningtree.ai', api_key: 'ours'}],
            watched_paths: [
              {name: 'a', path: 'C:/a', datalab: 'collab'},
              {name: 'b', path: 'C:/b', datalab: 'lt'}]};
          const pick = (d) => { const r = beholder.settingsFromConfig(cfg, d); return [r.dl.name, r.watches[0].name, r.s.apiKey]; };
          return {byDefault: pick(''), chosen: pick('collab'), unknown: pick('nope')};
        }""")
        check("multi-datalab config defaults to datalab.lightningtree.ai",
              picks["byDefault"] == ["lt", "b", "ours"], picks)
        check("an explicit datalab choice wins", picks["chosen"] == ["collab", "a", "theirs"], picks)
        check("an unknown datalab name falls back to the default", picks["unknown"] == ["lt", "b", "ours"], picks)
        check("blank max depth is unlimited", cases["unlimited"] is None or cases["unlimited"] == float("inf"), cases)

        # 9. A browser set up when the page watched one folder keeps that
        # folder and its rows, so nothing uploads again. The old database is
        # built from another page of the same origin, in a fresh context.
        MOCK.calls.clear()
        origin = f"http://localhost:{page_srv.server_port}"
        old = browser.new_context().new_page()
        old.on("pageerror", lambda e: errors.append(str(e)))
        old.goto(origin + "/no-such-page")  # a 404 page, but on the right origin
        old.evaluate(V1_SETUP, api_url)
        old.goto(origin + "/beholder-sync.html")
        old.wait_for_selector(".watch .tile")
        check("version 1 rows and config path carry over",
              old.locator(".watch .tile .n").first.inner_text() == "1"
              and "config path C:/x/data" in old.locator(".watch .path").inner_text(),
              old.locator(".watch").inner_text())
        old.evaluate("async () => { await beholder.useFolder('default', await navigator.storage.getDirectory()) }")
        check("version 1 files do not upload again",
              old.inner_text("#state") == "running" and uploads() == [] and "CEL-0009" not in MOCK.items,
              (old.inner_text("#state"), uploads()))

        check("no page errors", not errors, errors)
        browser.close()

    print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED'}")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
