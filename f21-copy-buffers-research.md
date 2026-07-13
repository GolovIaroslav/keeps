# F21 research: persistent copy buffers on KDE/Wayland

Date: 2026-07-13. Scope: three persistent Ditto-style copy buffers. Each
buffer has global Copy and Paste actions; Cut is deliberately out of scope.

## Recommendation

Implement buffers as a small, separate persistent store plus one Qt controller
that owns each short-lived clipboard transaction. Do **not** reuse `clips`: a
buffer operation must neither move history ordering nor create a history item.
This is the recommended answer to F21's open capture-policy question.

The controller must run every `QClipboard` and SQLite operation on Qt's main
thread. It may run the ydotool/xdotool subprocess in the existing worker pool.
It must never synchronously call `wl-paste` while Keeps owns the clipboard.
Keeps has already observed that exact self-read deadlock: an owner has to serve
the requested bytes, while the main thread is blocked waiting for `wl-paste`.

There is no Wayland protocol acknowledgement meaning "the target application
has finished pasting". Therefore clipboard restoration after buffer Paste is
inherently best-effort, not an atomic guarantee. Keep the temporary source
alive through injection and a conservative post-injection grace period, and
never restore over an intervening external clipboard owner.

## Proposed persistent model

Use schema v5, separate from the history tables:

```sql
CREATE TABLE copy_buffers (
  slot        INTEGER PRIMARY KEY CHECK(slot BETWEEN 1 AND 3),
  kind        TEXT NOT NULL,
  captured_at INTEGER NOT NULL,
  preview     TEXT NOT NULL
);
CREATE TABLE copy_buffer_data (
  slot INTEGER NOT NULL REFERENCES copy_buffers(slot) ON DELETE CASCADE,
  mime TEXT NOT NULL,
  data BLOB NOT NULL,
  PRIMARY KEY(slot, mime)
);
```

`Store.set_copy_buffer(slot, kind, mime_data)`, `get_copy_buffer(slot)`, and
`copy_buffers()` are sufficient. Reuse the existing canonical bundle rules
(`text`, `html`, `image`, `files`) and `build_preview`; do not make F21 an
implementation of F25's arbitrary-MIME fidelity. Empty slots simply make Paste
show a short notification and do nothing. `copy_buffers` deliberately survives
restarts and is not affected by history trim/clear/dedup.

Store the six action assignments in QSettings, not in SQLite: for example
`buffers/1/copy_hotkey`, `buffers/1/paste_hotkey`, etc. Create stable
KGlobalAccel action IDs such as `buffer-1-copy` and `buffer-1-paste`, using the
same registration/conflict/removal discipline as F20's `clip-<id>` actions.
No default key sequences should be invented: they are especially likely to
conflict with desktop or application shortcuts. Settings can show one compact
row per slot (Copy key, Paste key, current preview); this satisfies the plan's
"no separate UI section" intent without a popup workflow.

## Clipboard snapshot and restoration boundary

Before either operation, take an immediate **value copy** of the current
`QClipboard.mimeData()` on the GUI thread:

1. obtain the pointer once;
2. enumerate `formats()`;
3. copy every readable `data(format)` into Python `bytes`;
4. discard the `QMimeData` pointer.

Qt explicitly says a `mimeData()` pointer can be invalidated by the next
clipboard change; retaining it across a write is invalid. `QMimeData` supports
multiple formats and raw `data()`/`setData()` pairs, so the snapshot should
keep every format that Qt exposes, not merely the four history formats. Restore
the snapshot by constructing a fresh `QMimeData` and applying those raw bytes.

This is best-effort preservation of the user's ordinary clipboard, not a
promise of full platform-native fidelity. Qt may expose a converted or
platform-specific representation and F25 is the planned project phase for
general format fidelity. If snapshotting fails, is empty when the platform
reported usable data, or exceeds a clearly bounded total-size limit, abort the
buffer operation rather than knowingly replace the ordinary clipboard.

`QClipboard.ownsClipboard()` is the safety gate before a delayed restoration:
if it is false, an external program took ownership while the operation was in
flight, so cancel restoration and leave the newer external clipboard intact.
Do not use `SelfSetGuard` as the transaction mechanism. A time-window guard can
consume an unrelated user copy; `ownsClipboard()` already suppresses Keeps's
own temporary and restored selections on both current watchers.

## Copy buffer sequence

This flow preserves the old clipboard and captures the source application's
new copy without adding it to normal history.

```text
global Copy(slot)
  -> reject if another buffer transaction is active
  -> snapshot ordinary QClipboard (GUI thread)
  -> arm watcher.capture_next_for_buffer(generation, callback, timeout)
  -> determine F15 target app before any UI steals focus
  -> worker injects Ctrl+C (ydotool/xdotool)
  -> watcher sees the next external clipboard selection
  -> watcher reads its normal canonical bundle, but calls callback before Store.add()
  -> Store.set_copy_buffer(slot, bundle)
  -> restore snapshot immediately, only for this completed transaction
```

Arm the one-shot interception **before** injection. It needs a generation token
and a bounded `QTimer` (for example 1 second); a failure/missing injector or
timeout cancels it and leaves the old clipboard and existing slot untouched.
Only one pending Copy operation is allowed. On Wayland the interception belongs
at the start of `WaylandWatcher._on_triggered()`, before its `ownsClipboard()`
and `SelfSetGuard` returns; it may call the existing timed `_list_types()` /
`build_bundle()` path because the target application, not Keeps, owns this new
selection. On X11 it analogously happens before `Store.add()` in
`X11Watcher._on_changed()`.

