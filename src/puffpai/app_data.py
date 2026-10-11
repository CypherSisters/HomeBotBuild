"""
PuffPAI Application Data Manager
Shared data layer for HTML applications (AlarmFlow, Calendar, FreshTable, FitFam, Trivia).
Each app has a JSON file in app_data/ that both PuffPAI tools and HTML apps read/write.
"""

import json
import re
import os
import time
import random
import string
import threading
from datetime import datetime, timedelta

from puffpai import statedir
from puffpai import calendar_dates as _caldates

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: Shipped default for the app-data directory — ``<repo>/app_data``, exactly
#: the expression this replaced. Used when ``$PUFFPAI_DATA_DIR`` is unset.
_DEFAULT_DATA_DIR = os.path.join(_REPO_ROOT, "app_data")

#: Applications/ holds the shipped HTML apps. That is CODE, not runtime state,
#: so it deliberately does NOT follow ``$PUFFPAI_DATA_DIR`` — a hardened unit
#: mounts the code tree read-only on purpose and the launcher must keep
#: pointing at it. (It also resolves one level ABOVE the repo, a pre-existing
#: layout choice left untouched here.)
_APPS_DIR = os.path.join(os.path.dirname(_REPO_ROOT), "Applications")

#: Resolved app-data directory; ``None`` = not resolved yet. Module-level, so
#: ``importlib.reload`` resets it and a test can re-resolve after changing the
#: environment.
_data_dir_cache = None


def _data_dir():
    """Directory holding the HTML apps' JSON state, resolved on FIRST USE.

    Lazy on purpose: :func:`puffpai.statedir.resolve_runtime_dir` defers its
    ``puffpai.config`` import to call time, and calling it while this module is
    still being imported would drag config (and the secrets provider it pulls
    in) back into the import graph — the cycle risk documented in
    :mod:`puffpai.agent_events`.
    """
    global _data_dir_cache
    if _data_dir_cache is None:
        _data_dir_cache = statedir.resolve_runtime_dir(
            _DEFAULT_DATA_DIR, env_subdir="app_data")
    return _data_dir_cache


def _grocery_vendor():
    """The household's grocery vendor as ``(store_name, search_url_template)``.

    Nothing ships a vendor: a store link would say where the household shops,
    and a shipped default would say it for everyone. Both values are empty
    unless the household set ``grocery_store_name`` and ``grocery_search_url``
    in its configuration; the URL is a template whose ``{q}`` is replaced with
    the URL-encoded item name. Read defensively — a configuration that does not
    carry the keys (the shipped one does not) yields ``("", "")`` rather than
    raising.
    """
    try:
        from puffpai.config import get_config
        cfg = get_config()
    except Exception:
        return "", ""
    name = getattr(cfg, "grocery_store_name", "")
    url = getattr(cfg, "grocery_search_url", "")
    return (str(name or "").strip(), str(url or "").strip())


class AppDataCorrupt(Exception):
    """An app's document exists and holds bytes, but could not be read.

    Raised instead of quietly returning the app's DEFAULT shape. That silent
    fallback was the whole bug: a corrupt or half-written file read as an
    empty household, the merge path's drop guard saw empty lists in
    ``current`` and had nothing to protect, and the next page PUT wrote that
    emptiness over the owner's real data. A document that cannot be read is
    not an empty document; nothing here rewrites the file, and the caller is
    told so it can say so.
    """

    def __init__(self, app, path, cause=None):
        self.app = app
        self.path = path
        self.cause = cause
        super().__init__(
            f"{app} data file is unreadable ({path}): {cause}")


#: Paths whose unreadability has already been logged, so a page polling every
#: few seconds cannot fill the journal with the same sentence.
_corrupt_logged = set()

#: One lock per DOCUMENT, shared by every ``AppData`` instance in the process.
#: Instances are constructed ad hoc all over the codebase (``AppData()`` inside
#: a tool, a fastpath, the scheduler), so a per-instance lock serialised each
#: caller against itself and against nobody else. Keyed by the resolved file
#: path, because two instances built from different roots are genuinely
#: different documents.
_file_locks = {}
_file_locks_guard = threading.Lock()


def _lock_for(path):
    """The process-wide lock for one app document path."""
    key = os.path.abspath(path)
    lock = _file_locks.get(key)
    if lock is None:
        with _file_locks_guard:
            lock = _file_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                _file_locks[key] = lock
    return lock


def _uid():
    """Generate a unique ID matching the HTML apps' convention."""
    ts = str(int(time.time() * 1000))
    rand = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{ts}{rand}"


