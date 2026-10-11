/**
 * Where /dashboard/media/books points Calibre-web.
 *
 * The backend answers "show me <book>" / "show me <comic>" by queuing
 * /dashboard/media/books?book=<calibre id>&fmt=<epub|cbr|cbz|pdf>, and the
 * overlay frames that route. This module is the gate between that query string
 * — which arrives from the brain, from a voice transcript, ultimately from
 * anything that can hit the queue — and an iframe src.
 *
 * Calibre-web's shape:
 *   /book/<id>              the detail page (covers, formats, metadata)
 *   /read/<id>/<format>     the in-browser reader, format lower-case
 *   /                       the whole catalogue
 *
 * Validation is allow-list only: `book` must parse as a plain positive integer
 * and `fmt` must be one of the formats Calibre-web's reader actually serves.
 * Nothing is escaped or sanitised — a value that is not exactly what we expect
 * is DROPPED, so "1/../../admin" or "epub?x=1" degrade to the catalogue rather
 * than to a crafted path. Never throws.
 *
 * puff puff pass
 */

/** Formats Calibre-web has an in-browser reader for. Lower-case, no dots. */
const READABLE = new Set(["epub", "cbr", "cbz", "cb7", "pdf", "txt", "djvu"]);

/** Calibre ids are row ids in the library database; the ceiling is a sanity
 *  bound, not a protocol limit. */
const MAX_BOOK_ID = 100_000_000;

/** Digits only: no sign, no decimal point, no whitespace, no "1e3". Anything
 *  Number() would happily coerce but a path should not contain. */
const DIGITS = /^[0-9]+$/;

function bookId(raw: string | null | undefined): number | null {
  if (typeof raw !== "string") return null;
  if (!DIGITS.test(raw)) return null;
  const id = Number(raw);
  if (!Number.isSafeInteger(id) || id < 1 || id > MAX_BOOK_ID) return null;
  return id;
}

function format(raw: string | null | undefined): string | null {
  if (typeof raw !== "string") return null;
  const fmt = raw.trim().toLowerCase();
  return READABLE.has(fmt) ? fmt : null;
}

/**
 * Query params → a Calibre-web path, always starting with "/".
 *
 * book + fmt  => /read/<id>/<fmt>   (straight into the reader)
 * book only   => /book/<id>         (the detail page; she picks a format)
 * otherwise   => /                  (the catalogue, the page as it was)
 *
 * An invalid fmt does not poison a valid book: it falls back to the detail
 * page rather than to the catalogue.
 */
export function calibreTargetPath(params: {
  book?: string | null;
  fmt?: string | null;
}): string {
  const id = bookId(params?.book);
  if (id === null) return "/";
  const fmt = format(params?.fmt);
  return fmt ? `/read/${id}/${fmt}` : `/book/${id}`;
}
