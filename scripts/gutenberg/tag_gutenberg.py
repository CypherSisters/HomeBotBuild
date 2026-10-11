import sys
from calibre.library import db
lib = sys.argv[1]
d = db(lib).new_api
ids = [i for i in d.all_book_ids() if "gutenberg.org" in (d.field_for("identifiers", i) or {}).get("url", "")]
upd = {}
for i in ids:
    tags = list(d.field_for("tags", i) or ())
    if "Project Gutenberg" not in tags:
        upd[i] = tags + ["Project Gutenberg"]
if upd:
    d.set_field("tags", upd)
print("gutenberg books:", len(ids), "tagged now:", len(upd))
