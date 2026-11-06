// Pure helpers for the panel (no gpui, no herdr), so they can be tested with plain node.
//
// A "pin" is the identity of a process at the moment the user picked it: pid, the port shown on
// its card and its start time (epoch seconds, from `port-manager.py list`). Selections, the
// confirm dialog and the share panel hold pins, never bare pids, and what is sent to
// port-manager.py is always the pinned identity, never re-read from newer data. A refresh that
// shows a different process behind a pinned pid (new start time, or it no longer holds the pinned
// port) drops the pin.

export const isSystem = (p) => /^\/(System|usr\/(libexec|sbin)|sbin)\b/.test(p.command || "");

export const keyOf = (pin) => `${pin.pid}:${pin.start}`;

const validStart = (s) => Number.isInteger(s) && s > 0;

// The pin of a listed process, on `port` (default: the first port on its card). null when it
// cannot be pinned (no start time could be read, or it does not hold that port).
export function pinOf(p, port) {
  if (!p || !Number.isInteger(p.pid) || p.pid <= 0 || !validStart(p.start)) return null;
  const ports = p.ports ?? [];
  const chosen = port ?? ports[0]?.port;
  if (!ports.some((x) => x.port === chosen)) return null;
  return { pid: p.pid, port: chosen, start: p.start, name: p.name ?? "", sys: isSystem(p) };
}

// The listener of a TCP port that a share would be tied to: one of my processes, pinned.
export function listenerPin(processes, port) {
  const p = (processes ?? []).find((x) => x.mine && x.ports.some((y) => y.proto === "TCP" && y.port === port));
  return p ? pinOf(p, port) : null;
}

// Is the process behind this pin still the same one, still holding the pinned port?
export function stillSame(pin, processes, proto) {
  const p = (processes ?? []).find((x) => x.pid === pin.pid);
  return Boolean(p) && p.start === pin.start &&
    p.ports.some((x) => x.port === pin.port && (!proto || x.proto === proto));
}

// Reconciles the pinned selection, confirm dialog and share panel with freshly listed processes.
// Returns new values (inputs are not modified) and how many pinned processes were dropped.
export function reconcile(selected, pending, share, processes) {
  const dropped = new Set();
  const sel = new Map();
  for (const [k, pin] of selected) {
    if (stillSame(pin, processes)) sel.set(k, pin);
    else dropped.add(k);
  }
  let pend = null;
  if (pending) {
    const pins = pending.pins.filter((pin) => {
      if (stillSame(pin, processes)) return true;
      dropped.add(keyOf(pin));
      return false;
    });
    pend = pins.length ? { sig: pending.sig, pins } : null;
  }
  let sh = share;
  let shareChanged = false;
  if (share && !stillSame(share, processes, "TCP")) {
    sh = null;
    shareChanged = true;
  }
  return { selected: sel, pending: pend, share: sh, dropped: dropped.size, shareChanged };
}

// What `port-manager.py kill` is given: exactly the pinned identities.
export const targets = (pins) => pins.map((p) => `${p.pid}:${p.port}:${p.start}`);

export const changedNote = (n) =>
  `${n} process${n > 1 ? "es" : ""} exited or changed and ${n > 1 ? "were" : "was"} deselected`;
