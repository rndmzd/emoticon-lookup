"""Create a portable animated HTML gallery (or PDF) of one user's emoticons.

Connection URI comes from MONGODB_URI; it is never written to the output.
Requires Python 3.10+ and the packages in requirements.txt.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
from datetime import datetime
from hashlib import sha256
from io import BytesIO
import json
from html import escape
import math
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlencode, urljoin, urlparse

from PIL import Image, ImageStat, UnidentifiedImageError
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

API = "https://chaturbate.com/api/ts/emoticons/autocomplete/"
TOKEN_PATTERN = r"(?<!\S):([^\s:]+)"
MAX_MEDIA_BYTES = 20 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 25_000_000


def field(document, path):
    value = document
    for key in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def extract_messages(messages, pattern=TOKEN_PATTERN, starts_only=False):
    regex = re.compile(pattern)
    counts = Counter()
    for message in messages:
        if not isinstance(message, str):
            continue
        if starts_only and not re.match(r"^\s*:[^\s:]", message):
            continue
        for match in regex.finditer(message):
            counts[match.group(1)] += 1
    return counts


def mongo_pipeline(args):
    """Count tokens on the server without building a large messages array."""
    return [
        {"$match": {
            "method": {"$in": ["chatMessage", "privateMessage"]},
            args.user_field: {"$type": "string", "$regex": "^" + re.escape(args.user) + "$", "$options": "i"},
            args.message_field: {"$type": "string", "$regex": r"^\s*:[^\s:]" if args.starts_only else TOKEN_PATTERN},
        }},
        {"$project": {"tokens": {"$regexFindAll": {
            "input": "$" + args.message_field, "regex": TOKEN_PATTERN,
        }}}},
        {"$unwind": "$tokens"},
        {"$group": {"_id": {"$arrayElemAt": ["$tokens.captures", 0]}, "count": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ]


def collect(args):
    if args.input:
        text = args.input.read_text(encoding="utf-8-sig")
        rows = json.loads(text)
        if not isinstance(rows, list):
            raise ValueError("Input must be a JSON array of messages, events, or grouped results.")
        messages = []
        for row in rows:
            if isinstance(row, str):
                messages.append(row)  # A prefiltered export belonging to the requested user.
            elif isinstance(row, dict) and isinstance(row.get("messages"), list):
                if str(row.get("_id", "")).casefold() == args.user.casefold():
                    messages.extend(row["messages"])
            elif isinstance(row, dict):
                username = field(row, args.user_field)
                if (isinstance(username, str) and username.casefold() == args.user.casefold()
                        and row.get("method") in ("chatMessage", "privateMessage")):
                    messages.append(field(row, args.message_field))
        return extract_messages(messages, starts_only=args.starts_only)

    from pymongo import MongoClient
    from pymongo.errors import PyMongoError
    uri = os.environ.get("MONGODB_URI")
    if not uri:
        raise ValueError("Set MONGODB_URI to your MongoDB connection string, or use --input.")
    try:
        with MongoClient(uri, serverSelectionTimeoutMS=8000, appname="emoticon-lookup") as client:
            client.admin.command("ping")
            collection = client[args.database][args.collection]
            with collection.aggregate(mongo_pipeline(args), allowDiskUse=True, maxTimeMS=args.query_timeout * 1000) as cursor:
                return Counter({row["_id"]: int(row["count"]) for row in cursor})
    except PyMongoError as exc:
        # Avoid echoing connection strings or authentication details in exceptions.
        raise RuntimeError(f"MongoDB query failed ({type(exc).__name__}). Check connection, credentials, schema, and server version.") from None


def http_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "EmoticonLookup/1.0",
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://chaturbate.com/rndmzd/",
    })
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET"], respect_retry_after_header=True)
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def exact_media(payload, slug):
    if not isinstance(payload, dict) or not isinstance(payload.get("emoticons"), list):
        raise ValueError("API JSON does not contain the expected emoticons array")
    for item in payload["emoticons"]:
        if isinstance(item, dict) and item.get("slug") == slug and isinstance(item.get("url"), str):
            url = urljoin(API, item["url"])
            if urlparse(url).scheme != "https":
                raise ValueError("API returned a non-HTTPS media URL")
            return url
    return None


def download(session, url):
    with session.get(url, headers={"Accept": "image/*"}, timeout=(10, 30), stream=True) as response:
        response.raise_for_status()
        data = bytearray()
        for chunk in response.iter_content(64 * 1024):
            data.extend(chunk)
            if len(data) > MAX_MEDIA_BYTES:
                raise ValueError("Media exceeds the 20 MB download limit")
        return bytes(data)


def thumbnail(data):
    """Choose a visible static frame; keep the original animation on disk."""
    with Image.open(BytesIO(data)) as image:
        frames = getattr(image, "n_frames", 1)
        best, best_score = None, -1
        for index in sorted({0, min(1, frames - 1), frames // 4, frames // 2, 3 * frames // 4}):
            image.seek(index)
            candidate = image.convert("RGBA")
            candidate.thumbnail((600, 360), Image.Resampling.LANCZOS)
            background = Image.new("RGBA", candidate.size, "white")
            background.alpha_composite(candidate)
            score = sum(ImageStat.Stat(background.convert("RGB")).var)
            if score > best_score:
                best, best_score = candidate.copy(), score
        output = BytesIO()
        best.save(output, format="PNG")
        return output.getvalue(), frames > 1


def resolve(session, slug, room, cache_dir, refresh=False):
    key = sha256((room + "\0" + slug).encode("utf-8")).hexdigest()
    meta_path, media_path, preview_path = [cache_dir / (key + suffix) for suffix in (".json", ".media", ".png")]
    if refresh:
        meta_path.unlink(missing_ok=True)
    if not refresh and meta_path.exists() and media_path.exists() and preview_path.exists():
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            if metadata.get("slug") == slug and metadata.get("room") == room and metadata.get("status") == "ok":
                with Image.open(preview_path) as image:
                    image.verify()
                return metadata | {"preview": str(preview_path.resolve()), "media": str(media_path.resolve())}
        except (ValueError, OSError, UnidentifiedImageError):
            pass
    row = {"slug": slug, "room": room, "status": "missing", "url": None, "animated": False}
    try:
        response = session.get(API, params={"slug": slug, "room": room}, timeout=(10, 30))
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError:
            raise ValueError("API returned a non-JSON page; try again or check access") from None
        url = exact_media(payload, slug)
        if not url:
            row["detail"] = "No exact API match"
            return row
        row["url"] = url
        media = download(session, url)
        png, animated = thumbnail(media)
        media_path.write_bytes(media)
        preview_path.write_bytes(png)
        row.update(status="ok", animated=animated)
        meta_path.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
        row["preview"] = str(preview_path.resolve())
        row["media"] = str(media_path.resolve())
    except requests.RequestException as exc:
        row.update(status="error", detail=f"HTTP/network error ({type(exc).__name__})")
    except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        row.update(status="error", detail=str(exc)[:150])
    return row


def data_url(path):
    data = Path(path).read_bytes()
    with Image.open(BytesIO(data)) as image:
        mime = Image.MIME.get(image.format, "image/png")
    if mime not in ("image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "image/bmp"):
        raise ValueError(f"Browser cannot display the original media type: {mime}")
    return "data:" + mime + ";base64," + base64.b64encode(data).decode("ascii")


def write_html(path, rows, user, room, sample=False, sort="name"):
    """Embed original media, CSS, and JS: one file, no runtime requests."""
    cards = []
    for row in rows:
        slug = escape(row["slug"], quote=True)
        name = ":" + slug
        if row["status"] == "ok":
            poster = data_url(row["preview"])
            try:
                source = data_url(row["media"])
                note = "Demo animation" if row.get("demo") else "Animated" if row.get("animated") else "Still image"
            except ValueError:
                source, note = poster, "Static preview only"
            visual = f'<button class="preview-open" type="button" aria-label="View {name} at full size"><img src="{source}" data-poster="{poster}" alt="{name}" loading="lazy"></button>'
            link = (f'<a class="media-link" href="{escape(row["url"], quote=True)}" target="_blank" rel="noopener noreferrer">Original media ↗</a>'
                    if row.get("url") else '<span class="media-link">Synthetic animation example</span>')
        else:
            note = "No exact API match" if row["status"] == "missing" else "Download failed"
            visual = '<div class="placeholder"><span>◌</span>Preview unavailable</div>'
            link = f'<span class="media-link">{escape(row.get("detail", note))}</span>'
        cards.append(f'''<article class="card" data-name="{slug}" data-count="{int(row['count'])}" data-status="{row['status']}">
          <div class="visual">{visual}<span class="badge">{note}</span></div>
          <div class="card-body"><code>{name}</code><div class="card-bottom"><span>{row['count']} occurrence{'s' if row['count'] != 1 else ''}</span><button class="copy" type="button" aria-label="Copy {name}">Copy name</button></div>{link}</div>
        </article>''')
    good = sum(row["status"] == "ok" for row in rows)
    template = '''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Emoticon lookup · __USER__</title>
<style>
:root{color-scheme:light;--ink:#1c3038;--muted:#5f7077;--line:#dbe3e2;--accent:#167365;--paper:#f2f5f1}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
.container{width:min(1260px,calc(100% - 48px));margin:0 auto}header{background:#1c3038;color:#fff;padding:40px 0 32px}.eyebrow{font-size:12px;letter-spacing:2px;text-transform:uppercase;color:#adcbc3;font-weight:700}
h1{font-size:clamp(30px,4vw,46px);line-height:1.1;margin:12px 0 14px;letter-spacing:-1.2px}header p{margin:0;color:#c7d8d8}.stats{display:flex;gap:24px;flex-wrap:wrap;margin-top:24px}.stat strong{font-size:22px;color:#fff;margin-right:5px}.stat{color:#c7d8d8;font-size:13px}
.sample{background:#f6e6bd;color:#5a441b;padding:11px 0;font-size:13px}.toolbar{padding:22px 0;display:flex;gap:14px;align-items:end;flex-wrap:wrap;position:sticky;top:0;z-index:2;background:var(--paper);border-bottom:1px solid var(--line)}
.control{display:flex;flex-direction:column;gap:6px;font-size:12px;font-weight:650;color:var(--muted)}.search{flex:1;min-width:220px}input[type=search],select{border:1px solid #bdcbc7;border-radius:7px;padding:11px 12px;background:#fff;color:var(--ink);font:inherit;font-size:14px;min-height:44px}
.toggle{display:flex;gap:8px;align-items:center;min-height:44px;font-size:13px}input[type=checkbox]{accent-color:var(--accent);width:17px;height:17px}.results{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;font-size:13px;color:var(--muted);padding:20px 0 14px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:18px;padding-bottom:30px}.card{min-width:0;border:1px solid var(--line);border-radius:12px;background:#fff;overflow:hidden;box-shadow:0 2px 3px #1c303805}
.visual{height:190px;position:relative;display:flex;align-items:center;justify-content:center;padding:18px 14px 35px;background-color:#fafbfa;background-image:linear-gradient(45deg,#e7ece7 25%,transparent 25%),linear-gradient(-45deg,#e7ece7 25%,transparent 25%),linear-gradient(45deg,transparent 75%,#e7ece7 75%),linear-gradient(-45deg,transparent 75%,#e7ece7 75%);background-size:16px 16px;background-position:0 0,0 8px,8px -8px,-8px 0}
.preview-open{display:flex;align-items:center;justify-content:center;width:100%;height:100%;padding:0;border:0;background:transparent;cursor:zoom-in}.visual img{max-width:100%;max-height:100%;object-fit:contain}.badge{position:absolute;bottom:10px;left:12px;background:#fffffff0;border:1px solid var(--line);color:var(--muted);font-size:10px;padding:2px 7px;border-radius:12px;pointer-events:none}
.card-body{padding:16px}code{font:650 16px/1.4 ui-monospace,Consolas,monospace;overflow-wrap:anywhere;display:block}.card-bottom{display:flex;align-items:center;justify-content:space-between;gap:8px;margin:14px 0 10px;color:var(--muted);font-size:12px}
button{cursor:pointer}.copy,.action{background:#e3f1eb;border:1px solid #c8ded5;color:#166054;border-radius:6px;font-weight:650;font-size:12px;padding:7px 9px;white-space:nowrap}.copy:hover,.action:hover{background:#cee7dc}button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,summary:focus-visible{outline:3px solid #45a497;outline-offset:2px}
.media-link{font-size:11px;color:var(--muted);display:block;overflow-wrap:anywhere;text-underline-offset:3px}.placeholder{display:flex;flex-direction:column;align-items:center;color:var(--muted);font-size:12px}.placeholder span{font-size:42px;color:#85978e}
.empty{display:none;background:#fff;border:1px solid var(--line);padding:40px;border-radius:12px;text-align:center}.empty.show{display:block}.card[hidden]{display:none}footer{border-top:1px solid var(--line);padding:22px 0 30px;color:var(--muted);font-size:12px}.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#1c3038;color:#fff;padding:12px 22px;border-radius:8px;opacity:0;pointer-events:none;transition:opacity .15s;z-index:4}.toast.visible{opacity:1}
.group-panel{border:1px solid var(--line);border-radius:10px;background:#fff;margin-top:18px;padding:14px 18px}.group-panel summary{cursor:pointer;font-weight:650}.group-help{font-size:13px;color:var(--muted);margin:12px 0}.group-help code{display:inline;font-size:12px}.group-actions{display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin:14px 0}.secondary{background:#f3f6f4;border-color:var(--line);color:var(--ink)}.group-list{display:grid;gap:12px}.group-editor{border:1px solid var(--line);border-radius:8px;padding:14px;display:grid;grid-template-columns:minmax(150px,1fr) minmax(200px,2fr);gap:12px}.group-editor input[type=text],.group-editor textarea{width:100%;border:1px solid #bdcbc7;border-radius:6px;padding:9px;font:inherit;color:var(--ink);background:#fff}.group-editor textarea{font-family:ui-monospace,Consolas,monospace;min-height:76px;resize:vertical}.group-editor-actions{display:flex;flex-wrap:wrap;align-items:center;gap:12px;grid-column:1/-1}.group-error{font-size:12px;color:#a03929;white-space:pre-wrap}.group-error:empty{display:none}.group-save-note{font-size:12px;color:var(--muted)}.group-save-note[data-state=unsaved]{color:#8d5924}.group-editor label{font-size:12px;color:var(--muted)}.grouped-layout{display:block}.group-section{margin:0 0 30px}.group-section h2{font-size:19px;margin:0 0 12px;display:flex;gap:12px;align-items:baseline;flex-wrap:wrap}.group-section h2 span{font-size:12px;font-weight:400;color:var(--muted)}.group-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:18px}
.viewer{border:0;padding:0;background:transparent;color:#fff;width:max-content;max-width:calc(100vw - 32px);max-height:calc(100vh - 32px);max-height:calc(100dvh - 32px);overflow:hidden}.viewer::backdrop{background:rgba(12,23,28,.62);backdrop-filter:blur(2px)}.viewer-toolbar{display:flex;gap:18px;align-items:center;justify-content:space-between;padding:12px 16px;background:rgba(20,36,43,.88);border-radius:10px 10px 0 0}.viewer-name{margin:0;overflow-wrap:anywhere;min-width:0}.viewer-name code{font-size:14px}.viewer-size{font-size:11px;color:#c7d8d8}.viewer-buttons{display:flex;gap:8px;flex-shrink:0}.viewer-buttons button{background:#ffffff18;border:1px solid #ffffff45;color:#fff;padding:7px 10px;border-radius:6px}.viewer-stage{overflow:auto;max-width:calc(100vw - 32px);max-height:calc(100vh - 106px);max-height:calc(100dvh - 106px);background:transparent}.viewer-stage img{display:block;width:auto;height:auto;max-width:none;max-height:none;margin:auto}.viewer.fit .viewer-stage img{max-width:calc(100vw - 32px);max-height:calc(100vh - 106px);max-height:calc(100dvh - 106px);object-fit:contain}.viewer-stage:focus-visible{outline:2px solid #45a497;outline-offset:-2px}
@media(max-width:600px){.container{width:calc(100% - 28px)}header{padding:28px 0}.toolbar{position:static;gap:10px}.search{flex-basis:100%}.grid{grid-template-columns:repeat(auto-fill,minmax(205px,1fr))}.stats{gap:14px}.results{font-size:12px}}
@media(max-width:600px){.group-editor{grid-template-columns:1fr}.group-grid{grid-template-columns:repeat(auto-fill,minmax(205px,1fr))}.viewer-toolbar{gap:8px;padding:10px}.viewer-buttons{gap:5px}}
</style></head><body data-gallery-key="__GALLERY_KEY__" data-gallery-user="__USER__">
<header><div class="container"><div class="eyebrow">Personal chat reference</div><h1>Emoticon lookup</h1><p><strong>__USER__</strong> · room __ROOM__</p><div class="stats"><div class="stat"><strong>__TOTAL__</strong>distinct names</div><div class="stat"><strong>__GOOD__</strong>previews</div><div class="stat"><strong>__OCCURRENCES__</strong>occurrences</div></div></div></header>
__SAMPLE__
<main class="container"><div class="toolbar"><label class="control search">Find an emoticon<input id="search" type="search" placeholder="Search :name…" autocomplete="off"></label><label class="control">Sort<select id="sort"><option value="name">Name A–Z</option><option value="frequency">Most used</option></select></label><label class="control">Show<select id="filter"><option value="all">All names</option><option value="ok">Available previews</option><option value="unresolved">Unresolved names</option></select></label><label class="control">Group view<select id="group-view"><option value="all">All emoticons</option><option value="grouped">Grouped by rules</option><option value="ungrouped">Ungrouped</option></select></label><label class="toggle"><input id="animate" type="checkbox" checked>Animate previews</label></div>
<details id="group-panel" class="group-panel"><summary>Manage keyword groups</summary><p class="group-help">Create a group and enter one regex per line, such as <code>hello|wave</code> or <code>^et</code>. Any matching rule adds a name to the group. Names can belong to several groups. Matches ignore case unless you select case sensitive.</p><div class="group-actions"><button id="add-group" type="button" class="action">Create group</button><button id="export-groups" type="button" class="action secondary">Export groups</button><button id="import-groups" type="button" class="action secondary">Import groups</button><button id="save-gallery" type="button" class="action secondary">Save portable copy</button><input id="group-file" type="file" accept=".json,application/json" hidden></div><p id="group-storage" class="group-save-note" role="status"></p><div id="group-list" class="group-list"></div><p id="import-error" class="group-error" role="alert"></p></details>
<div class="results"><span id="result-count" aria-live="polite"></span><span>Click an image for full size. Copy a name to paste into chat.</span></div><div id="grid" class="grid">__CARDS__</div><div id="empty" class="empty">No emoticons match this search, filter, or group.</div></main>
<footer><div class="container">Saved __GENERATED__. Images are embedded in this file and work offline. Original media links require internet.<br>__SOURCE_NOTE__</div></footer><div id="toast" class="toast" role="status" aria-live="polite"></div>
<dialog id="media-viewer" class="viewer" aria-labelledby="viewer-name"><div class="viewer-toolbar"><p class="viewer-name"><code id="viewer-name"></code><span id="viewer-size" class="viewer-size"></span></p><div class="viewer-buttons"><button id="viewer-fit" type="button">Fit to screen</button><button id="viewer-close" type="button" autofocus>Close</button></div></div><div class="viewer-stage" tabindex="0" aria-label="Full-size media; scroll if larger than the screen"><img id="viewer-image" alt=""></div></dialog>
<script id="saved-groups" type="application/json">{"version":1,"updatedAt":0,"groups":[],"view":"all"}</script>
<script>
const $=id=>document.getElementById(id);
const cards=[...document.querySelectorAll('.card')], grid=$('grid'), search=$('search'), sort=$('sort'), filter=$('filter'), animate=$('animate'), groupView=$('group-view'), toast=$('toast');
const originals=new Map(cards.map(card=>[card.dataset.name,card.querySelector('.visual img')?.src]));
const storageKey='emoticon-lookup:groups:v1:'+document.body.dataset.galleryKey;
let timer, groups=[], updatedAt=0, memberships=new Map(), restoredView='all';const editorDrafts=new Map();
sort.value='__SORT__';
function notify(message){toast.textContent=message;toast.classList.add('visible');clearTimeout(timer);timer=setTimeout(()=>toast.classList.remove('visible'),2400)}
function element(tag,className,text){const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=text;return node}
function compileRules(rules,caseSensitive){return rules.map(rule=>new RegExp(rule,caseSensitive?'':'i'))}
function normalizeSnapshot(value){
  if(!value||value.version!==1||!Array.isArray(value.groups))throw new Error('Expected a version 1 groups export.');
  if(value.groups.length>100)throw new Error('A gallery supports up to 100 groups.');
  const ids=new Set();
  const normalized=value.groups.map((group,index)=>{
    if(!group||typeof group.name!=='string'||!group.name.trim()||!Array.isArray(group.rules)||group.rules.some(rule=>typeof rule!=='string'))throw new Error('Each group needs a name and an array of regex strings.');
    const rules=group.rules.map(rule=>rule.trim()).filter(Boolean);
    if(rules.length>100||rules.some(rule=>rule.length>1000))throw new Error('Use up to 100 rules per group and up to 1000 characters per rule.');
    compileRules(rules,Boolean(group.caseSensitive));
    let id=typeof group.id==='string'&&group.id?group.id:'import-'+index;
    while(ids.has(id))id+='-copy';ids.add(id);
    return {id,name:group.name.trim(),rules,caseSensitive:Boolean(group.caseSensitive)};
  });
  return {version:1,updatedAt:Number.isFinite(value.updatedAt)?value.updatedAt:0,groups:normalized,view:typeof value.view==='string'?value.view:'all'};
}
function snapshot(){return {version:1,updatedAt,groups,view:groupView.value}}
function persist(){
  updatedAt=Date.now();
  try{localStorage.setItem(storageKey,JSON.stringify(snapshot()));$('group-storage').textContent='Groups saved in this browser. Use Save portable copy to include them in a movable HTML file.'}
  catch(error){$('group-storage').textContent='Browser storage is unavailable. Use Save portable copy or Export groups to keep your changes.'}
}
function rebuildMemberships(){
  const compiled=groups.map(group=>({id:group.id,rules:compileRules(group.rules,group.caseSensitive)}));
  memberships=new Map(cards.map(card=>[card.dataset.name,new Set(compiled.filter(group=>group.rules.some(rule=>rule.test(card.dataset.name))).map(group=>group.id))]));
}
function renderGroupOptions(preferred=groupView.value){
  groupView.replaceChildren();
  for(const [value,label] of [['all','All emoticons'],['grouped','Grouped by rules'],['ungrouped','Ungrouped'],...groups.map(group=>['group:'+group.id,group.name])]){
    const option=element('option','',label);option.value=value;groupView.appendChild(option);
  }
  groupView.value=[...groupView.options].some(option=>option.value===preferred)?preferred:'all';
}
function renderEditors(focusId){
  const list=$('group-list');list.replaceChildren();
  if(!groups.length)list.appendChild(element('p','group-help','No groups yet. Create one to start organizing names.'));
  for(const group of groups){
    const draft=editorDrafts.get(group.id);
    const form=element('form','group-editor');form.dataset.groupId=group.id;
    const nameLabel=element('label','','Group name'), name=element('input');name.type='text';name.required=true;name.maxLength=150;name.value=draft?draft.name:group.name;nameLabel.appendChild(name);
    const rulesLabel=element('label','','Regex rules (one per line)'), rules=element('textarea');rules.rows=3;rules.placeholder='hello|wave\\n^et';rules.value=draft?draft.rules:group.rules.join('\\n');rulesLabel.appendChild(rules);
    const actions=element('div','group-editor-actions'), caseLabel=element('label','toggle'), caseBox=element('input');caseBox.type='checkbox';caseBox.checked=draft?draft.caseSensitive:group.caseSensitive;caseLabel.append(caseBox,document.createTextNode('Case sensitive'));
    const save=element('button','action','Save group');save.type='submit';const remove=element('button','action secondary','Delete group');remove.type='button';
    const count=element('span','group-save-note',cards.filter(card=>memberships.get(card.dataset.name).has(group.id)).length+' matching names');
    const dirty=element('span','group-save-note',draft?'Unsaved changes':'');dirty.dataset.state=draft?'unsaved':'saved';
    const error=element('div','group-error');error.setAttribute('role','alert');error.style.gridColumn='1 / -1';
    actions.append(caseLabel,save,remove,count,dirty);form.append(nameLabel,rulesLabel,actions,error);list.appendChild(form);
    const markDirty=()=>{editorDrafts.set(group.id,{name:name.value,rules:rules.value,caseSensitive:caseBox.checked});dirty.dataset.state='unsaved';dirty.textContent='Unsaved changes'};
    name.addEventListener('input',markDirty);rules.addEventListener('input',markDirty);caseBox.addEventListener('change',markDirty);
    form.addEventListener('submit',event=>{
      event.preventDefault();error.textContent='';
      try{
        const candidate=normalizeSnapshot({version:1,groups:[{...group,name:name.value,rules:rules.value.split(/\\r?\\n/),caseSensitive:caseBox.checked}]}).groups[0];
        groups=groups.map(item=>item.id===group.id?candidate:item);editorDrafts.delete(group.id);rebuildMemberships();renderGroupOptions();persist();renderEditors();update();notify('Saved '+candidate.name);
      }catch(problem){error.textContent='Could not save group: '+problem.message}
    });
    remove.addEventListener('click',()=>{groups=groups.filter(item=>item.id!==group.id);editorDrafts.delete(group.id);rebuildMemberships();renderGroupOptions();persist();renderEditors();update()});
    if(group.id===focusId){name.focus();name.select()}
  }
}
function update(){
  const query=search.value.trim().replace(/^:/,'').toLowerCase(), view=groupView.value;
  const ordered=[...cards].sort((a,b)=>sort.value==='frequency'?(Number(b.dataset.count)-Number(a.dataset.count)||a.dataset.name.localeCompare(b.dataset.name)):a.dataset.name.localeCompare(b.dataset.name));
  const filtered=ordered.filter(card=>card.dataset.name.toLowerCase().includes(query)&&(filter.value==='all'||(filter.value==='ok'?card.dataset.status==='ok':card.dataset.status!=='ok')));
  const used=new Set();grid.replaceChildren();grid.classList.toggle('grouped-layout',view==='grouped');
  function appendCard(parent,card){const node=used.has(card)?card.cloneNode(true):card;used.add(card);node.hidden=false;const img=node.querySelector('.visual img');if(img)img.src=animate.checked?originals.get(card.dataset.name):img.dataset.poster;parent.appendChild(node)}
  function section(name,items,total){if(!items.length)return;const wrapper=element('section','group-section'), heading=element('h2','',name), count=element('span','',items.length+' of '+total+' names'), content=element('div','group-grid');heading.appendChild(count);for(const card of items)appendCard(content,card);wrapper.append(heading,content);grid.appendChild(wrapper)}
  if(view==='grouped'){
    for(const group of groups)section(group.name,filtered.filter(card=>memberships.get(card.dataset.name).has(group.id)),cards.filter(card=>memberships.get(card.dataset.name).has(group.id)).length);
    section('Ungrouped',filtered.filter(card=>!memberships.get(card.dataset.name).size),cards.filter(card=>!memberships.get(card.dataset.name).size).length);
  }else{
    for(const card of filtered){const membership=memberships.get(card.dataset.name);if(view==='ungrouped'&&membership.size)continue;if(view.startsWith('group:')&&!membership.has(view.slice(6)))continue;appendCard(grid,card)}
  }
  $('result-count').textContent=used.size+' of '+cards.length+' names'+(view==='grouped'?' · names may appear in several groups':'');$('empty').classList.toggle('show',used.size===0);
}
function downloadFile(data,type,filename){const url=URL.createObjectURL(new Blob([data],{type})), link=element('a');link.href=url;link.download=filename;document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),10000)}
function safeFilename(){return (document.body.dataset.galleryUser||'emoticons').replace(/[^a-zA-Z0-9._-]+/g,'_')}
$('add-group').addEventListener('click',()=>{
  if(groups.length>=100){notify('This gallery already has 100 groups.');return}
  const id=globalThis.crypto?.randomUUID?.()||'group-'+Date.now()+'-'+Math.random().toString(36).slice(2);
  groups.push({id,name:'New group',rules:[],caseSensitive:false});rebuildMemberships();renderGroupOptions('group:'+id);persist();renderEditors(id);update();$('group-panel').open=true;
});
$('export-groups').addEventListener('click',()=>downloadFile(JSON.stringify(snapshot(),null,2),'application/json',safeFilename()+'_groups.json'));
$('import-groups').addEventListener('click',()=>$('group-file').click());
$('group-file').addEventListener('change',async event=>{
  const file=event.target.files[0];if(!file)return;$('import-error').textContent='';
  try{
    if(file.size>1024*1024)throw new Error('Groups files must be smaller than 1 MB.');
    const imported=normalizeSnapshot(JSON.parse(await file.text()));
    const ids=new Set(groups.map(group=>group.id));
    for(const group of imported.groups){while(ids.has(group.id))group.id+='-copy';ids.add(group.id)}
    const combined=normalizeSnapshot({version:1,groups:[...groups,...imported.groups]});groups=combined.groups;
    rebuildMemberships();renderGroupOptions('grouped');persist();renderEditors();update();notify('Imported '+imported.groups.length+' groups');
  }catch(problem){$('import-error').textContent='Could not import groups: '+problem.message}
  event.target.value='';
});
function portableHtml(){
  function cloneForExport(node){
    if(node.nodeType!==1)return node.cloneNode(true);
    const clone=node.cloneNode(false);
    if(node.id==='grid'){
      clone.className='grid';
      for(const card of cards){const copy=card.cloneNode(true);copy.hidden=false;const img=copy.querySelector('.visual img');if(img)img.src=originals.get(card.dataset.name);clone.appendChild(copy)}
      return clone;
    }
    if(node.id==='saved-groups'){clone.textContent=JSON.stringify(snapshot()).replace(/</g,()=>String.fromCharCode(92)+'u003c');return clone}
    if(node.id==='viewer-image'){clone.removeAttribute('src');return clone}
    if(node.id==='group-list')return clone;
    if(node.id==='media-viewer'){clone.removeAttribute('open');clone.className='viewer'}
    if(node.id==='toast')clone.className='toast';
    if(node.id==='group-panel')clone.removeAttribute('open');
    if(node.tagName==='BODY')clone.style.overflow='';
    for(const child of node.childNodes)clone.appendChild(cloneForExport(child));
    return clone;
  }
  return '<!doctype html>\\n'+cloneForExport(document.documentElement).outerHTML;
}
$('save-gallery').addEventListener('click',()=>{persist();downloadFile(portableHtml(),'text/html;charset=utf-8',safeFilename()+'_emoticons_grouped.html');notify('Saved portable gallery with group rules')});
const viewer=$('media-viewer'), viewerImage=$('viewer-image');let returnFocus=null, previousOverflow='';
function openViewer(card,trigger){
  const source=originals.get(card.dataset.name);if(!source)return;
  returnFocus=trigger;viewer.classList.remove('fit');$('viewer-fit').textContent='Fit to screen';$('viewer-name').textContent=':'+card.dataset.name;$('viewer-size').textContent=' · Original size (100%)';viewerImage.alt=':'+card.dataset.name;viewerImage.src=source;
  viewerImage.onload=()=>{$('viewer-size').textContent=' · '+viewerImage.naturalWidth+' × '+viewerImage.naturalHeight+' px · '+(viewer.classList.contains('fit')?'Fit to screen':'Original size (100%)')};
  viewer.showModal();previousOverflow=document.body.style.overflow;document.body.style.overflow='hidden';viewer.querySelector('.viewer-stage').scrollTo(0,0);
}
$('viewer-close').addEventListener('click',()=>viewer.close());
viewer.addEventListener('click',event=>{if(event.target===viewer)viewer.close()});
viewer.addEventListener('close',()=>{viewerImage.removeAttribute('src');viewerImage.onload=null;document.body.style.overflow=previousOverflow;returnFocus?.focus()});
$('viewer-fit').addEventListener('click',()=>{const fit=viewer.classList.toggle('fit');$('viewer-fit').textContent=fit?'Original size':'Fit to screen';$('viewer-size').textContent=' · '+viewerImage.naturalWidth+' × '+viewerImage.naturalHeight+' px · '+(fit?'Fit to screen':'Original size (100%)')});
grid.addEventListener('click',async event=>{
  const preview=event.target.closest('.preview-open'), button=event.target.closest('.copy'), card=event.target.closest('.card');if(!card)return;
  if(preview){openViewer(card,preview);return}if(!button)return;
  const name=':'+card.dataset.name;let copied=false;
  try{await navigator.clipboard.writeText(name);copied=true}catch(error){const box=element('textarea');box.value=name;box.style.cssText='position:fixed;left:-9999px';document.body.appendChild(box);box.select();try{copied=document.execCommand('copy')}catch(problem){}box.remove()}
  notify(copied?'Copied '+name:'Select the name on the card to copy it');
});
search.addEventListener('input',update);sort.addEventListener('change',update);filter.addEventListener('change',update);
groupView.addEventListener('change',()=>{persist();update()});
animate.addEventListener('change',()=>{for(const card of grid.querySelectorAll('.card')){const img=card.querySelector('.visual img');if(img)img.src=animate.checked?originals.get(card.dataset.name):img.dataset.poster}});
function restore(){
  let embedded=null,stored=null;
  try{embedded=normalizeSnapshot(JSON.parse($('saved-groups').textContent))}catch(problem){$('import-error').textContent='Embedded groups could not be restored: '+problem.message}
  try{const text=localStorage.getItem(storageKey);if(text)stored=normalizeSnapshot(JSON.parse(text))}catch(problem){}
  const selected=stored&&(!embedded||stored.updatedAt>=embedded.updatedAt)?stored:embedded;
  if(selected){groups=selected.groups;updatedAt=selected.updatedAt;restoredView=selected.view}
  rebuildMemberships();renderGroupOptions(restoredView);renderEditors();update();
  $('group-storage').textContent='Use Save group after editing rules. Save portable copy embeds your saved groups in the HTML.';
}
restore();
</script></body></html>'''
    values = {
        "__USER__": escape(user), "__ROOM__": escape(room), "__TOTAL__": str(len(rows)),
        "__GOOD__": str(good), "__OCCURRENCES__": str(sum(row["count"] for row in rows)),
        "__GALLERY_KEY__": sha256((user.casefold() + "\\0" + room.casefold()).encode("utf-8")).hexdigest(),
        "__CARDS__": "\n".join(cards), "__SORT__": sort,
        "__GENERATED__": escape(datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")),
        "__SAMPLE__": '<div class="sample"><div class="container">EXAMPLE · Demonstration data; this is not user history.</div></div>' if sample else "",
        "__SOURCE_NOTE__": "Example input; not user history." if sample else "Names were extracted from chatMessage and privateMessage records. Unresolved names are kept for reference.",
    }
    # Single-pass substitution prevents user strings from being treated as template tokens.
    html = re.sub(r"__[A-Z_]+__", lambda match: values[match.group()], template)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


def pdf_fonts():
    # Embed an available Unicode font, falling back to the standard PDF fonts.
    pairs = [
        (Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/arial.ttf",
         Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/arialbd.ttf"),
        (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
         Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")),
        (Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
         Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf")),
    ]
    for regular, bold in pairs:
        if regular.exists() and bold.exists():
            pdfmetrics.registerFont(TTFont("Lookup", str(regular)))
            pdfmetrics.registerFont(TTFont("LookupBold", str(bold)))
            return "Lookup", "LookupBold"
    return "Helvetica", "Helvetica-Bold"


def wrap_text(text, font, size, width):
    lines, line = [], ""
    for char in text:
        if line and pdfmetrics.stringWidth(line + char, font, size) > width:
            lines.append(line)
            line = ""
        line += char
    if line:
        lines.append(line)
    return lines or [""]


def write_pdf(path, rows, user, room, sample=False, sort="name"):
    regular, bold = pdf_fonts()
    width, height = letter
    margin, gap, columns, per_page = 36, 10, 3, 15
    card_width = (width - 2 * margin - 2 * gap) / columns
    card_height = 122
    pages = max(1, math.ceil(len(rows) / per_page))
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path), pagesize=letter)
    pdf.setTitle(f"Emoticon lookup - {user}")
    pdf.setAuthor("Emoticon Lookup")
    good = sum(row["status"] == "ok" for row in rows)
    generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    for page in range(pages):
        pdf.setFillColor(colors.HexColor("#183a48"))
        pdf.setFont(bold, 22)
        pdf.drawString(margin, height - 53, "Emoticon lookup")
        pdf.setFont(regular, 11)
        title = f"{'API EXAMPLE - ' if sample else ''}{user}  |  room: {room}"
        for i, line in enumerate(wrap_text(title, regular, 11, width - 2 * margin)[:2]):
            pdf.drawString(margin, height - 74 - 13 * i, line)
        pdf.setFillColor(colors.HexColor("#52666d"))
        pdf.setFont(regular, 9)
        pdf.drawString(margin, height - 104, f"{len(rows)} distinct names  /  {good} previews  /  sorted by {sort}")
        pdf.drawString(margin, height - 119, "Selectable names. Click a preview to open media. Animations use a static frame.")
        section = rows[page * per_page:(page + 1) * per_page]
        if not section:
            pdf.setFont(regular, 12)
            pdf.drawString(margin, height - 172, "No emoticons matched this user and filter.")
        for index, row in enumerate(section):
            x = margin + (index % columns) * (card_width + gap)
            y = height - 142 - (index // columns + 1) * card_height - (index // columns) * gap
            pdf.setFillColor(colors.HexColor("#f5f7f8"))
            pdf.setStrokeColor(colors.HexColor("#dce3e6"))
            pdf.roundRect(x, y, card_width, card_height, 6, fill=1, stroke=1)
            if row["status"] == "ok":
                image = ImageReader(row["preview"])
                iw, ih = image.getSize()
                scale = min((card_width - 16) / iw, 76 / ih)
                dw, dh = iw * scale, ih * scale
                pdf.drawImage(image, x + (card_width - dw) / 2, y + 40 + (76 - dh) / 2,
                              dw, dh, mask="auto")
                pdf.linkURL(row["url"], (x, y + 40, x + card_width, y + card_height), relative=0)
            else:
                pdf.setFillColor(colors.HexColor("#697a80"))
                pdf.setFont(bold, 10)
                pdf.drawCentredString(x + card_width / 2, y + 86, "Preview unavailable")
                pdf.setFont(regular, 8)
                status = "No exact API match" if row["status"] == "missing" else "Fetch/decode failed"
                pdf.drawCentredString(x + card_width / 2, y + 70, status)
                pdf.linkURL(API + "?" + urlencode({"slug": row["slug"], "room": room}),
                            (x, y + 40, x + card_width, y + card_height), relative=0)
            pdf.setFillColor(colors.HexColor("#183a48"))
            name = ":" + row["slug"]
            size = 10
            while size > 5 and len(wrap_text(name, bold, size, card_width - 16)) > 3:
                size -= 0.5
            lines = wrap_text(name, bold, size, card_width - 16)
            pdf.setFont(bold, size)
            for i, line in enumerate(lines):
                pdf.drawString(x + 8, y + 31 - i * (size + 1), line)
            pdf.setFillColor(colors.HexColor("#52666d"))
            pdf.setFont(regular, 7)
            label = f"{row['count']} occurrence{'s' if row['count'] != 1 else ''}"
            if row.get("animated"):
                label += "  |  animated"
            pdf.drawString(x + 8, y + 7, label)
        pdf.setFont(regular, 7)
        pdf.setFillColor(colors.HexColor("#52666d"))
        pdf.drawString(margin, 24, "Example input; not user history" if sample else f"Generated {generated}")
        pdf.drawRightString(width - margin, 24, f"{page + 1} / {pages}")
        pdf.showPage()
    pdf.save()


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user", default="socksandsmiles", help="Exact username, case insensitive")
    parser.add_argument("--room", default="rndmzd", help="Room used for API resolution")
    parser.add_argument("--database", default="mongobate")
    parser.add_argument("--collection", default="events")
    parser.add_argument("--user-field", default="object.user.username")
    parser.add_argument("--message-field", default="object.message.message")
    parser.add_argument("--input", type=Path, help="JSON message/event/grouped-result export instead of MongoDB")
    parser.add_argument("--output", type=Path, default=Path("emoticons.html"), help=".html for portable animations; .pdf for static previews")
    parser.add_argument("--cache-dir", type=Path, default=Path(".emoticon-cache"))
    parser.add_argument("--starts-only", action="store_true", help="Only messages starting with :name, like the supplied pipeline")
    parser.add_argument("--sort", choices=["name", "frequency"], default="name")
    parser.add_argument("--refresh", action="store_true", help="Resolve/download again rather than use successful cached entries")
    parser.add_argument("--delay", type=float, default=0.25, help="Pause between distinct name lookups in seconds")
    parser.add_argument("--query-timeout", type=int, default=300, help="MongoDB query time limit in seconds")
    parser.add_argument("--sample", action="store_true", help="Label the output as an example, not user history")
    args = parser.parse_args()
    if args.delay < 0 or args.query_timeout < 1:
        parser.error("--delay must be nonnegative and --query-timeout must be positive")
    if args.output.suffix.lower() not in (".pdf", ".html"):
        parser.error("--output must have a .html or .pdf extension")
    return args


def main():
    args = parse_args()
    try:
        counts = collect(args)
        print(f"Found {len(counts)} distinct names ({sum(counts.values())} occurrences).")
        args.cache_dir.mkdir(parents=True, exist_ok=True)
        rows = []
        with http_session() as session:
            session.headers["Referer"] = f"https://chaturbate.com/{args.room}/"
            names = sorted(counts, key=lambda name: (-counts[name], name.casefold(), name)) if args.sort == "frequency" else sorted(counts, key=lambda name: (name.casefold(), name))
            for index, name in enumerate(names):
                row = resolve(session, name, args.room, args.cache_dir, args.refresh)
                row["count"] = counts[name]
                rows.append(row)
                print(f"[{index + 1}/{len(names)}] :{name} - {row['status']}")
                if index + 1 < len(names):
                    time.sleep(args.delay)
        if args.output.suffix.lower() == ".html":
            write_html(args.output, rows, args.user, args.room, args.sample, args.sort)
        else:
            write_pdf(args.output, rows, args.user, args.room, args.sample, args.sort)
        manifest = args.output.with_suffix(".json")
        manifest.write_text(json.dumps({
            "user": args.user, "room": args.room, "sample": args.sample,
            "generated_at": datetime.now().astimezone().isoformat(),
            "sort": args.sort, "starts_only": args.starts_only, "emoticons": rows,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Lookup: {args.output.resolve()}")
        print(f"Manifest: {manifest.resolve()}")
        missing = sum(row["status"] != "ok" for row in rows)
        if missing:
            print(f"{missing} unresolved name(s) retained in lookup; see manifest details.", file=sys.stderr)
        return 0
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