The watcher should expose a focused API such as
`capture_next_for_buffer(callback) -> cancellation handle`, not a broad public
pause flag. The callback receives `(kind, mime_data)` and consumes exactly that
watcher event. Normal events continue to call `Store.add()` unchanged. It must
be cleared before setting the restored snapshot, whose watcher event is then
ignored by `ownsClipboard()`.

The raw Wayland `Ctrl+C` sequence is:

```text
ydotool key 29:1 46:1 46:0 29:0
```

where Linux defines `KEY_LEFTCTRL = 29` and `KEY_C = 46`. On X11 use
`xdotool key ctrl+c`. Add this as a sibling to the tested F15 `inject_paste()`
path, retaining the existing timeout and `YDOTOOL_SOCKET` repair. ydotool's
own documentation says that its `key` command takes keycodes and that ydotoold
is mandatory, so missing/failed injection must be a harmless no-op.

The first observed selection after Ctrl+C can theoretically be an unrelated
clipboard update; the protocol provides no active-window identity to prove
origin. Keep the timeout short, make the operation visibly busy, and document
this unavoidable race for manual testing. The restoration happens immediately
after a successful read, minimizing but not eliminating that window.

## Paste buffer sequence

```text
global Paste(slot)
  -> reject if busy or slot is empty
  -> snapshot ordinary QClipboard (GUI thread)
  -> determine F15 target app before changing clipboard
  -> set buffer bundle as fresh QMimeData (Keeps owns it)
  -> wait existing paste/delay_ms
  -> worker injects Ctrl+V or F15's terminal Ctrl+Shift+V
  -> on successful worker completion, retain source for post-paste grace
  -> if Keeps still owns QClipboard, restore snapshot; otherwise leave external owner alone
```

The temporary `QMimeData` must remain the clipboard source until **after** the
injected key and a GUI-thread grace period. A useful conservative initial value
is 500 ms after worker success, made a named constant and not presented as a
guarantee. Do not restore on worker start, and restore immediately on explicit
injection failure because no target should request the source.

This ordering follows the Wayland data-control protocol: the source advertises
MIME types, then a receiving client later requests one and the source writes it
to a file descriptor. The target may request more than one MIME type. Replacing
the selection immediately after ydotool returns can cancel the source before a
slow target reads it. The protocol has no acknowledgement that all target
requests are complete, so a fixed grace is the smallest honest design. A later
external selection flips `ownsClipboard()` to false; cancellation of restoration
then prevents Keeps from overwriting the user's newer copy.

Both the temporary-buffer and restored-snapshot writes are Keeps-owned.
Existing `WaylandWatcher` and `X11Watcher` already ignore those events using
`ownsClipboard()`, so neither must enter history. Do not issue `wl-paste` in
those events: the source is Keeps and this is precisely the deadlock to avoid.

## Safe automated work versus manual acceptance

Safe without a live clipboard or live database:

- v4-to-v5 migration, CRUD, restart persistence, and cascade tests against a
  temporary `Store` database;
- table-driven `copy_command()` and injection failure/timeout tests, mirroring
  `tests/test_paste.py`;
- a pure transaction/state-machine test with fake snapshot/setter/watcher/
  scheduler/injector: success, empty slot, timeout, duplicate invocation,
  watcher interception before history, injection failure, and ownership loss;
- offscreen Qt tests for Settings rows and hotkey conflict reporting;
- source-level tests that `WaylandWatcher` invokes the one-shot callback before
  `Store.add()` and clears it exactly once.

Require manual KDE/Wayland testing before calling the acceptance criterion met:

- Kate Copy buffer 1 -> Firefox Paste buffer 1; confirm the previous normal
  clipboard still pastes afterwards.
- Repeat for plain text, rich text, image, and file URLs within F21's supported
  canonical formats.
- Paste into Konsole and another terminal-map entry to prove F15 selects
  Ctrl+Shift+V; test a non-terminal stays Ctrl+V.
- No selection, missing/broken ydotoold, an empty slot, and a KGlobalAccel
  conflict must fail without changing the clipboard or buffer.
- During Copy and during the paste grace period, deliberately copy different
  text in another app; verify Keeps does not restore over that newer clipboard.
- Restart the daemon and verify all three buffer contents/actions persist and
  no Copy-buffer source content was inserted into History.

## Primary sources

- Qt, [`QClipboard`](https://doc.qt.io/qt-6/qclipboard.html): clipboard modes,
  `ownsClipboard()`, `dataChanged()`, `mimeData()` lifetime, and ownership
  transfer by `setMimeData()`.
- Qt, [`QMimeData`](https://doc.qt.io/qt-6/qmimedata.html): several MIME
  representations, `formats()`, raw `data()`, and `setData()`.
- wl-clipboard, [`wl-paste` source](https://github.com/bugaevc/wl-clipboard/blob/master/src/wl-paste.c): `--watch` spawns the supplied command for each
  selection and reads offered bytes through a pipe after flushing the request.
- wl-clipboard, [`wl-copy` source](https://github.com/bugaevc/wl-clipboard/blob/master/src/wl-copy.c): a clipboard source stays alive to serve paste
  requests (and `--paste-once` exits after a request).
- Wayland protocols, [`ext-data-control-v1.xml`](https://gitlab.freedesktop.org/wayland/wayland-protocols/-/blob/main/staging/ext-data-control/ext-data-control-v1.xml): a selection is an offer; data transfer is a later request over a file descriptor and may occur for several MIME types.
- ydotool, [upstream README](https://github.com/ReimuNotMoe/ydotool#readme):
  `key` uses keycodes and ydotoold is mandatory from v1.0.
- Linux, [`input-event-codes.h`](https://github.com/torvalds/linux/blob/master/include/uapi/linux/input-event-codes.h): `KEY_LEFTCTRL = 29`, `KEY_C = 46`,
  `KEY_V = 47`.
