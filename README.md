# Emoticon Lookup

Build a portable, animated reference gallery of the Chaturbate emoticons used by a MongoDB chat user. The default output is one self-contained HTML file: it embeds the media, styling, and controls and works offline in a modern browser. Static PDF export is also available.

## Features

- Search by name, sort alphabetically or by occurrence count, and filter available or unresolved media.
- Copy `:name` to paste into chat.
- Click an image to open its original media at full size in a translucent overlay. Large images can be scrolled or fitted to the screen. Close with Escape, the Close button, or a click outside the viewer.
- Create keyword groups with one JavaScript regex per line. A name belongs to a group if any rule matches. Groups can overlap, support case-sensitive matching, and reject invalid regexes without replacing the saved rules.
- Save groups in browser storage, import/export group JSON, or download a portable HTML copy with saved groups embedded.
- Cache successful downloads and retain unresolved names for reference.

Node.js is needed only for development tests. Opening a generated gallery needs only a browser.

## Setup and run

Requires Python 3.10+ and MongoDB 4.2+ when using the database source.

```powershell
py -m venv .venv
& .venv/Scripts/python.exe -m pip install -r requirements.txt

# Read your URI without displaying it or saving it in shell history.
$lookupSecret = Read-Host 'MongoDB connection URI' -AsSecureString
$env:MONGODB_URI = [System.Net.NetworkCredential]::new('', $lookupSecret).Password
try {
    & .venv/Scripts/python.exe emoticon_lookup.py --user YOUR_USERNAME --output generated/emoticons.html
} finally {
    Remove-Item Env:MONGODB_URI -ErrorAction SilentlyContinue
}
```

On macOS/Linux, create the environment with `python3 -m venv .venv`, install with `.venv/bin/python -m pip install -r requirements.txt`, and set `MONGODB_URI` in your environment. Keep database credentials out of command-line arguments and committed files.

The default schema is database `mongobate`, collection `events`, username field `object.user.username`, and message field `object.message.message`. It selects `chatMessage` and `privateMessage` records and matches the requested username exactly, ignoring case. Override these defaults with `--database`, `--collection`, `--user-field`, and `--message-field`. The autocomplete room defaults to `rndmzd`; override it with `--room`.

By default, every whitespace-delimited `:name` anywhere in a matching message is extracted. `--starts-only` restricts source messages to those starting with an emoticon after optional whitespace; all emoticons in those messages are still counted. Names retain punctuation and case. For example, `:hello,` is resolved as `hello,`. The query counts names on the server rather than grouping every message into one large document. It does not modify the database.

## Other inputs and options

```powershell
# Use an exported message list instead of MongoDB
& .venv/Scripts/python.exe emoticon_lookup.py --user demo_user --input examples/messages.json --output generated/example.html --sample

# Sort by frequency and refresh cached media
& .venv/Scripts/python.exe emoticon_lookup.py --user YOUR_USERNAME --sort frequency --refresh --output generated/emoticons.html

# Restrict source messages to those starting with :name
& .venv/Scripts/python.exe emoticon_lookup.py --user YOUR_USERNAME --starts-only --output generated/emoticons.html

# Static PDF
& .venv/Scripts/python.exe emoticon_lookup.py --user YOUR_USERNAME --output generated/emoticons.pdf
```

Input JSON can be a list of message strings already filtered to the requested user, raw event documents, or grouped results such as `[{"_id":"demo_user","messages":[":name", "text :othername"]}]`. Events and grouped results are filtered to `--user`.

`--cache-dir` overrides `.emoticon-cache`. `--delay` controls the pause between lookups (default 0.25 seconds). `--query-timeout` sets the MongoDB query limit (default 300 seconds). `--sample` marks the output as demonstration data.

## Keyword groups

Open **Manage keyword groups**, click **Create group**, give it a name, and enter one regex per line. `hello|wave` matches either word anywhere; `^et` matches names starting with `et`. Regexes run against the name without its leading colon. Matches ignore case by default; **Case sensitive** changes this. Click **Save group** to apply edits. Empty rules match nothing.

Use **Group view** to show a single group, all groups as sections, or ungrouped names. Search, sorting, and status filters still apply. An emoticon may appear in several sections, while the result counter counts distinct names.

Browser storage does not travel with the HTML file and may be restricted for local files. **Save portable copy** embeds saved groups in a new self-contained HTML file. **Export groups** writes JSON; **Import groups** adds validated groups to the current gallery. Unsaved editor drafts are not included in an export.

## API behavior and output

The script requests `https://chaturbate.com/api/ts/emoticons/autocomplete/?slug=NAME&room=ROOM` with JSON/AJAX headers and reads the response's `emoticons` array. Autocomplete can return prefix suggestions; only an exact `slug` match is accepted. The API response shape was verified during development, but it is not a versioned public API contract.

Media type is detected from the bytes: a `.jpg` URL can contain a GIF. HTML embeds the original GIF/WebP animation; PDFs use a static representative frame. The image viewer plays original animations even when thumbnail animation is paused. Unsupported browser image formats use a marked static preview.

The HTML works independently of the cache. Original-media links require internet. A companion JSON manifest records counts, media status, URLs, and local cache paths for diagnostics; move only the HTML file for portability. Downloads are limited to 20 MB each and 25 million decoded pixels per frame. Missing or failed media remains as an unresolved card. Empty results produce a valid empty gallery. Partial media failures still produce output and exit successfully; database/input failures return exit code 1.

Generated galleries contain the selected username and emoticon usage counts. They, their manifests, connection settings, downloaded media, and local environments are ignored by Git. The committed [animation demo](examples/animation_demo.html) uses synthetic media and is not user history. The sample message JSON is also synthetic; using it invokes live API lookups.

## Development and checks

```powershell
& .venv/Scripts/python.exe -m pip install -r requirements-dev.txt
& .venv/Scripts/python.exe -m unittest discover -s tests -p 'test_*.py' -v
& .venv/Scripts/python.exe scripts/build_examples.py --fixtures
npm ci
npm test
```

For `npm test` to generate missing fixtures automatically, set `PYTHON` to your Python executable or make `python` available on PATH. Rebuild the synthetic demo with `python scripts/build_examples.py`.

The Python checks cover extraction, user/method filtering, exact API matching, caching, original animation bytes, output escaping, empty output, and PDF pagination. Seven DOM scenarios cover the modal, regex OR/overlap/case behavior, invalid rules, persistence, drafts, import/export, and portable copies without storage. DOM checks emulate dialog behavior and do not establish browser layout, native dialog behavior, or animation playback. Tests generate their own media and do not require MongoDB or the live API.