def normalise_event_title(title) -> str:
    """Comparison key for "is this the same calendar item?".

    Case, punctuation, articles and doubled spaces are all noise when the
    same activity is dictated twice ("Take the kids to soccer practice." vs
    "take kids to soccer practice"). Returns "" for anything empty.
    """
    text = str(title or "").strip().lower()
    text = re.sub(r"[^\w\s]+", " ", text)
    text = re.sub(r"\b(?:a|an|the)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


#: Words that say what KIND of thing it is, or whose it is, rather than what
#: it IS. A speaker drops and adds them freely ("the soccer reminder" /
#: "the soccer practice"), so they carry no weight when a phrase is
#: matched against a stored title. "the"/"a"/"an" are already gone by then.
_TITLE_NOISE_WORDS = frozenset({
    'my', 'our', 'your', 'reminder', 'reminders', 'event', 'events',
    'task', 'tasks', 'todo', 'todos', 'appointment', 'appointments',
    'item', 'items', 'entry', 'entries', 'thing', 'things', 'one',
    'calendar', 'calender', 'schedule',
})


def _title_tokens(normalised: str) -> frozenset:
    """The words of an already-normalised title that actually name it."""
    return frozenset(word for word in (normalised or "").split()
                     if word not in _TITLE_NOISE_WORDS)


class AppData:
    """Unified CRUD layer for all PuffPAI HTML applications.

    Two sources, one store. An app that ships an ``apps/<id>/app.json``
    manifest is installed BY THAT FILE — its id, its data file and its starting
    shape all come from the manifest, and dropping the folder in and rescanning
    is the whole install mechanism (see :mod:`puffpai.app_registry`). The
    hardcoded lists below are the FALLBACK for everything else: they are what a
    build with no ``apps/`` directory runs on, and they still own ``mediahub``
    which is a core surface rather than an installable app.

    Where both exist the manifest wins for the fields it declares and the
    fallback fills the rest, so placing the five shipped manifests changes
    nothing an owner can observe — proven by ``tests/test_app_contract``.
    """

    #: The fallback app list: what this build carries when no manifest does.
    VALID_APPS = ['alarmflow', 'calendar', 'freshtable', 'fitfam', 'trivia',
                  'mediahub']

    def __init__(self):
        data_dir = _data_dir()
        os.makedirs(data_dir, exist_ok=True)
        self._files = {
            'alarmflow': os.path.join(data_dir, 'alarmflow.json'),
            'calendar': os.path.join(data_dir, 'calendar.json'),
            'freshtable': os.path.join(data_dir, 'freshtable.json'),
            'fitfam': os.path.join(data_dir, 'fitfam.json'),
            'trivia': os.path.join(data_dir, 'trivia.json'),
            'mediahub': os.path.join(data_dir, 'mediahub.json'),
        }
        self._defaults = {
            'alarmflow': lambda: {"items": [], "last_updated": ""},
            'calendar': lambda: {
                "family": [
                    {"id": "dad", "name": "Dad", "color": "#38bdf8", "emoji": "\U0001f468", "role": "parent"},
                    {"id": "mom", "name": "Mom", "color": "#a78bfa", "emoji": "\U0001f469", "role": "parent"},
                    {"id": "alex", "name": "Alex", "color": "#4ade80", "emoji": "\U0001f466", "role": "child"},
                    {"id": "emma", "name": "Emma", "color": "#fbbf24", "emoji": "\U0001f467", "role": "child"},
                ],
                "events": [], "tasks": [], "meds": [], "bills": [],
                "last_updated": ""
            },
            'freshtable': lambda: {
                "pantry": [], "shoppingList": [], "mealPlan": [],
                "recipes": [],
                "last_updated": ""
            },
            # FitFam ships with an EMPTY roster on purpose. A default is a
            # schema, not a household: members come from the data file,
            # created by whoever owns the box.
            'fitfam': lambda: {
                "users": {},
                "last_updated": ""
            },
            'trivia': lambda: {"results": [], "last_updated": ""},
            # Media hub: one doc per non-Jellyfin audio source. radio_command
            # is a one-slot queue, not a list — the dashboard player can only
            # be tuned to one station, so a second request supersedes the
            # first rather than piling up behind it.
            'mediahub': lambda: {
                "radio": {"status": "idle", "station": None, "updated_at": None},
                "radio_command": None,
                "spotify": {"active": False, "updated_at": None},
                "last_updated": "",
            },
        }

        #: Ids that came from a manifest rather than from the lists above.
        #: Kept so ``/apps`` can say which apps are installed as opposed to
        #: built in, and so a test can prove the fallback path still runs.
        self.manifest_apps = []
        self._merge_manifests(data_dir)
        #: Per-document locks, shared process-wide (see :func:`_lock_for`).
        #:
        #: PUBLISH ORDER IS LOAD-BEARING. ``VALID_APPS`` is what every reader
        #: checks before touching an app, so it is assigned LAST: `_files`,
        #: `_defaults` and `_locks` are all complete before anything can pass
        #: that check. Assigning `_locks` after it left a window in which an
        #: id was valid and its lock did not exist yet — a KeyError on a
        #: perfectly ordinary read.
        self._locks = {app: _lock_for(path)
                       for app, path in self._files.items()}
        #: The apps this instance actually serves: the fallback list plus
        #: anything a manifest installed. A LIST, not the class attribute, so
        #: an instance built after a rescan reflects the rescan.
        self.VALID_APPS = list(self._files.keys())

    def _merge_manifests(self, data_dir):
        """Fold the installed manifests into the file and default tables.

        The manifest is authoritative for what it declares and silent about
        what it does not: an app whose manifest carries no ``data.default``
        keeps the shipped shape from the table above (FitFam does exactly
        that, so the household's own seed users never had to move into a file
        the app store will one day distribute).

        Never raises. A registry that cannot be read leaves this build running
        on the hardcoded lists, which is the behaviour it had before manifests
        existed — the wrong outcome is a house whose alarms stop working
        because a JSON file three directories away is malformed.
        """
        try:
            from puffpai import app_registry
            manifests = app_registry.load_manifests()
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[AppData] app manifests unreadable, using built-in apps: {exc}")
            return

        for manifest in manifests:
            app_id = str(manifest.get('id') or '')
            if not app_registry.valid_id(app_id):
                continue
            try:
                path = app_registry.data_path(app_id, data_dir)
            except ValueError as exc:
                print(f"[AppData] app '{app_id}' skipped: {exc}")
                continue
            if app_id not in self._files:
                self.manifest_apps.append(app_id)
            self._files[app_id] = path
            declared = app_registry.default_data(manifest)
            if declared:
                # Bound to this manifest by value, and a FRESH copy per call:
                # handing out the manifest's own dict would let one app's first
                # write become every later owner's "default".
                self._defaults[app_id] = (
                    lambda m=manifest: app_registry.default_data(m))
            elif app_id not in self._defaults:
                self._defaults[app_id] = dict

    # ── Generic load/save ────────────────────────────────────────────────────

    def _load(self, app):
        """The app's document, or its default shape for a file that isn't there.

        A file that EXISTS and holds bytes but does not parse raises
        :class:`AppDataCorrupt` instead of falling back to the default.
        Falling back was silent data loss with extra steps: the empty
        default read as an empty household, the merge drop-guard saw empty
        lists in ``current`` and had nothing to protect, and the next page
        PUT overwrote the owner's real file with a partial document.
        Defaults are for a MISSING or EMPTY file, and nothing else.
        """
        path = self._files[app]
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    raw = f.read()
            except (IOError, OSError) as exc:
                raise AppDataCorrupt(app, path, exc) from exc
            if raw.strip():
                try:
                    return json.loads(raw)
                except (json.JSONDecodeError, ValueError) as exc:
                    if path not in _corrupt_logged:
                        _corrupt_logged.add(path)
                        print(f"[AppData] {app} data file will not parse "
                              f"and was NOT rewritten: {path}: {exc}")
                    raise AppDataCorrupt(app, path, exc) from exc
        return self._bootstrap(app)

    def _bootstrap(self, app):
        """First use of an app writes its data file, then returns the shape.

        Without this an app whose file nothing else creates (``trivia.json``,
        for one) works but has nowhere to land its data until the first
        write. This is generic rather than per-app: any app with a data
        file and no file on disk gets one, holding the shape its manifest (or
        the fallback table) declares.

        A file that exists but is corrupt is NOT overwritten — that is the
        owner's data and losing it silently would be worse than the fault. The
        write is best-effort: a read-only data directory degrades to the
        previous behaviour (defaults in memory) rather than failing the read.
        """
        default = self._defaults[app]()
        path = self._files[app]
        try:
            # Exclusive create: two readers racing on a cold box both bootstrap,
            # and the loser must not truncate the winner's file mid-write.
            # FileExistsError is the normal outcome of losing that race.
            with open(path, 'x', encoding='utf-8') as f:
                json.dump(default, f, indent=2, default=str,
                          ensure_ascii=False)
        except FileExistsError:
            pass
        except (IOError, OSError) as exc:
            print(f"[AppData] could not create {app} data file: {exc}")
        return default

    def _save(self, app, data):
        """The ONE place an app document is serialised to disk.

        Goes through :func:`puffpai.security.app_data_store.write_json_secure`
        so every app document lands atomically (temp + rename) at 0600 in a 0700
        directory. The previous ``open(path, 'w')`` truncated in place, which
        PRESERVED whatever mode the file was created with — that is why the two
        most sensitive documents on the appliance were also the loosest
        (calendar.json 0644, fitfam.json 0664) and why nothing in the write path
        would ever have corrected them.

        Imported lazily for the same reason :func:`_data_dir` is lazy: the
        security package pulls the logging config, and dragging it into this
        module's import graph reintroduces the cycle documented above.
        """
        data['last_updated'] = datetime.now().isoformat()
        from puffpai.security.app_data_store import write_json_secure
        write_json_secure(self._files[app], data,
                          indent=2, default=str, ensure_ascii=False)
        # Tell the served turn (if any) what it just KEPT, so a reply can never
        # claim the opposite. Deliberately AFTER the write and swallowing its
        # own errors: this is an honesty aid, not a gate, and it must never be
        # the reason a household loses a calendar entry. See
        # puffpai.security.turn_writes for why the record is per-thread.
        try:
            from puffpai.security import turn_writes
            turn_writes.note_write(app, data)
        except Exception:
            pass
        # Second, narrower record: did this document end up holding the turn's
        # health WORDS? calendar.tasks[] is not a tagged health position, so the
        # class record above cannot answer for a reminder whose title is the
        # sentence the user said. Same failure discipline; a no-op on every turn
        # that has no health terms. See puffpai.security.turn_health.
        try:
            from puffpai.security import turn_health
            turn_health.note_write(app, data)
        except Exception:
            pass

    def get_data(self, app):
        """Get full data for an app (used by REST API)."""
        if app not in self.VALID_APPS:
            return None
        with self._locks[app]:
            data = self._load(app)
        if app == 'calendar' and 'meds_vault_pending' in data:
            # A vaulted medication's name never rides out on the REST
            # payload. The med rows themselves carry only the label, so the
            # page and every API caller see exactly what the voice does.
            data = {k: v for k, v in data.items()
                    if k != 'meds_vault_pending'}
        return data

    def data_path(self, app):
        """On-disk path of an app's document, or ``None`` for an unknown app."""
        return self._files.get(app)

    def set_data(self, app, data, merge=False, allow_shrink=False, backup=False,
                 recover=False):
        """Write a full app document.

        ``merge=False`` is the historical whole-document replace and stays the
        default so internal read-modify-write callers are unchanged. The REST
        PUT asks for ``merge=True``, which is the safe shape for a browser
        push: keys the payload does not carry are PRESERVED (FreshTable's page
        never sends ``recipes``, and a replace would delete them), ``None``
        deletes a key explicitly, ``data_version`` is bumped, and the previous
        document is rotated into ``backups/``.

        Raises :class:`AppDataCorrupt` when the document on disk exists,
        holds bytes and does not decode — on BOTH the merge and the replace
        path, because a write is exactly the thing that would destroy it.
        ``recover=True`` is the owner's explicit "overwrite it anyway".

        Raises :class:`~puffpai.app_data_merge.AppDataWouldDropData` when a
        merge would empty or more than halve a top-level list that had
        content, unless ``allow_shrink`` says the deletion is intended.
        """
        if app not in self.VALID_APPS:
            return False
        if not merge:
            with self._locks[app]:
                if not recover:
                    # A replace does not otherwise read the document, so
                    # without this it is the ONE path that could write over
                    # a file we could not decode. Raises AppDataCorrupt.
                    self._load(app)
                if app == 'calendar' and 'meds_vault_pending' not in (data or {}):
                    # A write-back of a payload that never saw the vaulted
                    # names (get_data strips them) must not drop them off disk.
                    held = self._load(app).get('meds_vault_pending')
                    if held:
                        data = dict(data or {}, meds_vault_pending=held)
                self._save(app, data)
            return True

        from puffpai import app_data_merge
        with self._locks[app]:
            # Raises AppDataCorrupt on an undecodable document, unless the
            # caller has explicitly asked to overwrite it (recover), in
            # which case there is nothing to merge INTO and the payload
            # stands on its own.
            try:
                current = self._load(app)
            except AppDataCorrupt:
                if not recover:
                    raise
                current = self._defaults[app]()
            if app == 'calendar' and isinstance(data, dict) and data.get('meds_vault_pending', 0) is None:
                # A REST client never saw the vaulted names, so it may not
                # delete them with an explicit null; merge_document already
                # preserves the key when it is absent.
                data = {k: v for k, v in data.items() if k != 'meds_vault_pending'}
            if not allow_shrink:
                drops = app_data_merge.find_drops(current, data)
                if drops:
                    raise app_data_merge.AppDataWouldDropData(app, drops)
            merged = app_data_merge.merge_document(current, data)
            merged['data_version'] = app_data_merge.next_version(current)
            if backup:
                app_data_merge.rotate_backup(self._files[app], current)
            self._save(app, merged)
        return True

    def get_updated(self, app):
        """Get last_updated timestamp for an app (lightweight poll)."""
        if app not in self.VALID_APPS:
            return None
        data = self._load(app)
        return data.get('last_updated', '')

    # ══════════════════════════════════════════════════════════════════════════
    #  ALARMFLOW
    # ══════════════════════════════════════════════════════════════════════════

    def alarm_create_timer(self, desc, hours=0, minutes=0, seconds=0, sound='chime'):
        """Create a countdown timer. Returns confirmation string."""
        total = int(hours) * 3600 + int(minutes) * 60 + int(seconds)
        if total <= 0:
            return "Error: timer duration must be greater than zero."

        item = {
            "id": f"a{_uid()}",
            "mode": "timer",
            "desc": desc or "Timer",
            "tot": total,
            "rem": total,
            "sound": sound,
            "status": "running",
            "source": "puffpai",
            "ts": int(time.time() * 1000),
            "targetMs": None,
            "created_at": datetime.now().isoformat(timespec='seconds'),
        }

        with self._locks['alarmflow']:
            data = self._load('alarmflow')
            data['items'].append(item)
            self._save('alarmflow', data)

        parts = []
        if hours:
            parts.append(f"{int(hours)} hour{'s' if int(hours) != 1 else ''}")
        if minutes:
            parts.append(f"{int(minutes)} minute{'s' if int(minutes) != 1 else ''}")
        if seconds and not hours:
            parts.append(f"{int(seconds)} second{'s' if int(seconds) != 1 else ''}")
        time_str = " and ".join(parts) if parts else f"{total} seconds"
        # Said out loud, "Timer set: 2 minute, 2 minutes counting down." is
        # the duration twice and a missing plural. The label is only worth
        # saying when the person gave it a NAME;
        # otherwise it is the duration, which the sentence already carries.
        named = str(desc or "").strip()
        if (not named or named.lower() == "timer"
                or re.fullmatch(r"(?i)\d+\s+(?:hour|minute|second)s?", named)):
            return f"Timer set for {time_str}."
        return f"Timer set for {time_str}: {named}."

    # Repeat values an alarm can carry. 'once' (or absent, for pre-repeat
    # items) means the alarm stops when dismissed; anything else reschedules.
    ALARM_REPEATS = ('once', 'daily', 'weekdays', 'weekends', 'weekly')

    @staticmethod
    def _next_alarm_target(hour, minute, repeat, after, anchor_weekday=None):
        """Next datetime strictly after `after` matching the repeat rule.

        `anchor_weekday` pins 'weekly' to a specific weekday (Mon=0); the
        other rules ignore it.
        """
        candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
        for _ in range(400):  # bounded: worst case scans ~1 year of days
            if candidate > after:
                if repeat == 'weekdays' and candidate.weekday() >= 5:
                    pass
                elif repeat == 'weekends' and candidate.weekday() < 5:
                    pass
                elif repeat == 'weekly' and anchor_weekday is not None \
                        and candidate.weekday() != anchor_weekday:
                    pass
                else:
                    return candidate
            candidate += timedelta(days=1)
        return candidate

    def _alarm_find(self, data, item_id, active_only=True):
        """Voice-friendly item lookup: exact id/desc, then unambiguous
        substring. Returns (item, error_message) — exactly one is set."""
        ident = (item_id or '').lower().strip()
        for item in data.get('items', []):
            if item['id'] == item_id or item['desc'].lower() == ident:
                return item, None
        if len(ident) >= 3:
            hits = [i for i in data.get('items', [])
                    if ident in i['desc'].lower()
                    and (not active_only or i.get('status') != 'stopped')]
            if len(hits) == 1:
                return hits[0], None
            if len(hits) > 1:
                names = ", ".join(i['desc'] for i in hits[:4])
                return None, f"Several match '{item_id}': {names}. Which one?"
        return None, f"No alarm or timer found matching '{item_id}'."

    @staticmethod
    def _fmt_target(dt):
        h12 = dt.hour % 12 or 12
        ampm = "AM" if dt.hour < 12 else "PM"
        day = dt.strftime('%a')
        return f"{day} {h12}:{dt.minute:02d} {ampm}"

    def alarm_create_alarm(self, desc, hour, minute, sound='chime', repeat='once'):
        """Create an alarm for a specific time. Returns confirmation string.

        `repeat`: once | daily | weekdays | weekends | weekly. A repeating
        alarm reschedules itself on dismiss instead of stopping; the original
        hour/minute stay on the item so snoozing never drifts the schedule.
        """
        hour, minute = int(hour), int(minute)
        repeat = repeat if repeat in self.ALARM_REPEATS else 'once'
        now = datetime.now()
        if repeat == 'once':
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
        else:
            target = self._next_alarm_target(hour, minute, repeat, now)

        target_ms = int(target.timestamp() * 1000)
        remaining = int((target - now).total_seconds())

        item = {
            "id": f"a{_uid()}",
            "mode": "alarm",
            "desc": desc or "Alarm",
            "tot": remaining,
            "rem": remaining,
            "sound": sound,
            "status": "running",
            "source": "puffpai",
            "ts": int(time.time() * 1000),
            "targetMs": target_ms,
            "hour": hour,
            "minute": minute,
            "repeat": repeat,
            # Anchor for 'weekly': the weekday of the FIRST occurrence.
            # Stored at creation because snoozing moves targetMs to "in nine
            # minutes", which would otherwise corrupt the derived anchor.
            "weekday": target.weekday(),
            "snoozed": 0
        }

        with self._locks['alarmflow']:
            data = self._load('alarmflow')
            data['items'].append(item)
            self._save('alarmflow', data)

        h12 = hour % 12 or 12
        ampm = "AM" if hour < 12 else "PM"
        when = f"ringing at {h12}:{minute:02d} {ampm}"
        if repeat != 'once':
            when += f", repeating {repeat}"
        return f"Alarm set: {desc}, {when}."

    def _alarm_reschedule(self, item):
        """Advance a repeating alarm PAST its current target (item mutated).

        Dismissing a ringing alarm rolls to the next occurrence; dismissing
        one that hasn't rung yet SKIPS that occurrence — either way the new
        target is strictly after the old one, never a same-time no-op.
        """
        now = datetime.now()
        after = now
        anchor = item.get('weekday')
        if item.get('targetMs'):
            current = datetime.fromtimestamp(item['targetMs'] / 1000)
            if anchor is None:
                anchor = current.weekday()
            if current > after:
                after = current
        nxt = self._next_alarm_target(
            int(item.get('hour', 8)), int(item.get('minute', 0)),
            item.get('repeat', 'daily'), after, anchor_weekday=anchor)
        item['targetMs'] = int(nxt.timestamp() * 1000)
        item['tot'] = item['rem'] = int((nxt - now).total_seconds())
        item['ts'] = int(time.time() * 1000)
        item['status'] = 'running'
        item['snoozed'] = 0
        return nxt

    def alarm_snooze(self, item_id, minutes=9):
        """Push a ringing (or running) item out by `minutes`. Repeating
        alarms keep their original schedule for the NEXT occurrence."""
        minutes = max(1, int(minutes))
        with self._locks['alarmflow']:
            data = self._load('alarmflow')
            item, err = self._alarm_find(data, item_id)
            if err:
                return err
            if item['status'] == 'stopped':
                return f"'{item['desc']}' is already dismissed."
            now_ms = int(time.time() * 1000)
            if item['mode'] == 'alarm':
                item['targetMs'] = now_ms + minutes * 60_000
            else:
                item['ts'] = now_ms
                item['tot'] = minutes * 60
            item['rem'] = minutes * 60
            item['status'] = 'running'
            item['snoozed'] = int(item.get('snoozed', 0)) + 1
            self._save('alarmflow', data)
        return f"Snoozed {item['desc']} for {minutes} minutes."

    def alarm_pause(self, item_id):
        """Freeze a running timer (alarms have a fixed time — no pause)."""
        with self._locks['alarmflow']:
            data = self._load('alarmflow')
            item, err = self._alarm_find(data, item_id)
            if err:
                return err
            if item['mode'] != 'timer':
                return "Alarms ring at a set time and can't be paused — snooze or dismiss instead."
            if item['status'] != 'running':
                return f"'{item['desc']}' isn't running."
            elapsed = (int(time.time() * 1000) - item['ts']) / 1000
            item['rem'] = max(0, int(item['tot'] - elapsed))
            item['status'] = 'paused'
            item['ts'] = None
            self._save('alarmflow', data)
        mins, secs = divmod(item['rem'], 60)
        return f"Paused {item['desc']} with {mins}m {secs}s left."

    def alarm_resume(self, item_id):
        """Resume a paused timer from where it froze."""
        with self._locks['alarmflow']:
            data = self._load('alarmflow')
            item, err = self._alarm_find(data, item_id)
            if err:
                return err
            if item['status'] != 'paused':
                return f"'{item['desc']}' isn't paused."
            item['tot'] = item['rem']
            item['ts'] = int(time.time() * 1000)
            item['status'] = 'running'
            self._save('alarmflow', data)
        return f"Resumed {item['desc']}."

    #: Statuses `alarm_list` reports on, most urgent first. `fired` and
    #: `missed` are the server-side scheduler's ring contract
    #: (`alarm_scheduler`): an item that is going off RIGHT NOW, and one that
    #: came due while the service was off. Before they were listed here,
    #: "what alarms do I have" answered "no active timers or alarms" while the
    #: box was audibly ringing — the one loose end of the scheduler work.
    _ALARM_LIST_STATES = ('fired', 'ringing', 'missed', 'running', 'paused')

    #: The word said out loud for each state. `running` needs none.
    _ALARM_STATE_WORDS = {
        'fired': 'ringing now',
        'ringing': 'ringing now',
        'missed': 'missed',
        'paused': 'paused',
    }

    def alarm_list(self):
        """List timers and alarms, including anything ringing or missed."""
        with self._locks['alarmflow']:
            data = self._load('alarmflow')

        by_state = {s: [] for s in self._ALARM_LIST_STATES}
        for item in data.get('items', []):
            if isinstance(item, dict) and item.get('status') in by_state:
                by_state[item['status']].append(item)
        active = [i for s in self._ALARM_LIST_STATES for i in by_state[s]]
        if not active:
            return "No active timers or alarms."

        lines = []
        for item in active:
            word = self._ALARM_STATE_WORDS.get(item['status'], "")
            if item['mode'] == 'timer':
                mins = item['rem'] // 60
                secs = item['rem'] % 60
                status = f" ({word})" if word else ""
                lines.append(f"{item['desc']}: {mins}m {secs}s remaining{status}")
            else:
                target_ms = item.get('targetMs', 0)
                if target_ms:
                    target = datetime.fromtimestamp(target_ms / 1000)
                    h12 = target.hour % 12 or 12
                    ampm = "AM" if target.hour < 12 else "PM"
                    clock = f"{h12}:{target.minute:02d} {ampm}"
                    if item['status'] in ('fired', 'ringing'):
                        lines.append(f"{item['desc']}: ringing now "
                                     f"(alarm for {clock})")
                    elif item['status'] == 'missed':
                        lines.append(f"{item['desc']}: missed at {clock}")
                    else:
                        status = f" ({word})" if word else ""
                        lines.append(f"{item['desc']}: alarm at {clock}{status}")
                else:
                    lines.append(f"{item['desc']}: alarm ({word or item['status']})")

        return f"You have {len(active)} active item{'s' if len(active) != 1 else ''}:\n" + "\n".join(lines)

    def alarm_dismiss(self, item_id):
        """Dismiss a specific alarm or timer. A repeating alarm doesn't
        stop — it rolls to its next occurrence."""
        with self._locks['alarmflow']:
            data = self._load('alarmflow')
            item, err = self._alarm_find(data, item_id)
            if err:
                return err
            if (item['mode'] == 'alarm'
                    and item.get('repeat', 'once') != 'once'):
                nxt = self._alarm_reschedule(item)
                self._save('alarmflow', data)
                return f"Dismissed: {item['desc']} — next ring {self._fmt_target(nxt)}."
            item['status'] = 'stopped'
            self._save('alarmflow', data)
            return f"Dismissed: {item['desc']}"

    def alarm_dismiss_all(self):
        """Dismiss all active alarms/timers. Repeating alarms roll to their
        next occurrence instead of stopping."""
        with self._locks['alarmflow']:
            data = self._load('alarmflow')
            count = 0
            for item in data.get('items', []):
                if item['status'] in ('running', 'ringing', 'paused'):
                    if (item['mode'] == 'alarm'
                            and item.get('repeat', 'once') != 'once'):
                        self._alarm_reschedule(item)
                    else:
                        item['status'] = 'stopped'
                    count += 1
            self._save('alarmflow', data)
        return f"Dismissed {count} item{'s' if count != 1 else ''}." if count else "No active alarms to dismiss."

    def alarm_ids_at(self, hour, minute, mode='alarm'):
        """Ids of ACTIVE *mode* items set for exactly ``hour:minute``.

        The reciprocal of :meth:`alarm_create_alarm`. After "set an alarm
        for 6:45 am tomorrow", "cancel the 6:45 am alarm" cannot resolve
        through the identifier lookup: it matches on ``desc``, and an alarm's
        ``desc`` is the literal string "Alarm", so the one thing the user
        actually said -- the time -- is the one thing nothing resolves
        against. The hour/minute are on the item.
        """
        try:
            hour, minute = int(hour), int(minute)
        except (TypeError, ValueError):
            return []
        with self._locks['alarmflow']:
            data = self._load('alarmflow')
            items = list(data.get('items', []))
        out = []
        for item in items:
            if mode and item.get('mode') != mode:
                continue
            if item.get('status') == 'stopped':
                continue
            if int(item.get('hour', -1)) == hour                     and int(item.get('minute', -1)) == minute:
                out.append(item['id'])
        return out

    def alarm_active_clocks(self, mode='alarm'):
        """``{id: (hour, minute)}`` for every ACTIVE item of *mode*.

        The read behind "cancel all alarms": the caller needs both the ids to
        act on and the times to say back, and asking twice would race a
        concurrent AlarmFlow write between the two reads.
        """
        with self._locks['alarmflow']:
            data = self._load('alarmflow')
            items = list(data.get('items', []))
        out = {}
        for item in items:
            if mode and item.get('mode') != mode:
                continue
            if item.get('status') == 'stopped':
                continue
            try:
                out[item['id']] = (int(item.get('hour', 0)),
                                   int(item.get('minute', 0)))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def alarm_latest_id(self, mode='alarm'):
        """Id of the most recently CREATED active *mode* item, or None.

        Backs "cancel the alarm I just set". ``ts`` is set at creation and
        rewritten by a snooze/reschedule, which is the right ordering for
        "the one I just touched" either way.
        """
        with self._locks['alarmflow']:
            data = self._load('alarmflow')
            items = list(data.get('items', []))
        live = [i for i in items
                if (not mode or i.get('mode') == mode)
                and i.get('status') != 'stopped']
        if not live:
            return None
        return max(live, key=lambda i: int(i.get('ts') or 0))['id']

    def alarm_delete(self, item_id):
        """Permanently remove an alarm/timer."""
        ident = (item_id or '').lower().strip()
        if len(ident) < 2:
            return "I need the timer or alarm's name or id to delete it."
        with self._locks['alarmflow']:
            data = self._load('alarmflow')
            before = len(data.get('items', []))
            data['items'] = [i for i in data.get('items', [])
                             if i['id'] != item_id and i['desc'].lower() != ident]
            removed = before - len(data['items'])
            if not removed and len(ident) >= 3:
                # Unambiguous substring fallback for spoken names
                # ("seven minute timer" vs stored "7 minutes timer").
                hits = [i for i in data['items'] if ident in i['desc'].lower()]
                if len(hits) == 1:
                    data['items'] = [i for i in data['items']
                                     if i['id'] != hits[0]['id']]
                    removed = 1
                elif len(hits) > 1:
                    names = ", ".join(i['desc'] for i in hits[:4])
                    return f"Several match '{item_id}': {names}. Which one?"
            self._save('alarmflow', data)
        if not removed:
            return f"No item found matching '{item_id}'."
        # "Deleted 1 item." would not say WHICH of the two outcomes
        # happened. Cancel is a hard removal; dismiss stops one ring and keeps
        # the item. Two outcomes need two sentences.
        subject = "it" if removed == 1 else "they"
        return (f"Cancelled and removed {removed} "
                f"item{'s' if removed != 1 else ''} - {subject} won't ring. "
                f'(Say "dismiss" instead to stop one ring and keep it.)')

    # ══════════════════════════════════════════════════════════════════════════
    #  CALENDAR
    # ══════════════════════════════════════════════════════════════════════════

    def cal_add_event(self, title, start_iso, end_iso=None, cat='other',
                      assigned_to='all', prio='medium', location=None, recurrence=None):
        """Add a calendar event.

        Recurrence may be one of: None, "daily", "weekdays", "weekly",
        "biweekly", "monthly", "yearly", or a weekday set written as
        "custom:mon,tue,thu,fri" (calendar_dates.CUSTOM_PREFIX). The frontend
        expands it into concrete instances when rendering, and
        :meth:`cal_expanded_occurrences` is the server-side expansion the
        chat/voice surface and the week view read.
        """
        if not end_iso:
            try:
                start_dt = datetime.fromisoformat(start_iso)
                end_dt = start_dt + timedelta(hours=1)
                end_iso = end_dt.isoformat()
            except ValueError:
                end_iso = start_iso

        valid_recurrence = {
            None, "", "daily", "weekdays", "weekly",
            "biweekly", "monthly", "yearly",
        }
        if recurrence and recurrence not in valid_recurrence:
            # A weekday-set recurrence ("custom:mon,tue,thu,fri") is accepted
            # in canonical form only; anything else still falls back to None
            # exactly as before.
            recurrence = _caldates.normalise_recurrence(recurrence)
        if recurrence == "":
            recurrence = None

        event = {
            "id": f"e{_uid()}",
            "title": title,
            "cat": cat if cat in ('medical', 'sports', 'school', 'work', 'family', 'bills', 'appointment', 'other') else 'other',
            "start": start_iso,
            "end": end_iso,
            "to": assigned_to,
            "prio": prio if prio in ('low', 'medium', 'high', 'urgent') else 'medium',
            "src": "puffpai",
            "rec": recurrence,
            "loc": location,
            # When the row was written, so the scheduler can tell an occurrence
            # that is genuinely due from one that resolved into the past before
            # this row existed.
            "created_at": datetime.now().isoformat(timespec='seconds'),
        }

        with self._locks['calendar']:
            data = self._load('calendar')
            data['events'].append(event)
            self._save('calendar', data)

        try:
            dt = datetime.fromisoformat(start_iso)
            date_str = dt.strftime("%B %d").replace(" 0", " ")
            time_str = dt.strftime("%I:%M %p").lstrip("0")
            return f"Added {title} on {date_str} at {time_str}, assigned to {assigned_to}."
        except ValueError:
            return f"Added {title} on {start_iso}, assigned to {assigned_to}."

    def cal_add_task(self, title, due_iso, cat='other', assigned_to='all', prio='medium'):
        """Add a task/to-do and a corresponding calendar event."""
        task_id = f"t{_uid()}"
        task = {
            "id": task_id,
            "title": title,
            "cat": cat,
            "due": due_iso,
            "to": assigned_to,
            "prio": prio if prio in ('low', 'medium', 'high', 'urgent') else 'medium',
            "status": "pending",
            "src": "puffpai",
            "created_at": datetime.now().isoformat(timespec='seconds'),
            # The link is stored on BOTH rows. The event carries taskId;
            # without the reverse pointer a task deleted by title has no way
            # to name the event it created.
            "eventId": None,
        }

        # Build a corresponding event so the task appears on the calendar month view
        try:
            due_dt = datetime.fromisoformat(due_iso)
            # If the ISO string has no explicit time component (date-only), default to 09:00
            if "T" not in due_iso:
                due_dt = due_dt.replace(hour=9, minute=0, second=0)
            start_iso = due_dt.isoformat()
            end_iso = (due_dt + timedelta(hours=1)).isoformat()
        except ValueError:
            start_iso = due_iso
            end_iso = due_iso

        task_event = {
            "id": f"te{_uid()}",
            "title": title,
            "cat": cat if cat in ('medical', 'sports', 'school', 'work', 'family', 'bills', 'appointment', 'other') else 'other',
            "start": start_iso,
            "end": end_iso,
            "to": assigned_to,
            "prio": prio if prio in ('low', 'medium', 'high', 'urgent') else 'medium',
            "src": "task",
            "taskId": task_id,
            "rec": None,
            "loc": None,
            "created_at": datetime.now().isoformat(timespec='seconds'),
        }
        task["eventId"] = task_event["id"]

        with self._locks['calendar']:
            data = self._load('calendar')
            data['tasks'].append(task)
            data['events'].append(task_event)
            self._save('calendar', data)

        # Not a row of field labels ("Task added: …, due September 13,
        # assigned to all"): one sentence, the way it would be said out
        # loud, and a date inside
        # the next day is spoken as "today"/"tomorrow".
        try:
            dt = datetime.fromisoformat(due_iso)
            today = datetime.now().date()
            delta = (dt.date() - today).days
            when = ("today" if delta == 0 else
                    "tomorrow" if delta == 1 else
                    "yesterday" if delta == -1 else
                    dt.strftime("%B %d").replace(" 0", " "))
            sentence = f"Added a task: {title}, due {when}"
            # "all" is the default owner of a household task and naming it
            # says nothing; a person named on purpose is kept.
            if assigned_to and str(assigned_to).lower() not in ("all",
                                                                "everyone"):
                sentence += f", for {assigned_to}"
            return sentence + "."
        except ValueError:
            return f"Added a task: {title}, due {due_iso}."

    # ── medications, including vaulted ones ──────────────────────────────────
    #
    # A person may VAULT a medication and still get reminders. The rule that
    # makes that safe is structural rather than a formatting habit: when a med
    # is vaulted its real name never sits in the row at all. ``name`` holds the
    # LABEL the person chose ("morning pills", or the generic "your
    # medication"), and the real name moves to ``meds_vault_pending`` keyed by
    # an opaque ``med_id``. Every existing reader — cal_list_meds, the
    # medication reader, the dashboard tile, the reminder scheduler — therefore
    # renders the label without having to know anything about vaulting, and
    # there is no code path from a reply or a scheduler payload back to the
    # name.
    #
    # ``meds_vault_pending`` is the INTERIM home for the name: sec-app-data
    # owns the encrypted-at-rest projection, and when that lands the structure
    # is what gets projected. Until then the calendar file is chmod 0600 on
    # first vault (best effort; a no-op on filesystems without POSIX modes).
    # Nothing here reads meds_vault_pending back out — the unlock path is the
    # vault's, not the calendar's.

    _MED_GENERIC_LABEL = "your medication"

    @staticmethod
    def _med_schedule(freq, next_time, times=None, days=None):
        """The real schedule behind a medication row.

        A bare "8:00 AM" with no date and no days is not a schedule — a
        scheduler cannot fire on it, which is half of why medications never
        alerted. ``times`` is a list of 24-hour "HH:MM" strings and ``days``
        a list of weekday numbers (0 = Monday), both in the box's LOCAL
        time, the same wall-clock convention the rest of the calendar uses.
        """
        freq = str(freq or 'daily').lower()
        if not times:
            hint = _caldates.parse_time_hint(str(next_time or ''))
            if hint is None:
                hint = (8, 0)
            times = ["%02d:%02d" % hint]
            if freq.startswith('twice'):
                second = ((hint[0] + 12) % 24, hint[1])
                times.append("%02d:%02d" % second)
        if days is None:
            if freq.startswith('weekday'):
                days = [0, 1, 2, 3, 4]
            elif freq.startswith('week'):
                days = [datetime.now().weekday()]
            else:
                days = [0, 1, 2, 3, 4, 5, 6]
        return sorted(set(times)), sorted(set(int(d) for d in days))

    def cal_add_med(self, name, dose, assigned_to, freq='daily',
                    next_time='8:00 AM', vaulted=False, label=None,
                    refill=None, times=None, days=None):
        """Add a medication reminder, optionally vaulted from the start.

        ``refill`` stays null unless the person actually states one. The old
        row invented ``now + 30 days`` and a surface rendered it as a refill
        countdown — a fabricated prescription date, which is worse than no
        date at all.
        """
        med_id = f"mv{_uid()}"
        display = (label or self._MED_GENERIC_LABEL) if vaulted else name
        med_times, med_days = self._med_schedule(freq, next_time, times, days)
        med = {
            "id": f"m{_uid()}",
            "med_id": med_id,
            "name": display,
            "dose": "as prescribed" if vaulted else dose,
            "to": assigned_to,
            "freq": freq,
            "next": next_time,
            "times": med_times,
            "days": med_days,
            "tz": "local",
            "status": "due",
            "vaulted": bool(vaulted),
            "label": label or (self._MED_GENERIC_LABEL if vaulted else None),
            "refill": refill or None,
            # Adding a medication at 01:12 with an 08:00 daily schedule must not
            # make the box announce YESTERDAY's 08:00 dose. The scheduler
            # reads this to refuse occurrences that
            # predate the row.
            "created_at": datetime.now().isoformat(timespec='seconds'),
        }

        with self._locks['calendar']:
            data = self._load('calendar')
            if vaulted:
                pending = data.setdefault('meds_vault_pending', {})
                pending[med_id] = {"name": name, "dose": dose}
            data['meds'].append(med)
            self._save('calendar', data)
            if vaulted:
                self._cal_restrict_file()

        if vaulted:
            return (f"Added {display} ({freq} for {assigned_to}), vaulted — "
                    "I'll remind you by that label and never say the name out "
                    "loud.")
        return f"Medication added: {name} ({dose}), {freq} for {assigned_to}."

    def cal_vault_med(self, med_identifier='', label=None):
        """Vault a medication's NAME, keeping its reminders working.

        "vault that medication" with no name vaults the most recently added
        one. After this the row carries only the label; the name is moved out
        and is not readable through any calendar method.
        """
        with self._locks['calendar']:
            data = self._load('calendar')
            meds = data.get('meds', [])
            if not meds:
                return "There are no medications tracked yet."
            ident = (med_identifier or '').strip()
            if ident and len(ident) >= 2:
                matches = self._cal_resolve(meds, ident, field='name')
                if not matches:
                    return f"No medication found matching '{ident}'."
                if len({m.get('id') for m in matches}) > 1:
                    return self._cal_confirm(
                        matches, ident, field='name', noun='medications ',
                        question="Which one should I move to the Vault? "
                                 "Nothing has changed yet.")
                target = matches[0]
            else:
                target = meds[-1]
            if target.get('vaulted'):
                return (f"'{target.get('name')}' is already vaulted — I only "
                        "ever use that label.")
            med_id = target.get('med_id') or f"mv{_uid()}"
            pending = data.setdefault('meds_vault_pending', {})
            pending[med_id] = {"name": target.get('name'),
                               "dose": target.get('dose')}
            display = label or self._MED_GENERIC_LABEL
            target['med_id'] = med_id
            target['vaulted'] = True
            target['label'] = display
            target['name'] = display
            target['dose'] = "as prescribed"
            if not target.get('times'):
                times, days = self._med_schedule(
                    target.get('freq'), target.get('next'))
                target['times'], target['days'] = times, days
                target['tz'] = 'local'
            self._save('calendar', data)
            self._cal_restrict_file()
        return (f"Vaulted. I'll call it '{display}' from now on and keep the "
                "reminders running; the name stays in the vault.")

    def _cal_restrict_file(self):
        """Best-effort 0600 on the calendar file once it holds a vault row."""
        try:
            os.chmod(self._files['calendar'], 0o600)
        except (OSError, KeyError, NotImplementedError):
            pass

    def cal_list_meds(self):
        """List medication reminders. A vaulted med shows its LABEL only."""
        with self._locks['calendar']:
            data = self._load('calendar')

        meds = data.get('meds', [])
        if not meds:
            return "No medications tracked."

        lines = []
        for m in meds:
            if m.get('vaulted'):
                lines.append(
                    f"{m.get('label') or self._MED_GENERIC_LABEL} - "
                    f"{m['freq']} for {m['to']}, next: {m['next']} "
                    "(vaulted — name hidden)")
            else:
                lines.append(f"{m['name']} ({m['dose']}) - {m['freq']} "
                             f"for {m['to']}, next: {m['next']}")
        return f"{len(meds)} medication{'s' if len(meds) != 1 else ''}:\n" + "\n".join(lines)

    def cal_add_bill(self, name, amount, due_iso, cat='utilities', auto=False):
        """Add a bill to track and a corresponding calendar event."""
        bill_id = f"b{_uid()}"
        bill = {
            "id": bill_id,
            "name": name,
            "amt": float(amount) if amount else 0,
            "due": due_iso,
            "cat": cat,
            "status": "pending",
            "auto": auto
        }

        # Build a corresponding event so the bill appears on the calendar month view
        amt_str = f"${float(amount):.2f}" if amount else "$0.00"
        bill_title = f"{name} ({amt_str})"
        try:
            due_dt = datetime.fromisoformat(due_iso)
            if "T" not in due_iso:
                due_dt = due_dt.replace(hour=9, minute=0, second=0)
            start_iso = due_dt.isoformat()
            end_iso = (due_dt + timedelta(hours=1)).isoformat()
        except ValueError:
            start_iso = due_iso
            end_iso = due_iso

        bill_event = {
            "id": f"be{_uid()}",
            "title": bill_title,
            "cat": "bills",
            "start": start_iso,
            "end": end_iso,
            "to": "all",
            "prio": "medium",
            "src": "bill",
            "billId": bill_id,
            "rec": None,
            "loc": None
        }

        with self._locks['calendar']:
            data = self._load('calendar')
            data['bills'].append(bill)
            data['events'].append(bill_event)
            self._save('calendar', data)

        try:
            dt = datetime.fromisoformat(due_iso)
            date_str = dt.strftime("%B %d").replace(" 0", " ")
            return f"Bill tracked: {name}, ${float(amount):.2f}, due {date_str}."
        except (ValueError, TypeError):
            return f"Bill tracked: {name}, ${amount}, due {due_iso}."

    def cal_expanded_occurrences(self, from_dt=None, to_dt=None):
        """Every event occurrence in [from_dt, to_dt], recurrence expanded.

        ``rec`` is stored by ``cal_add_event``; expanded only by the static
        page's own client-side code, the chat/voice surface would lose a
        recurring event after its first occurrence. This is the ONE
        server-side expansion; ``cal_list_events`` reads through it, and the
        reminder scheduler consumes it directly (a task/bill event carries
        ``taskId``/``billId`` so the scheduler can attribute what it fires).

        Args:
            from_dt: naive local ``datetime`` (or ISO string); default now.
            to_dt: naive local ``datetime`` (or ISO string); default +7 days.

        Returns a list of ``{"when": datetime, "title": str, "id": str,
        "cat": str, "to": str, "prio": str, "rec": str|None,
        "taskId": str|None, "billId": str|None, "loc": str|None,
        "src": str}`` dicts, sorted by ``when``. Times are naive local
        wall-clock, the convention the store itself uses.
        """
        def _as_dt(value, default):
            if value is None:
                return default
            if isinstance(value, datetime):
                return value
            try:
                parsed = datetime.fromisoformat(str(value))
            except ValueError:
                return default
            return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed

        now = datetime.now()
        first = _as_dt(from_dt, now)
        last = _as_dt(to_dt, first + timedelta(days=7))
        with self._locks['calendar']:
            data = self._load('calendar')
        out = []
        for when, event in _caldates.expanded_occurrences(
                data.get('events', []), first, last):
            span, all_day = self._cal_span(event)
            out.append({
                "when": when,
                # Additive (week view): the occurrence's own end, carried
                # forward from the source row's duration, and whether the row
                # was written without a time of day at all.
                "end": when + span if span else when,
                "all_day": all_day,
                "id": event.get('id'),
                "title": event.get('title', ''),
                "cat": event.get('cat', 'other'),
                "to": event.get('to', 'all'),
                "prio": event.get('prio', 'medium'),
                "rec": event.get('rec') or None,
                "taskId": event.get('taskId'),
                "billId": event.get('billId'),
                "loc": event.get('loc'),
                "src": event.get('src', 'puffpai'),
            })
        return out

    @staticmethod
    def _cal_span(event):
        """``(duration, all_day)`` for a stored event row.

        The store has never carried an all-day flag, so it is derived: a row
        whose ``start`` was written as a bare date (no ``T``) had no time of
        day, and so does one that runs a full day or longer. Returns a
        ``timedelta`` (or ``None`` when the row has no usable end) so a
        recurrence's Nth occurrence keeps the first one's length.
        """
        raw_start = event.get('start')
        raw_end = event.get('end')
        all_day = isinstance(raw_start, str) and 'T' not in raw_start
        span = None
        try:
            if isinstance(raw_start, str) and isinstance(raw_end, str):
                begin = datetime.fromisoformat(raw_start[:19])
                finish = datetime.fromisoformat(raw_end[:19])
                if finish >= begin:
                    span = finish - begin
        except ValueError:
            span = None
        if span is not None and span >= timedelta(days=1):
            all_day = True
        return span, all_day

    def cal_occurrences(self, from_dt, to_dt):
        """One JSON-ready row per occurrence in a window — the week view.

        Built on :meth:`cal_schedulable_reminders` (which already owns the
        medication schedule and the vaulting rule) plus
        :meth:`cal_expanded_occurrences` for each row's end and all-day flag.
        The contract, per row::

            {occurrence_id, event_id, title, start, end, all_day,
             person, kind: "event"|"task"|"med", label_only}

        ``title`` is the speakable/showable string ONLY: for a vaulted
        medication it is the label and ``label_only`` is true, and the real
        name is not present anywhere in this payload. ``person`` is a family
        member id, or ``None`` for a household-wide row. A bill occurrence is
        an event on the calendar, so it reports ``kind: "event"``.
        """
        spans = {
            "{0}@{1}".format(item['id'], item['when'].isoformat()): item
            for item in self.cal_expanded_occurrences(from_dt, to_dt)
        }
        out = []
        for row in self.cal_schedulable_reminders(from_dt, to_dt):
            span = spans.get(row.get('occurrence_id')) or {}
            kind = ('med' if row.get('kind') == 'medication'
                    else 'task' if row.get('kind') == 'task' else 'event')
            person = row.get('to')
            if person in (None, '', 'all'):
                person = None
            end = span.get('end') or row['when']
            out.append({
                'occurrence_id': row.get('occurrence_id'),
                'event_id': row.get('id'),
                'title': row.get('display', ''),
                'start': row['when'].isoformat(),
                'end': end.isoformat(),
                'all_day': bool(span.get('all_day')),
                'person': person,
                'kind': kind,
                'label_only': bool(row.get('vaulted')),
            })
        out.sort(key=lambda r: (r['start'], str(r['occurrence_id'])))
        return out

    # The reminder scheduler consumes this name; keep the alias stable.
    expanded_occurrences = cal_expanded_occurrences

    def cal_schedulable_reminders(self, from_dt=None, to_dt=None):
        """Everything in [from_dt, to_dt] that something should FIRE on.

        The interface the reminder scheduler consumes (this is the calendar
        side of it). One row per
        occurrence:

            {"kind": "event"|"task"|"bill"|"med",
             "id": str,                # stable row id, stable across calls
             "when": datetime,         # naive LOCAL wall-clock
             "display": str,           # safe to speak or show, always
             "vaulted": bool,          # True → display is a label, not a name
             "to": str, "cat": str,
             "rec": str|None,          # recurrence that produced this one
             "source_id": str|None}    # taskId / billId / med_id

        ``display`` is the ONLY field a reminder should read aloud. For a
        vaulted medication it is the label, and the real
        name is not present anywhere in this payload.
        """
        def _as_dt(value, default):
            if value is None:
                return default
            if isinstance(value, datetime):
                return value
            try:
                parsed = datetime.fromisoformat(str(value))
            except ValueError:
                return default
            return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed

        now = datetime.now()
        first = _as_dt(from_dt, now)
        last = _as_dt(to_dt, first + timedelta(days=1))

        out = []
        for item in self.cal_expanded_occurrences(first, last):
            src = item.get('src')
            kind = ('task' if src == 'task' else
                    'bill' if src == 'bill' else 'event')
            out.append({
                "kind": kind,
                "id": item['id'],
                "occurrence_id": f"{item['id']}@{item['when'].isoformat()}",
                "when": item['when'],
                "display": item['title'],
                "vaulted": False,
                "to": item['to'],
                "cat": item['cat'],
                "rec": item['rec'],
                "source_id": item.get('taskId') or item.get('billId'),
            })

        with self._locks['calendar']:
            data = self._load('calendar')
        for med in data.get('meds', []):
            times, days = self._med_schedule(
                med.get('freq'), med.get('next'),
                med.get('times'), med.get('days'))
            day = first.date()
            guard = 0
            while day <= last.date() and guard < 400:
                guard += 1
                if day.weekday() in days:
                    for stamp in times:
                        hint = _caldates.parse_time_hint(stamp)
                        if hint is None:
                            continue
                        when = datetime.combine(
                            day, datetime.min.time()).replace(
                            hour=hint[0], minute=hint[1])
                        if not (first <= when <= last):
                            continue
                        out.append({
                            "kind": "medication",
                            "id": med.get('id'),
                            "occurrence_id": (
                                f"{med.get('id')}@{when.isoformat()}"),
                            "when": when,
                            # The row's own name IS the label when the
                            # med is vaulted, so this can never leak.
                            "display": (med.get('label')
                                        or self._MED_GENERIC_LABEL)
                            if med.get('vaulted') else med.get('name', ''),
                            "vaulted": bool(med.get('vaulted')),
                            "label": med.get('label'),
                            "to": med.get('to', 'all'),
                            "cat": "medical",
                            "rec": str(med.get('freq', 'daily')),
                            "source_id": med.get('med_id'),
                        })
                day = day + timedelta(days=1)
        out.sort(key=lambda row: row["when"])
        return out

    def cal_list_events(self, days_ahead=7, start=None, end=None, label=None):
        """List events in a window, with recurring events expanded.

        ``days_ahead`` keeps the old "next N days from now" behaviour for
        every existing caller. ``start``/``end`` (naive local datetimes)
        scope the read to exactly the day a question named, so "tomorrow"
        does not return the whole week. ``label`` is what the answer should call that window, so an
        empty day says "Nothing on Saturday" instead of "no events".
        """
        now = datetime.now()
        if start is None or end is None:
            first = now - timedelta(hours=1)
            last = now + timedelta(days=int(days_ahead))
        else:
            first, last = start, end

        upcoming = self.cal_expanded_occurrences(first, last)

        where = ""
        if label:
            where = (f" on {label}" if label[:1].isupper()
                     else f" {label}")
        if not upcoming:
            if label:
                return f"Nothing{where}."
            return f"No events in the next {days_ahead} days."

        lines = []
        for item in upcoming:
            date_str = item["when"].strftime("%b %d, %I:%M %p").replace(" 0", " ")
            lines.append(f"{date_str} - {item['title']} ({item['cat']})")

        head = (f"{len(upcoming)} event{'s' if len(upcoming) != 1 else ''}"
                f"{where}" if label else
                f"{len(upcoming)} upcoming event"
                f"{'s' if len(upcoming) != 1 else ''}")
        return f"{head}:\n" + "\n".join(lines)

    def cal_list_day(self, day=None):
        """Everything on ONE named day -- "tomorrow", "Saturday", "today".

        The calendar manifest's ``list_day`` utterance. The weekly read,
        ``cal_list_events(days_ahead=7)``, is a fixed week, so "what is on my
        calendar tomorrow" would answer with the whole week. This action is
        built on top of the day scoping (:func:`calendar_dates.day_scope`)
        and the recurrence expansion the calendar already has. It NAMES the
        window back, so an empty day says "Nothing on Saturday." rather than
        a bare "no events".

        ``day`` accepts the phrase a person actually said ("tomorrow", "on
        Saturday", "this weekend"), an ISO date, or nothing -- which means
        today, never "the next seven days".
        """
        phrase = day if isinstance(day, str) else ("" if day is None else str(day))
        phrase = phrase.strip() or "today"
        try:
            scope = _caldates.day_scope(phrase)
        except Exception:
            scope = None
        if scope is None:
            try:
                when = datetime.fromisoformat(phrase[:19])
            except ValueError:
                return ("I couldn't tell which day you meant. Try \"today\", "
                        "\"tomorrow\", or a day of the week.")
            scope = {
                'start': datetime.combine(when.date(), datetime.min.time()),
                'end': datetime.combine(
                    when.date(), datetime.max.time()).replace(microsecond=0),
                'label': when.strftime('%b %d').replace(' 0', ' '),
                'days': 1,
            }
        return self.cal_list_events(scope.get('days', 1),
                                    start=scope['start'], end=scope['end'],
                                    label=scope.get('label'))

    def cal_list_tasks(self, status='pending'):
        """List tasks filtered by status."""
        with self._locks['calendar']:
            data = self._load('calendar')

        tasks = [t for t in data.get('tasks', []) if t.get('status') == status]
        if not tasks:
            return f"No {status} tasks."

        lines = []
        for t in tasks:
            due_str = ""
            try:
                due = datetime.fromisoformat(t['due'])
                due_str = f" (due {due.strftime('%b %d')})"
            except (ValueError, KeyError):
                pass
            prio = f" [{t.get('prio', '')}]" if t.get('prio') in ('urgent', 'high') else ""
            lines.append(f"{t['title']}{due_str}{prio}")

        return f"{len(tasks)} {status} task{'s' if len(tasks) != 1 else ''}:\n" + "\n".join(lines)

    def cal_list_bills(self, status='pending'):
        """List bills filtered by status."""
        with self._locks['calendar']:
            data = self._load('calendar')

        bills = [b for b in data.get('bills', []) if b.get('status') == status]
        if not bills:
            return f"No {status} bills."

        total = sum(b.get('amt', 0) for b in bills)
        lines = []
        for b in bills:
            due_str = ""
            try:
                due = datetime.fromisoformat(b['due'])
                due_str = f" due {due.strftime('%b %d')}"
            except (ValueError, KeyError):
                pass
            lines.append(f"{b['name']}: ${b.get('amt', 0):.2f}{due_str}")

        return f"{len(bills)} {status} bill{'s' if len(bills) != 1 else ''} (total ${total:.2f}):\n" + "\n".join(lines)

    def cal_find_event(self, title, start_iso=None):
        """The stored event row for *title*, or None — the idempotency read.

        Voice repeats itself: "…can you add that?", then "add it to the
        calendar", then "put that on the calendar too" all name the SAME
        thing, and without this read each turn creates another row (three
        copies, possibly with different recurrences). A caller that is
        about to write can ask here first: an exact match on *start_iso* wins,
        otherwise the first row with the same normalised title comes back so
        the caller can UPDATE it instead of adding a duplicate.

        Titles are compared case-, punctuation- and article-insensitively.
        Returns a copy, so a caller cannot mutate the store through it.
        """
        rows = self.cal_find_events(title)
        if not rows:
            return None
        if start_iso:
            for event in rows:
                if str(event.get('start') or '')[:16] == str(start_iso)[:16]:
                    return event
        return rows[0]

    def cal_find_events(self, title):
        """EVERY event row whose normalised title matches, oldest first.

        :meth:`cal_find_event` collapses this to one row, which is the wrong
        shape for a caller deciding whether a request names the SAME item or a
        second one: "every Saturday at 9 am to take the kids to soccer
        practice" matches an existing Mon, Tue, Thu, Fri 06:15 row on title
        alone and would overwrite it. A caller that has
        all the candidates can compare the schedules and add a second row when
        the schedules differ.

        Returns copies, so a caller cannot mutate the store through them.
        """
        wanted = normalise_event_title(title)
        if not wanted:
            return []
        with self._locks['calendar']:
            data = self._load('calendar')
            return [dict(event) for event in data.get('events', [])
                    if normalise_event_title(event.get('title')) == wanted]

    def cal_find_items(self, phrase):
        """The calendar THINGS a phrase names — tasks and events, deduped.

        Returns ``[{'kind': 'task'|'event', 'row': {...}}]``.

        Two behaviours matter and neither is available from ``cal_find_events``
        or ``cal_delete_event``:

        * **A task and its mirror event are ONE thing.** ``cal_add_task``
          writes both rows with the same title, so "delete the soccer
          practice reminder" would hit ``cal_delete_event``'s ambiguity guard
          and come back "I found 2 that match: 'X', 'X'. Which one?", two
          identical options and nothing deleted. The mirror is folded into
          its task here, so a task plus a real
          event reads as two DIFFERENT things and one task reads as one.
        * **Resolution is by normalised title**, exact first, then a
          normalised substring ("the soccer practice" naming "Take the kids
          to soccer practice"). Case, punctuation and articles are noise.

        Ordering is tasks before events, each oldest first, so a caller's
        prose is stable.
        """
        wanted = normalise_event_title(phrase)
        if len(wanted) < 3:
            return []
        with self._locks['calendar']:
            data = self._load('calendar')
            tasks = [dict(t) for t in data.get('tasks', [])]
            events = [dict(e) for e in data.get('events', [])]

        tokens = _title_tokens(wanted)

        def _pick(rows):
            exact = [r for r in rows
                     if normalise_event_title(r.get('title')) == wanted]
            if exact:
                return exact
            inside = [r for r in rows
                      if wanted in normalise_event_title(r.get('title'))]
            if inside or len(tokens) < 2:
                return inside
            # Third chance: every word the speaker used is IN the title, in
            # any order. "delete the kids soccer reminder" and
            # "... soccer practice" both name "Take the kids to soccer
            # practice", and neither is a substring of it. Two
            # significant words minimum, so a single common word cannot
            # sweep up half the calendar.
            return [r for r in rows
                    if tokens <= _title_tokens(
                        normalise_event_title(r.get('title')))]

        hit_tasks = _pick(tasks)
        hit_events = _pick(events)
        claimed = {t.get('id') for t in hit_tasks} - {None}
        mirrors = {t.get('eventId') for t in hit_tasks} - {None}
        items = [{'kind': 'task', 'row': task} for task in hit_tasks]
        for event in hit_events:
            if event.get('taskId') in claimed or event.get('id') in mirrors:
                continue    # already reported as its task
            items.append({'kind': 'event', 'row': event})
        return items

    def cal_edit_event(self, event_id, **kwargs):
        """Edit an existing calendar event. Match by ID or title (partial match).
        Accept optional: title, cat, start, end, to (assigned_to), prio, loc, rec."""
        with self._locks['calendar']:
            data = self._load('calendar')
            for event in data.get('events', []):
                if (event['id'] == event_id or
                        event_id.lower() in event['title'].lower()):
                    field_map = {
                        'title': 'title', 'cat': 'cat', 'start': 'start',
                        'end': 'end', 'to': 'to', 'prio': 'prio',
                        'loc': 'loc', 'rec': 'rec',
                    }
                    updated = []
                    for param, field in field_map.items():
                        if param in kwargs and kwargs[param] is not None:
                            event[field] = kwargs[param]
                            updated.append(param)
                    if not updated:
                        return f"No fields to update for '{event['title']}'."
                    self._save('calendar', data)
                    return f"Updated '{event['title']}': changed {', '.join(updated)}."
            return f"No event found matching '{event_id}'."

    def cal_edit_task(self, task_id_or_title, **kwargs):
        """Edit an existing task. Match by ID or title (partial match).
        Accept optional: title, due, cat, to, prio."""
        with self._locks['calendar']:
            data = self._load('calendar')
            for task in data.get('tasks', []):
                if (task['id'] == task_id_or_title or
                        task_id_or_title.lower() in task['title'].lower()):
                    field_map = {
                        'title': 'title', 'due': 'due', 'cat': 'cat',
                        'to': 'to', 'prio': 'prio',
                    }
                    updated = []
                    for param, field in field_map.items():
                        if param in kwargs and kwargs[param] is not None:
                            task[field] = kwargs[param]
                            updated.append(param)
                    if not updated:
                        return f"No fields to update for '{task['title']}'."
                    self._save('calendar', data)
                    return f"Updated '{task['title']}': changed {', '.join(updated)}."
            return f"No task found matching '{task_id_or_title}'."

    def cal_complete_task(self, task_identifier):
        """Mark a task as completed by title or ID, and update its linked calendar event."""
        with self._locks['calendar']:
            data = self._load('calendar')
            for task in data.get('tasks', []):
                if (task['id'] == task_identifier or
                        task_identifier.lower() in task['title'].lower()):
                    task['status'] = 'completed'
                    # Update the linked calendar event to show completion
                    for event in data.get('events', []):
                        if event.get('taskId') == task['id']:
                            if not event['title'].startswith('\u2705 '):
                                event['title'] = f'\u2705 {event["title"]}'
                            break
                    self._save('calendar', data)
                    return f"Done, marked '{task['title']}' as completed."
            return f"No task found matching '{task_identifier}'."

    #: Words a person puts around the thing's name when they ask for it to
    #: go away. Stripping them is what lets "delete the dentist reminder"
    #: find the event that "remind me to call the dentist" created.
    _CAL_MATCH_STOPWORDS = frozenset((
        'the', 'a', 'an', 'my', 'our', 'his', 'her', 'their', 'that', 'this',
        'event', 'events', 'task', 'tasks', 'reminder', 'reminders', 'todo',
        'to-do', 'appointment', 'appointments', 'meeting', 'entry', 'item',
        'calendar', 'schedule', 'from', 'on', 'in', 'at', 'for', 'off',
        'delete', 'remove', 'cancel', 'clear', 'drop', 'please',
    ))

    @classmethod
    def _cal_match_tokens(cls, identifier):
        """The words in an identifier that actually name something."""
        words = re.findall(r"[a-z0-9']+", (identifier or '').lower())
        return [w for w in words
                if len(w) >= 3 and w not in cls._CAL_MATCH_STOPWORDS]

    #: Words that only name a DAY. The day fallback of ``cal_delete_event``
    #: may run only when the identifier is made of these and nothing else.
    _CAL_DAY_WORDS = frozenset((
        'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday',
        'sunday', 'today', 'tonight', 'tomorrow', 'weekend', 'week', 'next',
    ))

    @classmethod
    def _cal_resolve(cls, rows, identifier, field='title'):
        """Find the row(s) an identifier names AS A WHOLE.

        Tiers, narrowest first: exact id, exact title, the whole phrase as
        whole words inside the title, then every meaningful word of the
        identifier being a word of the title. Returns the list of matching
        rows; a caller with more than one asks before deleting rather than
        removing several things at once.

        An earlier last tier matched rows sharing ANY one word, so a test
        item called "sample dishes" resolved to "do the dishes" and the
        planner deleted a real task and its event. A word the speaker said
        that is not in the title now means NOT a match.
        """
        ident = (identifier or '').strip()
        if not ident:
            return []
        low = ident.lower()
        exact_id = [r for r in rows if r.get('id') == ident]
        if exact_id:
            return exact_id
        # Only ignored words ("the", "my", "reminder") name nothing: "the"
        # must not whole-word match "do the dishes".
        if all(w in cls._CAL_MATCH_STOPWORDS
               for w in re.findall(r"[a-z0-9']+", low)):
            return []
        exact_title = [r for r in rows
                       if str(r.get(field, '')).lower() == low]
        if exact_title:
            return exact_title
        whole = re.compile(r"(?<![a-z0-9])" + re.escape(low) + r"(?![a-z0-9])")
        phrase = [r for r in rows if whole.search(str(r.get(field, '')).lower())]
        if phrase:
            return phrase
        tokens = set(cls._cal_match_tokens(ident))
        if not tokens:
            return []
        return [r for r in rows
                if tokens <= set(re.findall(r"[a-z0-9']+",
                                            str(r.get(field, '')).lower()))]

    @staticmethod
    def _cal_when(row):
        """", due September 17" / " on September 19 at 6 PM" / ""."""
        raw = str(row.get('due') or row.get('start') or '').strip()
        try:
            when = datetime.fromisoformat(raw[:19])
        except (TypeError, ValueError):
            return ""
        day = when.strftime("%B ") + str(when.day)
        if len(raw) > 10:
            clock = str(when.hour % 12 or 12)
            if when.minute:
                clock += ":%02d" % when.minute
            day += " at " + clock + (" AM" if when.hour < 12 else " PM")
        if row.get('rec'):
            day += " (repeating)"
        return (", due " if row.get('due') else " on ") + day

    @classmethod
    def _cal_confirm(cls, matches, identifier, field='title', noun='',
                     question="Which one should I delete? "
                              "Nothing has been deleted yet."):
        names = "; ".join(f"'{m.get(field, '')}'{cls._cal_when(m)}"
                          for m in matches[:4])
        more = "" if len(matches) <= 4 else f" (and {len(matches) - 4} more)"
        return (f"I found {len(matches)} {noun}that match '{identifier}': "
                f"{names}{more}. {question}")

    def cal_delete_event(self, event_identifier):
        """Delete a calendar event by title, id, or the day it falls on.

        The natural follow-up ("delete the dentist event", "remove the
        event on Saturday") is rarely a raw substring of the title, so
        resolution falls back to the meaningful words and to a day name, and
        an ambiguous match ASKS instead of deleting several rows at once.
        """
        event_identifier = (event_identifier or '').strip()
        # A blank/tiny identifier substring-matched EVERY title; an empty
        # string deleted the whole calendar. Still refused.
        if len(event_identifier) < 2:
            return "I need the event's name or id to delete it."
        with self._locks['calendar']:
            data = self._load('calendar')
            events = data.get('events', [])
            matches = self._cal_resolve(events, event_identifier)
            words = self._cal_match_tokens(event_identifier)
            if not matches and words and set(words) <= self._CAL_DAY_WORDS:
                # Only a pure day phrase ("the event on Saturday"). With any
                # other word in it ("dishes on Friday") the day alone would
                # pick whatever single event falls on it.
                matches = self._cal_events_on_day(events, event_identifier)
            if not matches:
                return (f"No event matches '{event_identifier}', so nothing "
                        "was deleted.")
            if len({m.get('id') for m in matches}) > 1:
                return self._cal_confirm(matches, event_identifier)
            target = matches[0]
            data['events'] = [e for e in events if e is not target]
            # An event created FOR a task carries taskId; dropping the event
            # without the task would leave the task orphaned the same way.
            also = ""
            task_id = target.get('taskId')
            if task_id:
                before = len(data.get('tasks', []))
                data['tasks'] = [t for t in data.get('tasks', [])
                                 if t.get('id') != task_id]
                if len(data['tasks']) < before:
                    also = ", and its reminder"
            self._save('calendar', data)
        return (f"Deleted the event '{target.get('title', '')}'"
                f"{self._cal_when(target)}{also}.")

    def _cal_events_on_day(self, events, phrase):
        """Events on the day a phrase names ("on Saturday"), or []."""
        try:
            scope = _caldates.day_scope(phrase)
        except Exception:
            return []
        if not scope:
            return []
        return [e for _when, e in _caldates.expanded_occurrences(
            events, scope['start'], scope['end'])]

    def cal_delete_task(self, task_identifier):
        """Delete a task AND the calendar event it created.

        ``cal_add_task`` writes a mirror event so the task shows on the
        month view, and deleting only the task would leave that event behind
        forever. The link is followed here in both directions (the task's
        ``eventId``, and any event carrying this ``taskId`` — rows written
        before the reverse pointer existed).
        """
        task_identifier = (task_identifier or '').strip()
        # Same blank-identifier mass-delete guard as cal_delete_event.
        if len(task_identifier) < 2:
            return "I need the task's name or id to delete it."
        with self._locks['calendar']:
            data = self._load('calendar')
            tasks = data.get('tasks', [])
            matches = self._cal_resolve(tasks, task_identifier)
            if not matches:
                return (f"No task matches '{task_identifier}', so nothing "
                        "was deleted.")
            if len({m.get('id') for m in matches}) > 1:
                return self._cal_confirm(matches, task_identifier)
            target = matches[0]
            data['tasks'] = [t for t in tasks if t is not target]
            linked = {target.get('eventId')} - {None}
            events = data.get('events', [])
            before = len(events)
            data['events'] = [e for e in events
                              if e.get('id') not in linked
                              and e.get('taskId') != target.get('id')]
            dropped = before - len(data['events'])
            self._save('calendar', data)
        also = ", and removed it from the calendar" if dropped else ""
        return (f"Deleted the task '{target.get('title', '')}'"
                f"{self._cal_when(target)}{also}.")

    def cal_set_med_schedule(self, med_identifier, next_time=None,
                             freq=None):
        """Point an EXISTING medication at a time and cadence.

        Resolves by row id only - the caller has already resolved the name,
        and a fuzzy match here would silently reschedule the wrong row.
        Returns the updated row, or None when nothing matched.
        """
        with self._locks['calendar']:
            data = self._load('calendar')
            for med in data.get('meds', []):
                if med_identifier and med_identifier in (med.get('id'),
                                                         med.get('med_id')):
                    med['freq'] = str(freq or med.get('freq') or 'daily')
                    if next_time:
                        med['next'] = next_time
                    med['times'], med['days'] = self._med_schedule(
                        med['freq'], med.get('next'))
                    med['status'] = 'due'
                    self._save('calendar', data)
                    return dict(med)
            return None

    def cal_take_med(self, med_identifier):
        """Mark a medication as taken."""
        with self._locks['calendar']:
            data = self._load('calendar')
            for med in data.get('meds', []):
                if (med['id'] == med_identifier or
                        med_identifier.lower() in med['name'].lower()):
                    med['status'] = 'taken'
                    self._save('calendar', data)
                    return f"Recorded: {med['name']} taken."
            return f"No medication found matching '{med_identifier}'."

    def cal_pay_bill(self, bill_identifier):
        """Mark a bill as paid and update its linked calendar event."""
        with self._locks['calendar']:
            data = self._load('calendar')
            for bill in data.get('bills', []):
                if (bill['id'] == bill_identifier or
                        bill_identifier.lower() in bill['name'].lower()):
                    bill['status'] = 'paid'
                    # Update the linked calendar event to show payment
                    for event in data.get('events', []):
                        if event.get('billId') == bill['id']:
                            if not event['title'].startswith('\u2705 '):
                                event['title'] = f'\u2705 {event["title"]}'
                            break
                    self._save('calendar', data)
                    return f"Marked '{bill['name']}' as paid."
            return f"No bill found matching '{bill_identifier}'."

    # ══════════════════════════════════════════════════════════════════════════
    #  FRESHTABLE
    # ══════════════════════════════════════════════════════════════════════════

    _FOOD_CATEGORIES = {
        'Protein': ['chicken', 'beef', 'pork', 'fish', 'salmon', 'shrimp', 'turkey', 'steak',
                     'bacon', 'sausage', 'lamb', 'tofu', 'eggs', 'egg'],
        'Produce': ['apple', 'banana', 'orange', 'tomato', 'lettuce', 'spinach', 'broccoli',
                    'carrot', 'onion', 'pepper', 'avocado', 'potato', 'garlic', 'cucumber',
                    'celery', 'mushroom', 'corn', 'berry', 'grape', 'lemon', 'lime', 'mango'],
        'Dairy': ['milk', 'cheese', 'yogurt', 'butter', 'cream', 'sour cream', 'cottage'],
        'Grain': ['bread', 'rice', 'pasta', 'flour', 'oat', 'cereal', 'tortilla', 'noodle'],
        'Pantry': ['oil', 'vinegar', 'sauce', 'soup', 'can', 'bean', 'sugar', 'salt',
                   'spice', 'honey', 'peanut butter', 'jam', 'jelly', 'ketchup', 'mustard'],
        'Frozen': ['frozen', 'ice cream', 'pizza'],
        'Beverage': ['water', 'juice', 'soda', 'coffee', 'tea', 'beer', 'wine'],
        'Snack': ['chips', 'crackers', 'cookies', 'nuts', 'popcorn', 'candy', 'chocolate'],
    }

    def _guess_category(self, name):
        lower = name.lower()
        for cat, keywords in self._FOOD_CATEGORIES.items():
            if any(kw in lower for kw in keywords):
                return cat
        return "Other"

    def fresh_add_pantry(self, name, quantity=1, unit='pcs', category=None, price=0):
        """Add an item to the pantry."""
        if not category:
            category = self._guess_category(name)

        item = {
            "id": f"id_{_uid()}",
            "name": name,
            "emoji": "",
            "category": category,
            "quantity": int(quantity),
            "maxQuantity": max(int(quantity) * 3, 5),
            "unit": unit,
            "priceEach": float(price) if price else 0,
            "addedDate": datetime.now().isoformat()
        }

        with self._locks['freshtable']:
            data = self._load('freshtable')
            # Check if item already exists, increment quantity
            for existing in data.get('pantry', []):
                if existing['name'].lower() == name.lower():
                    existing['quantity'] += int(quantity)
                    self._save('freshtable', data)
                    return f"Updated {name}: now {existing['quantity']} {existing['unit']} in pantry."
            data['pantry'].append(item)
            self._save('freshtable', data)

        return f"Added {quantity} {unit} of {name} to the pantry ({category})."

    @staticmethod
    def _pantry_term_variants(term):
        """Case-folded singular/plural variants of one pantry filter word.

        "rice" -> {rice, rices}; "eggs" -> {eggs, egg}; "berries" -> {berries,
        berry}. Both directions are generated because the query and the stored
        item name disagree about number far more often than about spelling
        ("do we have eggs" against a stored "Egg", "any tomato left" against
        "Tomatoes").
        """
        base = re.sub(r"[^a-z0-9 ]+", " ", str(term or "").lower())
        base = re.sub(r"\s+", " ", base).strip()
        # Leading determiners and partitives are never part of an item name.
        # The chat rules strip these before the tool is called, but the tool is
        # also reachable from the agent, from voice and from the app, and "an
        # egg" searched literally is a deterministic false denial over real
        # stock. Stripped twice on purpose.
        # ``(?:\s+|\Z)`` so a term that is NOTHING but determiners collapses to
        # empty and yields an honest miss, rather than searching for "the".
        base = re.sub(r"\A(?:(?:of\s+the|of\s+our|of\s+my|enough|some|more|"
                      r"any|the|our|an|my|of|a)(?:\s+|\Z)){0,2}",
                      "", base).strip()
        if not base:
            return []
        if len(base) < 2:
            # A one-character filter is not a name. Matching it as a whole
            # word would claim half the pantry; an honest miss is better.
            return []
        variants = {base}
        if base.endswith("ies") and len(base) > 4:
            variants.add(base[:-3] + "y")
        if base.endswith("es") and len(base) > 3:
            variants.add(base[:-2])
        if base.endswith("s") and len(base) > 2:
            variants.add(base[:-1])
        if not base.endswith("s"):
            variants.add(base + "s")
            variants.add(base + "es")     # tomato -> Tomatoes, box -> Boxes
        return sorted(variants, key=len, reverse=True)

    @classmethod
    def _pantry_item_matches(cls, item, term):
        """True when *term* names this pantry item, by NAME or by category.

        Comparing only against ``category`` would make a pantry holding
        three "Rice" entries filed under "Grains" answer "no rice". Name
        first, category second, whole words only so "rice" does
        not match "liquorice".
        """
        hay = "{} {}".format(item.get("name", ""), item.get("category", ""))
        hay = str(hay).lower()
        for variant in cls._pantry_term_variants(term):
            if re.search(r"\b" + re.escape(variant) + r"\b", hay):
                return True
        return False

    def fresh_list_pantry(self, filter_cat=None):
        """List pantry items, optionally filtered by 'low', an item NAME, or a category."""
        with self._locks['freshtable']:
            data = self._load('freshtable')

        pantry = data.get('pantry', [])
        stocked = len(pantry)
        if not pantry:
            return "Pantry is empty."

        term = str(filter_cat).strip().strip("'\"") if filter_cat else ""
        if term and term.lower() == 'low':
            pantry = [i for i in pantry if i['quantity'] <= i.get('maxQuantity', 5) * 0.3]
            if not pantry:
                return (f"Nothing is running low in the pantry — I checked the "
                        f"stock level on all {stocked} item"
                        f"{'s' if stocked != 1 else ''} listed.")
            label = "Low stock items"
        elif term:
            pantry = [i for i in pantry if self._pantry_item_matches(i, term)]
            if not pantry:
                # A miss says what was searched. A bare empty result is what
                # let a model turn "the filter found nothing" into a confident
                # "no, we don't have any" over real stock.
                return (f"I don't see \"{term}\" in the pantry — I searched all "
                        f"{stocked} item name{'s' if stocked != 1 else ''} and "
                        f"categories on the list and found no match.")
            label = f"Pantry — \"{term}\""
        else:
            label = "Pantry"

        lines = []
        for item in pantry:
            lines.append(f"{item['name']}: {item['quantity']} {item['unit']} ({item.get('category', '')})")

        return f"{label} ({len(pantry)} item{'s' if len(pantry) != 1 else ''}):\n" + "\n".join(lines)

    # ── Meal planning + grocery ordering (local-first, replaces the dead
    #    automation meal_planner/grocery_list webhooks) ─────────────────────

    _MEAL_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday",
                  "saturday", "sunday"]
    _MEAL_TYPES = ("breakfast", "lunch", "dinner", "snack")

    def fresh_set_meal(self, day, meal, dish, notes=None, recipe_id=None):
        """Upsert one meal-plan slot (day = weekday name, meal = type)."""
        day = str(day).strip().lower()
        meal = str(meal).strip().lower()
        if day not in self._MEAL_DAYS:
            return f"Unknown day '{day}' — use monday..sunday."
        if meal not in self._MEAL_TYPES:
            return f"Unknown meal '{meal}' — use breakfast/lunch/dinner/snack."
        entry = {"id": f"id_{_uid()}", "day": day, "meal": meal,
                 "dish": str(dish).strip()}
        if notes:
            entry["notes"] = str(notes)[:500]
        if recipe_id:
            entry["recipeId"] = recipe_id
        with self._locks['freshtable']:
            data = self._load('freshtable')
            plan = data.setdefault('mealPlan', [])
            plan[:] = [e for e in plan
                       if not (e.get('day') == day and e.get('meal') == meal)]
            plan.append(entry)
            self._save('freshtable', data)
        return f"Planned {meal} on {day.capitalize()}: {dish}."

    def fresh_get_meal_plan(self):
        with self._locks['freshtable']:
            data = self._load('freshtable')
        plan = data.get('mealPlan', [])
        order = {d: i for i, d in enumerate(self._MEAL_DAYS)}
        morder = {m: i for i, m in enumerate(self._MEAL_TYPES)}
        return sorted(plan, key=lambda e: (order.get(e.get('day'), 9),
                                           morder.get(e.get('meal'), 9)))

    def fresh_clear_meal_plan(self):
        with self._locks['freshtable']:
            data = self._load('freshtable')
            n = len(data.get('mealPlan', []))
            data['mealPlan'] = []
            self._save('freshtable', data)
        return f"Cleared {n} planned meal(s)."

    def fresh_grocery_from_plan(self):
        """Rebuild the meal-plan portion of the shopping list from planned
        meals. Ingredients come from each entry's notes ('ingredients: a, b')
        or its linked recipe. Manually added items are left untouched."""
        with self._locks['freshtable']:
            data = self._load('freshtable')
            plan = data.get('mealPlan', [])
            recipes = {r.get('id'): r for r in data.get('recipes', [])}
            wanted = {}
            for e in plan:
                src = ""
                notes = e.get('notes', '') or ''
                if 'ingredients:' in notes.lower():
                    src = notes.lower().split('ingredients:', 1)[1]
                elif e.get('recipeId') in recipes:
                    src = recipes[e['recipeId']].get('ingredients', '')
                for raw in src.split(','):
                    name = raw.strip().strip('.').strip()
                    if 2 < len(name) < 60:
                        key = name.lower()
                        wanted[key] = wanted.get(key, 0) + 1
            # Replace previous auto-generated items; keep manual ones
            slist = [i for i in data.get('shoppingList', [])
                     if not i.get('fromMealPlan')]
            for name, count in wanted.items():
                slist.append({
                    "id": f"id_{_uid()}",
                    "name": name.capitalize(),
                    "quantity": count,
                    "unit": "pcs",
                    "category": self._guess_category(name),
                    "estimatedPrice": 0,
                    "checked": False,
                    "fromMealPlan": True,
                })
            data['shoppingList'] = slist
            self._save('freshtable', data)
        return (f"Grocery list rebuilt from the meal plan: "
                f"{len(wanted)} ingredient(s) added.")

    def fresh_build_grocery_order(self):
        """Package unchecked shopping-list items as a grocery order draft.

        No vendor ships with the appliance. A household that wants store
        links sets ``grocery_store_name`` and ``grocery_search_url`` (a
        template containing ``{q}``, the URL-encoded item name); until then
        the draft is a plain item + quantity list with empty ``store`` and
        ``url`` fields. PuffPAI never checks out — the user reviews the cart
        and pays (by design).
        """
        from urllib.parse import quote_plus
        from datetime import datetime as _dt
        store, url_tmpl = _grocery_vendor()
        with self._locks['freshtable']:
            data = self._load('freshtable')
            items = [i for i in data.get('shoppingList', [])
                     if not i.get('checked')]
            order = {
                "created_at": _dt.now().isoformat(timespec='seconds'),
                "store": store,
                "item_count": len(items),
                "items": [{
                    "name": i['name'],
                    "quantity": i.get('quantity', 1),
                    "url": (url_tmpl.replace("{q}", quote_plus(i['name']))
                            if url_tmpl else ""),
                } for i in items],
            }
            data['groceryOrder'] = order
            self._save('freshtable', data)
        return order

    def fresh_add_shopping(self, name, quantity=1, est_price=0, category=None):
        """Add an item to the shopping list."""
        if not category:
            category = self._guess_category(name)

        item = {
            "id": f"id_{_uid()}",
            "name": name,
            "quantity": int(quantity),
            "unit": "pcs",
            "category": category,
            "estimatedPrice": float(est_price) if est_price else 0,
            "checked": False
        }

        with self._locks['freshtable']:
            data = self._load('freshtable')
            data.setdefault('shoppingList', []).append(item)
            self._save('freshtable', data)

        price_str = f", about ${float(est_price):.2f}" if est_price else ""
        return f"Added {name} to the shopping list, quantity {quantity}{price_str}."

    def fresh_shopping_items(self):
        """The UNCHECKED shopping-list rows, as stored.

        The one structured read behind every count and every write
        verification the chat path makes: a caller that needs to know whether
        an item landed, or how many there are, reads the store rather than
        counting words in a sentence. ``fresh_list_shopping`` is the prose view
        of exactly these rows.
        """
        with self._locks['freshtable']:
            data = self._load('freshtable')
        return [i for i in data.get('shoppingList', [])
                if isinstance(i, dict) and not i.get('checked')]

    def fresh_list_shopping(self):
        """List current shopping list items."""
        items = self.fresh_shopping_items()
        if not items:
            return "Shopping list is empty."

        total = sum(i.get('estimatedPrice', 0) * i.get('quantity', 1) for i in items)
        lines = []
        for item in items:
            price = f" ~${item.get('estimatedPrice', 0):.2f}" if item.get('estimatedPrice') else ""
            lines.append(f"{item['name']}: {item['quantity']}{price}")

        result = f"Shopping list ({len(items)} item{'s' if len(items) != 1 else ''}):\n" + "\n".join(lines)
        if total > 0:
            result += f"\nEstimated total: ${total:.2f}"
        return result

    def fresh_remove_pantry(self, item_identifier):
        """Remove an item from the pantry by name or ID."""
        with self._locks['freshtable']:
            data = self._load('freshtable')
            before = len(data.get('pantry', []))
            data['pantry'] = [i for i in data.get('pantry', [])
                              if i['id'] != item_identifier and
                              item_identifier.lower() not in i['name'].lower()]
            removed = before - len(data['pantry'])
            self._save('freshtable', data)
        return f"Removed {removed} item{'s' if removed != 1 else ''} from pantry." if removed else f"No pantry item found matching '{item_identifier}'."

    def fresh_check_shopping(self, item_identifier):
        """Check off (mark as bought) a shopping list item by name or ID."""
        with self._locks['freshtable']:
            data = self._load('freshtable')
            for item in data.get('shoppingList', []):
                if (item['id'] == item_identifier or
                        item_identifier.lower() in item['name'].lower()):
                    item['checked'] = True
                    self._save('freshtable', data)
                    return f"Checked off '{item['name']}' from the shopping list."
            return f"No shopping list item found matching '{item_identifier}'."

    def fresh_remove_shopping(self, item_identifier):
        """Remove an item from the shopping list by id or name.

        EXACT name match first (case-insensitive). A substring match is only
        honoured when it hits exactly ONE row — "take milk off the list" must
        not silently take "oat milk", "milk" and "buttermilk" with it, which is
        what the pantry remover's unconditional substring rule would do.

        Returns ``(removed_names, ambiguous_names)``: both empty means nothing
        matched, and a non-empty ``ambiguous`` means nothing was removed and the
        caller should ask which one.
        """
        wanted = str(item_identifier or "").strip()
        if not wanted:
            return [], []
        low = wanted.lower()
        with self._locks['freshtable']:
            data = self._load('freshtable')
            rows = data.get('shoppingList', []) or []
            exact = [i for i in rows
                     if i.get('id') == wanted
                     or str(i.get('name', '')).strip().lower() == low]
            if exact:
                victims = exact
            else:
                partial = [i for i in rows
                           if low in str(i.get('name', '')).lower()]
                if len(partial) != 1:
                    return [], [str(i.get('name', '')) for i in partial]
                victims = partial
            ids = {id(i) for i in victims}
            data['shoppingList'] = [i for i in rows if id(i) not in ids]
            self._save('freshtable', data)
        return [str(i.get('name', '')) for i in victims], []

    def fresh_create_recipe(self, name, ingredients, instructions):
        """Create a new recipe with name, ingredients list, and cooking instructions.

        Stores ingredients as a comma-joined string so the frontend can render
        it directly. Pass either a list or a string.
        """
        if isinstance(ingredients, list):
            ingredients_str = ", ".join(str(i).strip() for i in ingredients)
        else:
            ingredients_str = str(ingredients or "").strip()
        ingredient_count = len([i for i in ingredients_str.split(",") if i.strip()])

        recipe = {
            "id": f"r{_uid()}",
            "name": name,
            "ingredients": ingredients_str,
            "instructions": str(instructions or "").strip(),
            "createdAt": datetime.now().isoformat(),
        }

        with self._locks['freshtable']:
            data = self._load('freshtable')
            data.setdefault('recipes', []).append(recipe)
            self._save('freshtable', data)

        return f"Recipe created: {name} with {ingredient_count} ingredients. Open FreshTable > Recipes to see it."

    def fresh_list_recipes(self, filter_text=None):
        """List saved recipes, optionally filtered by name/ingredient text."""
        with self._locks['freshtable']:
            data = self._load('freshtable')

        recipes = data.get('recipes', [])
        f = (filter_text or "").strip().lower()
        if f:
            recipes = [r for r in recipes
                       if f in str(r.get('name', '')).lower()
                       or f in str(r.get('ingredients', '')).lower()]
        if not recipes:
            return "No recipes found." if f else "No recipes saved yet."

        lines = []
        for r in recipes:
            ingredients = str(r.get('ingredients', '')).strip()
            lines.append(f"{r.get('name', 'Untitled')}: {ingredients}"
                         if ingredients else str(r.get('name', 'Untitled')))
        header = f"Recipes ({len(recipes)}):"
        return header + "\n" + "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════════
    #  FITFAM
    # ══════════════════════════════════════════════════════════════════════════

    def fit_log_workout(self, user, workout_type, duration, notes=None):
        """Log a completed workout for a family member."""
        user = user.lower().strip()

        with self._locks['fitfam']:
            data = self._load('fitfam')
            users = data.get('users', {})
            if user not in users:
                return f"Unknown user '{user}'. Available: {', '.join(users.keys())}"

            entry = {
                "date": datetime.now().isoformat(),
                "type": workout_type,
                "duration": int(duration),
                "notes": notes or ""
            }

            u = users[user]
            u.setdefault('history', []).append(entry)
            u['totalWorkouts'] = u.get('totalWorkouts', 0) + 1

            # Update streak
            today = datetime.now().date()
            yesterday = today - timedelta(days=1)
            has_yesterday = any(
                datetime.fromisoformat(h['date']).date() == yesterday
                for h in u.get('history', []) if h != entry
            )
            if has_yesterday or u.get('streak', 0) == 0:
                u['streak'] = u.get('streak', 0) + 1
            else:
                u['streak'] = 1

            self._save('fitfam', data)

        streak_str = f" That's {u['streak']} day{'s' if u['streak'] != 1 else ''} in a row!" if u['streak'] > 1 else ""
        return f"Logged {users[user]['name']}'s {int(duration)}-minute {workout_type} workout.{streak_str}"

    @staticmethod
    def _fit_active_days(u):
        """The set of dates on which *u* logged anything (history or sets)."""
        days = set()
        for coll in ('history', 'sets'):
            for entry in (u or {}).get(coll, []) or []:
                try:
                    days.add(datetime.fromisoformat(
                        str(entry.get('date') or '')).date())
                except (TypeError, ValueError):
                    continue
        return days

    @classmethod
    def _fit_recompute_streak(cls, u):
        """Recount the streak from what is actually left in the history.

        The logging paths keep the streak incrementally, which cannot survive a
        deletion (removing the entry that started a run must shorten it). After
        a delete we recount instead: consecutive days back from the most recent
        active day, and zero unless that day is today or yesterday.
        """
        days = cls._fit_active_days(u)
        if not days:
            return 0
        today = datetime.now().date()
        latest = max(days)
        if (today - latest).days > 1:
            return 0
        streak = 0
        cursor = latest
        while cursor in days:
            streak += 1
            cursor = cursor - timedelta(days=1)
        return streak

    def fit_user_keys(self):
        """The FitFam slice keys, for a 'whose workout?' prompt.

        Keys only — never the contents of a slice.
        """
        with self._locks['fitfam']:
            data = self._load('fitfam')
        return list((data.get('users') or {}).keys())

    def fit_delete_entry(self, user, title=None, last=False):
        """Remove ONE logged workout from *user*'s OWN history.

        The undo for a mis-logged workout, which is otherwise permanent on
        every surface. Scoped to a single slice by construction:
        ``user`` is required and nothing outside ``users[user]`` is read or
        written, so this can never touch another household member's history.

        ``title`` fuzzy-matches the workout type; ``last=True`` (or no title)
        removes the most recent entry. When a title matches more than one
        DISTINCT workout title the call refuses and reports the candidates, so
        the caller can confirm rather than delete the wrong thing.

        Returns a dict. ``removed`` is a TITLE ONLY — no duration, date, streak
        or age is ever returned, because the confirmation is spoken aloud in a
        household that may include a child's data.
        """
        key = (user or "").lower().strip()
        with self._locks['fitfam']:
            data = self._load('fitfam')
            users = data.get('users', {})
            if not key or key not in users:
                return {"ok": False, "reason": "unknown_user",
                        "users": list(users.keys())}

            u = users[key]
            history = u.get('history') or []
            if not history:
                return {"ok": False, "reason": "empty", "user": key}

            wanted = None
            if title and not last:
                from puffpai.fitfam_access import find_by_title
                hits = find_by_title(history, title)
                if not hits:
                    return {"ok": False, "reason": "no_match",
                            "user": key, "title": str(title)}
                distinct = []
                for i in hits:
                    name = str(history[i].get('type') or '')
                    if name not in distinct:
                        distinct.append(name)
                if len(distinct) > 1:
                    return {"ok": False, "reason": "ambiguous",
                            "user": key, "candidates": distinct}
                # Same title several times over: the most recent one is the
                # one they just logged and meant.
                wanted = hits[-1]
            else:
                wanted = len(history) - 1

            entry = history.pop(wanted)
            removed_title = str(entry.get('type') or 'workout')
            u['history'] = history
            u['totalWorkouts'] = max(0, int(u.get('totalWorkouts', 0) or 0) - 1)
            u['streak'] = self._fit_recompute_streak(u)
            self._save('fitfam', data)

        return {"ok": True, "user": key, "name": u.get('name'),
                "removed": removed_title, "remaining": len(history)}

    def fit_get_status(self, user=None):
        """Get fitness status for a user or all users."""
        with self._locks['fitfam']:
            data = self._load('fitfam')

        users = data.get('users', {})
        if user and user.lower().strip() in users:
            u = users[user.lower().strip()]
            last = ""
            if u.get('history'):
                h = u['history'][-1]
                last = f" Last workout: {h['type']} ({h['duration']}min)"
            return (f"{u['name']}: {u.get('streak', 0)}-day streak, "
                    f"{u.get('totalWorkouts', 0)} total workouts.{last}")

        lines = []
        for uid, u in users.items():
            lines.append(f"{u['name']}: {u.get('streak', 0)}-day streak, "
                         f"{u.get('totalWorkouts', 0)} total workouts")
        return "\n".join(lines) if lines else "No fitness data."

    def fit_log_set(self, user, exercise_id, weight, reps, rpe=None, notes=""):
        """Log a single structured set for an exercise.

        This is the science-based logging path: per-set tracking with
        weight, reps, and optional RPE for autoregulation.

        The set is appended to the user's `sets` history. We also bump
        their `totalWorkouts` if this is their first set today.
        """
        with self._locks['fitfam']:
            data = self._load('fitfam')
            users = data.get('users', {})
            if user not in users:
                return f"Unknown user '{user}'. Available: {', '.join(users.keys())}"

            try:
                weight = float(weight)
                reps = int(reps)
                rpe_val = float(rpe) if rpe not in (None, "") else None
            except (TypeError, ValueError):
                return "Weight, reps, and RPE must be numeric."

            entry = {
                "id": f"s{_uid()}",
                "date": datetime.now().isoformat(),
                "exerciseId": exercise_id,
                "weight": weight,
                "reps": reps,
                "rpe": rpe_val,
                "notes": notes or "",
            }

            u = users[user]
            u.setdefault('sets', []).append(entry)

            # Bump totalWorkouts only if this is the first entry today
            today = datetime.now().date().isoformat()
            existing_today = [
                s for s in u['sets'][:-1]
                if s.get('date', '').startswith(today)
            ]
            if not existing_today:
                u['totalWorkouts'] = u.get('totalWorkouts', 0) + 1
                # Update streak
                yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
                had_yesterday = any(
                    s.get('date', '').startswith(yesterday)
                    for s in u.get('sets', [])
                ) or any(
                    h.get('date', '').startswith(yesterday)
                    for h in u.get('history', [])
                )
                if had_yesterday:
                    u['streak'] = u.get('streak', 0) + 1
                else:
                    u['streak'] = 1

            self._save('fitfam', data)

        return (f"Logged: {weight}x{reps}"
                + (f" @ RPE {rpe_val}" if rpe_val else "")
                + f" of {exercise_id} for {users[user]['name']}.")

    def fit_recent_sets(self, user, exercise_id=None, limit=20):
        """Return the most recent sets for a user, optionally filtered by exercise."""
        with self._locks['fitfam']:
            data = self._load('fitfam')
        users = data.get('users', {})
        if user not in users:
            return []
        sets = users[user].get('sets', [])
        if exercise_id:
            sets = [s for s in sets if s.get('exerciseId') == exercise_id]
        sets = sorted(sets, key=lambda s: s.get('date', ''), reverse=True)
        return sets[:limit]

    def fit_personal_records(self, user):
        """Return the best estimated 1RM per exercise for a user.

        Uses the Epley formula: 1RM = w * (1 + r/30).
        """
        with self._locks['fitfam']:
            data = self._load('fitfam')
        users = data.get('users', {})
        if user not in users:
            return {}
        sets = users[user].get('sets', [])
        records = {}
        for s in sets:
            ex = s.get('exerciseId')
            w = s.get('weight') or 0
            r = s.get('reps') or 0
            if not ex or w <= 0 or r <= 0:
                continue
            est_1rm = w * (1 + r / 30) if r > 1 else w
            if ex not in records or est_1rm > records[ex]['estimated1RM']:
                records[ex] = {
                    'exerciseId': ex,
                    'weight': w,
                    'reps': r,
                    'date': s.get('date'),
                    'estimated1RM': round(est_1rm, 1),
                }
        return records

    def fit_weekly_volume(self, user):
        """Compute working sets per muscle group for the past 7 days.

        Returns a dict keyed by muscle group. Note: this counts SETS not
        TOTAL VOLUME (weight x reps), since volume landmarks are typically
        expressed in working sets per week.
        """
        with self._locks['fitfam']:
            data = self._load('fitfam')
        users = data.get('users', {})
        if user not in users:
            return {}
        sets = users[user].get('sets', [])
        cutoff = datetime.now() - timedelta(days=7)
        volume = {}
        for s in sets:
            try:
                dt = datetime.fromisoformat(s.get('date', ''))
                if dt < cutoff:
                    continue
            except (ValueError, TypeError):
                continue
            # We don't have the exercise->muscle mapping in Python.
            # The frontend handles muscle aggregation. Here we just
            # return raw set counts per exercise for the frontend to
            # aggregate.
            ex = s.get('exerciseId', 'unknown')
            volume[ex] = volume.get(ex, 0) + 1
        return volume

    def fit_list_goals(self, user=None):
        """List fitness goals for a user or all users."""
        with self._locks['fitfam']:
            data = self._load('fitfam')

        users = data.get('users', {})
        if user and user.lower().strip() in users:
            u = users[user.lower().strip()]
            goals = u.get('goals', [])
            if not goals:
                return f"{u['name']} has no fitness goals set."
            lines = [f"{u['name']}'s fitness goals:"]
            for i, g in enumerate(goals, 1):
                if isinstance(g, dict):
                    lines.append(f"  {i}. {g.get('name', g.get('goal', str(g)))}")
                else:
                    lines.append(f"  {i}. {g}")
            return "\n".join(lines)

        all_lines = []
        for uid, u in users.items():
            goals = u.get('goals', [])
            if goals:
                all_lines.append(f"{u['name']}'s goals:")
                for i, g in enumerate(goals, 1):
                    if isinstance(g, dict):
                        all_lines.append(f"  {i}. {g.get('name', g.get('goal', str(g)))}")
                    else:
                        all_lines.append(f"  {i}. {g}")
            else:
                all_lines.append(f"{u['name']}: no goals set")
        return "\n".join(all_lines) if all_lines else "No fitness data."

    # ══════════════════════════════════════════════════════════════════════════
    #  TRIVIA
    # ══════════════════════════════════════════════════════════════════════════

    def trivia_get_stats(self):
        """Get trivia game history and aggregate stats."""
        with self._locks['trivia']:
            data = self._load('trivia')

        results = data.get('results', [])
        if not results:
            return "No trivia games played yet."

        total = len(results)
        avg_score = sum(r.get('score', 0) for r in results) / total
        best = max(results, key=lambda r: r.get('score', 0))
        return (f"Trivia stats: {total} game{'s' if total != 1 else ''} played. "
                f"Average score: {avg_score:.1f}. "
                f"Best: {best.get('score', 0)} in {best.get('category', 'unknown')} "
                f"({best.get('difficulty', 'unknown')}).")

    # ══════════════════════════════════════════════════════════════════════════
    #  MEDIA HUB
    # ══════════════════════════════════════════════════════════════════════════
    #
    # Control here is INVERTED relative to every other app. The dashboard owns
    # the <audio> element for World Radio, so the browser is the only thing
    # that can actually start or stop sound — the backend can only leave a
    # note. Hence report (browser -> us), poll/ack (browser pulls the note),
    # and request (LLM leaves the note). See media_hub.py for the reasoning.

    @staticmethod
    def _mediahub_clean_station(station):
        """Validate a station dict down to the fields the player uses.

        Returns the trimmed dict, or raises ValueError. Anything the catalog
        search did not produce is dropped rather than stored: this doc is
        handed straight to a browser audio element.
        """
        if not isinstance(station, dict):
            raise ValueError("station must be an object")
        if len(json.dumps(station, default=str)) > 16384:
            raise ValueError("station payload too large")

        allowed = ("stationuuid", "name", "url_resolved", "country",
                   "countrycode", "codec", "bitrate", "tags", "votes",
                   "homepage")
        out = {}
        for key in allowed:
            val = station.get(key)
            if key in ("bitrate", "votes"):
                try:
                    out[key] = int(val or 0)
                except (TypeError, ValueError):
                    out[key] = 0
            else:
                out[key] = str(val or "")

        if not out["url_resolved"]:
            raise ValueError("station.url_resolved is required")
        if not out["url_resolved"].lower().startswith(("http://", "https://")):
            raise ValueError("station.url_resolved must be an http(s) URL")
        if not out["name"]:
            out["name"] = "Unnamed station"
        return out

    def mediahub_report_radio(self, status, station=None, source=None):
        """Dashboard OR the appliance player -> backend: what the radio is
        actually doing.

        TWO SLOTS, one per player. ``radio`` is owned by the
        APPLIANCE (the mpv container, started by voice/chat); ``radio_display``
        is owned by whatever browser is running the kiosk bridge. They are
        different players on different speakers, and the bridge reports its own
        idle state on every mount and heartbeat — writing that into the single
        old slot erased a voice-started station from the tile the moment the
        owner refreshed the dashboard.

        Also refreshes the in-process heartbeat. This is the write path every
        SERVER-side player reports through (voice/chat play via
        ``_radio_play_serverside``, the stop tool), and those paths involve no
        kiosk poll — without the touch, a stale module marker gated their
        perfectly fresh state out of the Now Playing tile for 45s+. Touching
        at the source covers every present and future server-side writer
        instead of one call site.
        """
        from puffpai.media_hub import (
            VALID_RADIO_STATUS, _now_iso, touch_radio_last_seen)

        status = str(status or "").strip().lower()
        if status not in VALID_RADIO_STATUS:
            raise ValueError(
                f"status must be one of {', '.join(VALID_RADIO_STATUS)}")

        clean = self._mediahub_clean_station(station) if station else None
        # "appliance" = the server-side mpv container, which media_hub can
        # verify directly instead of waiting on a heartbeat. Anything else
        # (the dashboard's browser player) stays on the heartbeat gate.
        who = "appliance" if str(source or "").strip().lower() == "appliance" else "display"

        with self._locks['mediahub']:
            data = self._load('mediahub')
            row = {
                "status": status,
                "station": clean,
                "updated_at": _now_iso(),
                "source": who,
            }
            if who == "appliance":
                data['radio'] = row
            else:
                # NEVER data['radio']: a display report says nothing about the
                # appliance's stream, and the bridge sends "idle" on mount.
                data['radio_display'] = row
            data['radio_last_seen'] = _now_iso()
            self._save('mediahub', data)
        touch_radio_last_seen()
        return {"ok": True}

    #: The keys radio discovery owns in the mediahub doc: favorites, play history and the last candidate shortlist.
    #: A CLOSED list, because the player slots above ('radio' / 'radio_display')
    #: carry ownership rules — appliance vs display — that a generic writer
    #: would quietly break. Anything not named here is refused.
    MEDIAHUB_RADIO_KEYS = ("radio_favorites", "radio_history", "radio_candidates")

    def mediahub_set(self, key, value):
        """Write one of the radio-discovery mediahub keys. Never the player slots."""
        if key not in self.MEDIAHUB_RADIO_KEYS:
            raise ValueError(f"mediahub_set refuses key {key!r}")
        if len(json.dumps(value, default=str)) > 64 * 1024:
            raise ValueError("mediahub payload too large")
        with self._locks['mediahub']:
            data = self._load('mediahub')
            data[key] = value
            self._save('mediahub', data)
        return {"ok": True}

    # ── favorite radio stations, the ONE list ─────────────────────────────────
    #
    # The dashboard's World Radio hearts are NOT browser localStorage, which
    # voice cannot see. These four actions let the page read and write the
    # SAME ``radio_favorites`` rows the voice tools use. Every one of them is a
    # thin wrapper: the validation (control characters, http(s)-only stream
    # URLs refused by the player's own check, field caps, the 40-station cap)
    # lives in media_hub beside the rows it protects, and the write goes
    # through mediahub_set — the closed-key writer — like every other radio key.
    # They return a result dict and never raise, so a bad row from a browser is
    # a refusal, not a 500.

    def mediahub_favorites_list(self):
        """Dashboard -> backend: the household's favorite stations."""
        from puffpai import media_hub
        return media_hub.favorites_view(app_data=self)

    def mediahub_favorites_add(self, station=None):
        """Dashboard -> backend: heart this station (add or update in place)."""
        from puffpai import media_hub
        return media_hub.favorites_add(station, app_data=self)

    def mediahub_favorites_remove(self, id=None, url=None):
        """Dashboard -> backend: un-heart by stationuuid or stream URL."""
        from puffpai import media_hub
        return media_hub.favorites_remove(station_id=id, url=url, app_data=self)

    def mediahub_favorites_import(self, stations=None):
        """Dashboard -> backend: merge this browser's localStorage hearts in."""
        from puffpai import media_hub
        return media_hub.favorites_import(stations, app_data=self)

    def mediahub_poll_command(self):
        """Dashboard -> backend: heartbeat + "anything queued for me?".

        Deliberately does NOT clear the command. The player acks only once the
        stream actually opens, so a poll that races a browser reload does not
        drop the request on the floor.

        The heartbeat is memory-only (media_hub.touch_radio_last_seen): this
        fires every 4s for the appliance's lifetime and each persisted write
        is a whole-file truncate+rewrite on flash. Commands past their TTL are
        expired here instead of delivered — a lost ack must not make the kiosk
        start playing on its own at the next reload, hours later.
        """
        from puffpai.media_hub import (
            touch_radio_last_seen, _age_seconds, RADIO_COMMAND_TTL_SECONDS)

        touch_radio_last_seen()
        with self._locks['mediahub']:
            data = self._load('mediahub')
            command = data.get('radio_command')
            if isinstance(command, dict):
                age = _age_seconds(command.get('issued_at'))
                if age is None or age > RADIO_COMMAND_TTL_SECONDS:
                    data['radio_command'] = None
                    self._save('mediahub', data)
                    command = None
            elif command is not None:
                command = None
        return command

    def mediahub_ack_command(self, id, status, station=None):
        """Dashboard -> backend: "I acted on command <id>, here's the result."

        The id check makes the ack idempotent and stops a late ack from
        clearing a newer command that superseded the one being acked.

        An ack comes from a DISPLAY by definition, so it lands in the
        display slot and never touches the appliance's ``radio`` row.
        """
        from puffpai.media_hub import VALID_RADIO_STATUS, _now_iso

        status = str(status or "").strip().lower()
        if status not in VALID_RADIO_STATUS:
            raise ValueError(
                f"status must be one of {', '.join(VALID_RADIO_STATUS)}")

        clean = self._mediahub_clean_station(station) if station else None
        cmd_id = str(id or "").strip()

        with self._locks['mediahub']:
            data = self._load('mediahub')
            pending = data.get('radio_command')
            if not isinstance(pending, dict):
                pending = {}
            cleared = bool(cmd_id) and pending.get('id') == cmd_id
            if cleared:
                data['radio_command'] = None
            data['radio_display'] = {
                "status": status,
                "station": clean,
                "updated_at": _now_iso(),
                "source": "display",
            }
            data['radio_last_seen'] = _now_iso()
            self._save('mediahub', data)
        return {"ok": True, "cleared": cleared}

    def mediahub_radio_request(self, action, station=None):
        """Any surface -> the radio: queue a play/stop for the browser player,
        and on a stop ALSO silence the server-side appliance player.

        This is the one function every stop passes through — voice
        (``media_hub.tool_radio_stop``), the LLM tool, and the dashboard /
        media page via ``POST /apps/mediahub/action`` — so the appliance kill
        belongs here rather than in any single caller. Previously a stop from
        a browser only queued a command for the KIOSK player, and
        a station started by voice (which plays through the ``puffpai-radio``
        mpv container, see ``radio_player.play``) kept streaming with no
        control that could stop it.

        ``pause`` is accepted as a synonym for ``stop``: the appliance player
        has no pause (it is a stream, not a file) and the kiosk bridge only
        understands play/stop, so "pause" means "make it stop making sound".
        """
        import uuid
        from puffpai.media_hub import _now_iso

        action = str(action or "").strip().lower()
        if action == "pause":
            action = "stop"
        if action not in ("play", "stop"):
            raise ValueError("action must be 'play', 'pause' or 'stop'")

        if action == "stop":
            # Outside the lock: this shells out to docker and must not block
            # readers. Idempotent (``docker rm -f`` on a missing container) and
            # silent when docker/the image is absent — radio_player.stop()
            # swallows its own errors and returns False.
            try:
                from puffpai import radio_player
                radio_player.stop()
            except Exception:
                pass

        clean = None
        if action == "play":
            if not station:
                raise ValueError("station is required for action 'play'")
            clean = self._mediahub_clean_station(station)

        command = {
            "id": uuid.uuid4().hex,
            "action": action,
            "station": clean,
            "issued_at": _now_iso(),
        }

        with self._locks['mediahub']:
            data = self._load('mediahub')
            data['radio_command'] = command
            # Optimistic: the tile shows intent immediately instead of sitting
            # on stale state for a poll interval. The ack corrects it.
            if action == "play":
                now = _now_iso()
                data['radio'] = {"status": "connecting", "station": clean,
                                 "updated_at": now}
                # Radio is the one now-playing source with no server-authoritative
                # heartbeat, so the aggregate stale-gates any state whose
                # radio_last_seen is not ALSO fresh. Stamp the persisted fallback
                # AND touch the in-process marker so this just-issued command
                # shows immediately instead of being gated out until the kiosk acks.
                data['radio_last_seen'] = now
                try:
                    from puffpai.media_hub import touch_radio_last_seen
                    touch_radio_last_seen()
                except Exception:
                    pass
            else:
                # The appliance player is gone as of a moment ago, so say so
                # instead of leaving the tile on a station nobody can hear.
                # Same write the voice stop path makes via
                # mediahub_report_radio("idle", source="appliance").
                #
                # NO heartbeat stamp here, unlike the play branch: the
                # heartbeat is the DISPLAY's liveness signal, and faking it
                # would make tool_radio_stop tell the owner a dead kiosk had
                # just checked in. An idle row is never stale-gated anyway.
                data['radio'] = {"status": "idle", "station": None,
                                 "updated_at": _now_iso(),
                                 "source": "appliance"}
            self._save('mediahub', data)
        return command

    def mediahub_spotify_event(self, payload):
        """librespot -> backend: one player event. Must never raise on shape."""
        from puffpai.media_hub import normalize_spotify_event

        try:
            if len(json.dumps(payload, default=str)) > 16384:
                raise ValueError("spotify payload too large")
        except (TypeError, ValueError) as exc:
            if "too large" in str(exc):
                raise
            payload = {}  # unserialisable input is not worth a 500

        with self._locks['mediahub']:
            data = self._load('mediahub')
            data['spotify'] = normalize_spotify_event(
                payload, previous=data.get('spotify'))
            self._save('mediahub', data)
        return {"ok": True}

# ── Module-level app launcher helpers ─────────────────────────────────

# Map from app_id → HTML filename (relative to _APPS_DIR)
_APP_FILES = {
    "alarm": "alarm.html",
    "calendar": "Calendar.html",
    "freshtable": "freshtable.html",
    "fitfam": "FitFam Index.html",
    "trivia": "trivia-app.html",
}


def get_app_paths() -> dict[str, str]:
    """Return ``{app_id: absolute_path}`` for every app whose HTML file exists.

    Example::

        >>> paths = get_app_paths()
        >>> paths.get("alarm")
        'C:\\Users\\owner\\...\\Applications\\alarm.html'
    """
    found: dict[str, str] = {}
    for app_id, filename in _APP_FILES.items():
        full = os.path.join(_APPS_DIR, filename)
        if os.path.isfile(full):
            found[app_id] = full
    return found


def open_app(app_id: str) -> bool:
    """Open an HTML application in the default browser.

    Returns ``True`` if the file was found and opened, ``False`` otherwise.
    """
    import webbrowser

    filename = _APP_FILES.get(app_id)
    if not filename:
        return False

    full = os.path.join(_APPS_DIR, filename)
    if not os.path.isfile(full):
        return False

    webbrowser.open(f"file:///{full}")
    return True
