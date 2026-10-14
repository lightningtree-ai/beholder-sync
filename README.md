# beholder-sync

A single web page that uploads cycler data files from a folder on your computer to a [datalab](https://docs.datalab-org.io) instance.
It does the job of [datalab-beholder](https://github.com/datalab-industries/datalab-beholder) on machines where you cannot run its `.exe` in the background, such as a university-managed PC.
Nothing is installed.
The page runs in Chrome or Edge and syncs while its tab is open.

## Running it

1. Open https://lightningtree-ai.github.io/beholder-sync/beholder-sync.html in Chrome or Edge.
   Opening a downloaded copy of `beholder-sync.html` from disk also loads, but folder access from a local file has not been tested.
2. Open **Settings**. Paste a beholder `config.yaml` into the top box and click **Load config**, or fill in the boxes by hand.
   Get an API key from `<your datalab URL>/get-api-key` if the config has none.
3. Click **Choose folder** and pick the folder your cycler writes to.
   When Chrome asks about folder access, choose **Allow on every visit**, or you will have to click **Resume access to folder** after every browser restart.
4. Leave the tab open.

The page syncs once when you pick the folder, then every `sync.metadata_interval` seconds from the config (20 minutes by default).
**Sync now** runs a pass immediately and retries files that are waiting out a failure.

## What a sync does

Each pass walks the folder and compares every file's size and modified time with the last version that reached datalab.
For each new or changed file it:

1. Works out the item ID from the path with the config's `id_patterns` and `item_id_template`.
2. Creates the item if it does not exist, with `item_type` and the collection from `collection_id_template`.
3. Uploads the file.
   A file already attached under the same name is replaced in place, so its ID and any blocks stay the same.
4. Adds a block from `block_patterns` (for example `cycle` for `*.ndax`) if the item has none wired to that file.

These are the same API calls, in the same order, that datalab-beholder makes.
Patterns follow beholder's rules: globs match the file name without regard to case, and ID regexes can use Python's `(?P<name>...)` syntax.

The status card shows how many files are synced, pending, stuck, or have no item ID, plus the failing files with their errors and the ten most recent uploads.
A failed upload is retried after 1, 2, 4 and so on up to 60 minutes.
A pass where datalab does not answer at all leaves files pending without counting a failure.

## Limitations

- It only syncs while the tab is open. Browsers give pages no background access to files.
- Chrome and Edge only. Firefox and Safari cannot read folders.
- One watched path per tab. With several in the config, a dropdown picks which one.
- The API key is stored unencrypted in the browser's storage for that page. Anyone who can use the browser can read it. Use a key belonging to the person whose PC it is.
- Every GitHub Pages site under `lightningtree-ai.github.io` shares that storage. Do not turn on Pages for another repo in this organisation until the page has a domain of its own.
- New items are not shared with a group. Beholder does this when an ID regex captures `group_id`. The page logs a warning instead.
- Files are not checked for being mid-write. A file the cycler is still writing is uploaded and then replaced on a later pass, as beholder does.
- Do not point the page and a running beholder at the same folder. Both would upload the same files.

## Requirements

- Chrome or Edge, version 122 or later for the persistent folder permission.
- A datalab server whose API sends `Access-Control-Allow-Origin` for the page's origin. datalab's API sends `*`.
- Network access to `cdnjs.cloudflare.com`, which serves the YAML parser. Syncing works without it. Only **Load config** needs it.

## Testing

`test_sync.py` drives the page in headless Chromium against a mock datalab served from a second port, so every request is cross-origin.
The watched folder is built in the browser's origin-private file system, whose handles behave like a folder the user picked.

```
uv run --with playwright python test_sync.py
```

Run `playwright install chromium` once first, or set `CHROMIUM_PATH` to a Chromium binary already on disk.
The last lines of a passing run:

```
PASS Sync now retries a file in backoff
PASS status: stuck clears after the retry
PASS deleted file is forgotten
PASS globs ignore case
PASS glob [!a] negates
PASS greedy regex takes the last ID in the path
PASS no match gives no item
PASS second regex line is tried
PASS blank max depth is unlimited
PASS no page errors

ALL PASSED
```

The test cannot open the native folder picker, so choosing a folder and the permission prompt are only checked by hand.

---

Written with [Claude Code](https://claude.com/claude-code).
