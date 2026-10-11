"""Extract every EPUB from a Kiwix Gutenberg ZIM into a staging directory.

Uses PuffPAI's own zim_reader (no libzim). Output layout:
  <out>/<bucket>/<gutenberg_id>.epub      bucket = id // 1000, keeps dirs small
Writes <out>/manifest.tsv (id, title, bytes, zim_path) and is resumable:
existing files with the expected size are skipped.
"""
import os, re, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'src'))
from puffpai.knowledge.zim_reader import ZIMReader

zim, out = sys.argv[1], sys.argv[2]
os.makedirs(out, exist_ok=True)
r = ZIMReader(zim); h = r._header
ID_RE = re.compile(r'\.(\d+)\.epub$')
man = open(os.path.join(out, 'manifest.tsv'), 'a', encoding='utf-8')
n = written = skipped = bad = 0; t0 = time.time(); total_bytes = 0
for i in range(h.entry_count):
    e = r._read_directory_entry(i)
    if e.is_redirect or not (e.mime_type or '').startswith('application/epub'):
        continue
    n += 1
    m = ID_RE.search(e.url)
    gid = m.group(1) if m else f"noid_{i}"
    title = e.url[:-(len(m.group(0)) if m else 5)]
    bucket = os.path.join(out, str(int(gid) // 1000) if m else 'noid')
    os.makedirs(bucket, exist_ok=True)
    dest = os.path.join(bucket, f"{gid}.epub")
    a = r.get_article(i)
    if a is None or not a.content.startswith(b'PK'):
        bad += 1; continue
    if os.path.exists(dest) and os.path.getsize(dest) == len(a.content):
        skipped += 1; continue
    with open(dest, 'wb') as f: f.write(a.content)
    written += 1; total_bytes += len(a.content)
    man.write(f"{gid}\t{title}\t{len(a.content)}\t{e.url}\n")
    if written % 2000 == 0:
        print(f"  {written:,} written, {skipped:,} skipped, {total_bytes/1e9:.1f} GB, {time.time()-t0:.0f}s", flush=True)
man.close(); r.close()
print(f"DONE epub entries={n:,} written={written:,} skipped={skipped:,} bad={bad} bytes={total_bytes/1e9:.2f} GB in {time.time()-t0:.0f}s")
