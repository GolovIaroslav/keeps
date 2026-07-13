# Ф15 research: active-window detection on KDE Plasma Wayland

Date: 2026-07-13. Scope: identify the application that owned focus immediately
before the Keeps popup opens, then select `Ctrl+V` or `Ctrl+Shift+V`.

## Recommendation

Use `kdotool getactivewindow getwindowclassname` on KDE Plasma Wayland, invoked
**before** showing/focusing the popup. Normalize successful stdout with
`strip().casefold()` and match it against the editable per-app map. Bound the
subprocess to about 250 ms; missing executable, timeout, non-zero status, empty
or malformed output, non-KDE Wayland, or no active window must all silently
select the default `Ctrl+V` path.

This is the smallest reliable option for Keeps. `kdotool` generates a one-shot
KWin script, loads/runs/unloads it over the session D-Bus, and implements the
requested chain directly. Its `getactivewindow` reads `workspace.activeWindow`
and `getwindowclassname` prints `resourceClass` ([README and supported
commands](https://github.com/jinliu/kdotool/blob/v0.2.3/README.md), [implementation
of the KWin properties](https://github.com/jinliu/kdotool/blob/v0.2.3/src/templates.rs)).
KWin's Plasma 6 API exposes `workspace.activeWindow` and the window
`resourceClass`; for native Wayland xdg-toplevels KWin sets `resourceClass` from
the client's xdg `appId` ([KWin scripting API](https://develop.kde.org/docs/plasma/kwin/api/),
[KWin source](https://invent.kde.org/plasma/kwin/-/blob/3ed7f1a3e300019692d2577ec0f14be4cc78e4b0/src/xdgshellwindow.cpp#L1029)).

Local validation on this machine (KWin 6.7.2, Wayland) against current kdotool
source returned the actual active app id and took 5–7 ms across five invocations.
This supports a conservative 250 ms outer timeout while keeping popup startup
bounded. Do not run detection after `show()`: the active window may already be
Keeps, defeating the purpose.

On X11, keep the native equivalent
`xdotool getactivewindow getwindowclassname`. On other Wayland compositors there
is no compositor-independent equivalent in these dependencies; use the default
paste combination.

## Why not the other candidates

- A custom KWin-script D-Bus bridge can expose the same
  `workspace.activeWindow.resourceClass`, but it adds a separately packaged,
  installed and enabled KWin script plus lifecycle/version handling. KDE's
  documented installation flow requires a KPackage, `kpackagetool6`, enabling
  the plugin, and reconfiguring KWin ([KWin scripting tutorial](https://develop.kde.org/docs/plasma/kwin/)).
  It is only worth revisiting if one-shot kdotool latency becomes measurable.
- `org_kde_plasma_window_management` does publish window `app_id` and active
  state, but its own protocol says it is a desktop-environment implementation
  detail that regular clients must not use and that only one client can bind it
  ([protocol](https://invent.kde.org/libraries/plasma-wayland-protocols/-/blob/c421474708c26a409817c255e1c43939351444d8/src/protocols/plasma-window-management.xml)).
  KWin also classifies it as restricted for sandboxed clients
  ([KWin allow-list source](https://invent.kde.org/plasma/kwin/-/blob/3ed7f1a3e300019692d2577ec0f14be4cc78e4b0/src/wayland_server.cpp#L128)).
  Using it would additionally require generated Wayland bindings/KWayland and
  asynchronous registry round-trips. It is not an appropriate Keeps API.

## Runtime and packaging

Treat `kdotool` as an optional KDE-Wayland runtime helper, like `ydotool`, not a
Python dependency. Version 0.2.3 supports Plasma 5 and 6; the current upstream
0.3 branch is Plasma-6-only. On this Arch system `kdotool` is available from AUR
(`kdotool` 0.2.3-1; runtime dependencies dbus/glibc/libgcc), not the official
repositories. `qdbus6` is not a runtime dependency of kdotool: its Rust binary
talks to D-Bus directly. AppImage/source diagnostics should report kdotool as
optional and explain that terminal-aware detection falls back to default paste
when absent.

Expected failure modes are: KWin/session D-Bus unavailable or restarting,
unsupported KDE version, no active window, stale/mismatched app-provided class,
and kdotool's internal D-Bus wait. The outer timeout and default mapping cover
all of them. App classes are supplied by applications, so matching must be
case-insensitive and the Settings table must remain editable; presets should
include observed native IDs such as `org.kde.konsole` as well as the short
aliases requested by the plan.

## Injection commands

For Wayland terminal paste use:

```text
ydotool key 29:1 42:1 47:1 47:0 42:0 29:0
```

`ydotool key` accepts raw `<keycode>:<pressed>` events, where `:1` is press and
`:0` release ([ydotool help/source](https://github.com/ReimuNotMoe/ydotool/blob/708e96ff27e381a8c549418a9d34cdde12305317/Client/tool_key.c)). Linux defines
`KEY_LEFTCTRL=29`, `KEY_LEFTSHIFT=42`, and `KEY_V=47`
([kernel input-event codes](https://github.com/torvalds/linux/blob/master/include/uapi/linux/input-event-codes.h)).
The release order is deliberately V, Shift, Ctrl. `ydotoold` is mandatory since
ydotool 1.0 and needs access to `/dev/uinput`
([upstream runtime notes](https://github.com/ReimuNotMoe/ydotool#runtime)).

For X11 use the named chord:

```text
xdotool key ctrl+shift+v
```

xdotool documents `+`-joined keysyms and modifier aliases for `key`; it uses
XTEST and explicitly does not work correctly as a Wayland injector
([xdotool documentation](https://github.com/jordansissel/xdotool/blob/5c27b117c91bdc4d0f56a71ac4e78c04e4e60dba/xdotool.pod),
[Wayland limitation](https://github.com/jordansissel/xdotool#wayland)). Existing
Keeps behavior remains important: set the clipboard even if injection is
unavailable, return failure from the helper, and notify rather than losing the
copy.
