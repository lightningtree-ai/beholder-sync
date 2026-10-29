# beholder-sync

A single web page that uploads cycler data files from folders on your computer to a [datalab](https://docs.datalab-org.io) instance.
It does the job of [datalab-beholder](https://github.com/datalab-industries/datalab-beholder) on machines where you cannot run its `.exe` in the background, such as a university-managed PC.
Nothing is installed.
The page runs in Chrome or Edge and syncs while its tab is open.

## Running it

1. Open https://lightningtree-ai.github.io/beholder-sync/beholder-sync.html in Chrome or Edge.
   Opening a downloaded copy of `beholder-sync.html` from disk also loads, but folder access from a local file has not been tested.
2. Open **Datalab settings**. Paste a beholder `config.yaml` into the top box and click **Load config**.
   Each watched path in the config appears under **Watched folders**.
   Without a config, fill in the boxes by hand and click **Add folder** for each folder.
   Get an API key from `<your datalab URL>/get-api-key` if the config has none.
   If a line does not parse, the rest of the config still loads, and a banner names the line.
   Write Windows paths in single quotes, such as `path: 'C:\Users\me\data'`.
3. On each watched folder, click **Choose folder** and pick the folder that instrument writes to.
   When Chrome asks about folder access, choose **Allow on every visit**.
   Otherwise you need to click **Resume folder access**, next to the page title, after every browser restart.
   Chrome may ask once per folder.
4. Leave the tab open.

The page syncs once when you pick a folder, then every `sync.metadata_interval` seconds from the config (20 minutes by default).
**Sync now** runs a pass immediately and retries files that are waiting out a failure.

## What a sync does

A watched path is one entry of the config's `watched_paths`: a folder, and the patterns that pick and file its data.
Several paths can share a machine, such as two cyclers and an EIS potentiostat.
They all upload to the same datalab.

Each pass walks every chosen folder and compares every file's size and modified time with the last version that reached datalab.
For each new or changed file it:

1. Works out the item ID from the path with that watched path's `id_patterns` and `item_id_template`.
2. Creates the item if it does not exist, with `item_type` and the collection from `collection_id_template`.
3. Uploads the file.
   A file already attached under the same name is replaced in place, so its ID and any blocks stay the same.
4. Adds a block from `block_patterns` (for example `cycle` for `*.ndax`) if the item has none wired to that file.

These are the same API calls, in the same order, that datalab-beholder makes.
Patterns follow beholder's rules: globs match the file name without regard to case, and ID regexes can use Python's `(?P<name>...)` syntax.

## Writing the item ID rule

The item ID regex picks the part of a file's path that names its datalab item.
**Edit patterns** on a folder's row opens a window with that path's rules, and offers two aids for writing the regex.

Type one item ID in the box beside the regex, such as `CEL-081`, and click **Build regex**.
The regex box then holds a regex that matches the ID's text as typed, with any number in place of each run of digits.
It replaces what the box held before. Nothing is saved until **Save patterns**.
For `CEL-081` that is `(?P<item_id>CEL-[0-9]+)`, which finds `CEL-9999` in `253_CEL-9999-1_4_9.ndax`.
The page refuses an example that is not letters plus a number. Digits alone would give `[0-9]+`, which takes the first number anywhere in the path, such as a channel number. Letters alone would send every file to one item.

Beside the boxes, a preview lists files from the chosen folder with the item ID each would get.
Files that get none come first, in red.
Above the list, the distinct IDs found are shown in sort order, so a wrong one such as `253` stands out at an end.
The preview follows the boxes as you type, before you save.
**Cancel** or Escape closes the window and drops what was not saved.

For IDs that are not text plus a number, an AI assistant such as Claude or ChatGPT can write the regex.
Give it three file names and the ID in each, and ask for a Python regex with a named group `item_id`.
Paste the answer into the regex box and check it in the preview.
Assistants often get this kind of regex wrong, because the ID is only part of the name, so check every time.

Each watched path has a row under **Watched folders**.
The row shows the path's name, its state, the folder chosen for it, and counts of its files that are synced, pending, stuck, or have no item ID.
**Show details** opens the row.
Inside are its rules, its failing files with their errors, and its ten most recent uploads.
A long list of failing files shows the ten that failed most often, and a count of the rest.
A row opens by itself while it has no folder, and again when it starts failing.
The pill at the top sums the rows and shows the worst state.
A failed upload is retried after 1, 2, 4 and so on up to 60 minutes.
Each pass asks datalab whether it is up, even with nothing to upload, so **running** means it answered at the last sync.
A pass where datalab does not answer at all shows **datalab unreachable** and leaves files pending without counting a failure.
That check does not test the API key. A wrong key shows as uploads that fail.

## Limitations

- It only syncs while the tab is open. Browsers give pages no background access to files.
- Chrome and Edge only. Firefox and Safari cannot read folders.
- One datalab per tab. With several in the config, a dropdown picks which one, and only its watched paths load.
- Loading a config replaces the watched paths. A path already set up keeps its folder and sync state when the config has a path with the same name or folder path. Failing both, a chosen folder named like the last part of the config's path also counts.
- The API key is stored unencrypted in the browser's storage for that page. Anyone who can use the browser can read it. Use a key belonging to the person whose PC it is.
- Every GitHub Pages site under `lightningtree-ai.github.io` shares that storage. Do not turn on Pages for another repo in this organisation until the page has a domain of its own.
- New items are not shared with a group. Beholder does this when an ID regex captures `group_id`. The page logs a warning instead.
- Files are not checked for being mid-write. A file the cycler is still writing is uploaded and then replaced on a later pass, as beholder does.
- Do not point the page and a running beholder at the same folder. Both would upload the same files.

## Requirements

- Chrome or Edge, version 122 or later for the persistent folder permission.
- A datalab server whose API sends `Access-Control-Allow-Origin` for the page's origin. datalab's API sends `*`.
- Network access to `cdnjs.cloudflare.com`, which serves the YAML parser. Syncing works without it. Only **Load config** needs it.

## Trying it out

`demo.py` runs the page against a mock datalab on this computer, so nothing reaches a real server.

```
python3 demo.py
```

It fills `~/Desktop/beholder-demo` with cycler and EIS files, some with awkward names, and writes a `config.yaml` for them. On a Mac it also puts the config text on the clipboard, ready to paste into **Datalab settings**.
It prints the steps, and a line for each change the page makes on the mock datalab.
Type `f` and Enter to make uploads fail, `d` to make datalab stop answering, and `q` to quit.
The mock forgets everything when the script stops.

## Testing

`test_sync.py` drives the page in headless Chromium against a mock datalab served from a second port, so every request is cross-origin.
The watched folder is built in the browser's origin-private file system, whose handles behave like a folder the user picked.

```
uv run --with playwright python test_sync.py
```

Run `playwright install chromium` once first, or set `CHROMIUM_PATH` to a Chromium binary already on disk.
The last lines of a passing run:

```
PASS matching folder name, any case, gives no warning
PASS wrong folder name warns
PASS no config path gives no warning
PASS multi-datalab config defaults to datalab.lightningtree.ai
PASS an explicit datalab choice wins
PASS an unknown datalab name falls back to the default
PASS blank max depth is unlimited
PASS version 1 rows and config path carry over
PASS version 1 files do not upload again
PASS no page errors

ALL PASSED
```

The test cannot open the native folder picker, so choosing a folder and the permission prompt are only checked by hand.

---

Written with [Claude Code](https://claude.com/claude-code).
