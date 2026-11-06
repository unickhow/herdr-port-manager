import { View, div } from "gpui-kit";
import { v_flex, h_flex, InputState, Input } from "gpui-base";
import { exec, appHome, theme } from "herdr";
import { isSystem, keyOf, pinOf, listenerPin, reconcile, targets, changedNote } from "./pin.js";

// Port Manager panel: lists what is listening on this computer, who holds it, and stops it on request.
// Everything goes through port-manager.py; nothing leaves this machine.

const REFRESH_MS = 10000;
// Must equal VERSION in port-manager.py. Herdr never updates an installed mod, so a half-updated
// folder (new panel, old script, or the reverse) is detected and reported instead of failing oddly.
const VERSION = 2;
const script = () => `${appHome()}/mods/port-manager/port-manager.py`;

// Optional ways to share a port. The panel never installs them: it only checks they exist.
const SHARE = [
  { key: "cloudflared", name: "Cloudflare Tunnel", visibility: "Public: anyone with the link", warn: true,
    install: "brew install cloudflared", note: "No account. New random URL on every start." },
  { key: "devtunnel", name: "Dev Tunnels", visibility: "Private: only you, signed in", warn: false,
    install: "brew install --cask devtunnel", note: "Sign in once. Other devices sign in with the same account." },
];

// The shell has no setTimeout; wait with Herdr's own timer.
const sleep = (cx, ms) => new Promise((ok) => cx.timer.after(ms, ok));

export default class PortManager extends View {
  init(_props, cx) {
    this.data = null;
    this.error = null;
    this.loading = false;
    this.polling = false; // a tunnel status poll is in flight; load() and polls never overlap
    this.again = false; // a load was asked for while busy: run it once the current one ends
    // Pinned identities (see pin.js), never bare pids: what is confirmed is what is sent.
    this.selected = new Map(); // keyOf(pin) -> { pid, port, start, name, sys }
    this.pending = null; // { sig: "TERM" | "KILL", pins }
    this.note = null;
    this.showUdp = false;
    this.showSystem = false;
    this.query = "";
    this.share = null; // { pid, start, port }: the listener pinned when Share was opened
    this.copied = null;
    this.busy = new Set();
    this.showQr = true;
    this.share_msg = null;
    this.input = InputState.new({ placeholder: "Filter by port, name, pid or folder" });
    this.input.on("change", (_e, cx) => { this.query = this.input.value(); cx.notify(); });
    this.load(cx);
    cx.timer.every(REFRESH_MS, (cx) => this.load(cx));
  }

  // Never two loads (or a load and a poll) at once; a load asked for meanwhile runs right after.
  load(cx) {
    if (this.loading || this.polling) { this.again = true; return; }
    this.loading = true;
    this.again = false;
    cx.spawn(async (cx) => {
      try {
        await this.fetch();
        this.error = null;
      } catch (e) {
        this.error = String(e.message ?? e);
      } finally {
        this.loading = false;
        cx.notify();
        if (this.again) this.load(cx);
      }
    });
  }

  async fetch() {
    const r = await exec("python3", [script(), "list"]);
    if (r.code !== 0) throw new Error((r.stderr || "port-manager.py failed").trim().slice(0, 200));
    this.data = JSON.parse(r.stdout);
    this.reconcile();
  }

  // Drops every pinned process that is no longer the same one (another start time behind its pid,
  // or it no longer holds the pinned port), from the selection, the confirm dialog (cancelled when
  // nothing is left) and the share panel, and says so.
  reconcile() {
    const r = reconcile(this.selected, this.pending, this.share, this.data?.processes ?? []);
    this.selected = r.selected;
    this.pending = r.pending;
    this.share = r.share;
    const msgs = [];
    if (r.dropped) msgs.push(changedNote(r.dropped));
    if (r.shareChanged) { msgs.push("the process you chose to share exited or changed; press Share again"); this.share_msg = null; }
    if (msgs.length) { const m = msgs.join("; "); this.note = { bad: true, text: m[0].toUpperCase() + m.slice(1) }; }
  }

  toggleSelect(p, cx) {
    const pin = pinOf(p);
    if (!pin) {
      this.note = { bad: true, text: `Cannot select pid ${p.pid}: its start time could not be read` };
    } else if (this.selected.has(keyOf(pin))) {
      this.selected.delete(keyOf(pin));
      this.note = null;
    } else {
      this.selected.set(keyOf(pin), pin);
      this.note = null;
    }
    this.reconcile(); // the card may already be older than the latest data
    cx.notify();
  }

  // Opens the confirm dialog for these pins (taken when the button was pressed).
  ask(sig, pins, cx) {
    const ok = pins.filter(Boolean);
    if (ok.length < pins.length) {
      this.note = { bad: true, text: "Cannot stop it: its start time could not be read" };
    } else if (ok.length) {
      this.pending = { sig, pins: ok };
      this.note = null;
      this.reconcile();
    }
    cx.notify();
  }

  held() {
    return new Set((this.data?.processes ?? []).flatMap((p) => p.ports.map((x) => x.port)));
  }

  // Shares shown at the top of the list because they need attention: stopped because the shared
  // service exited; status unknown (kept, never silently forgotten); still sharing a port nothing
  // listens on any more; started by an older version without a known link; or by a newer version.
  notices() {
    const held = this.held();
    return (this.data?.tunnels ?? []).filter((tn) => tn.newer || tn.uncertain || (!tn.running && tn.ended) ||
      (tn.running && (!held.has(tn.port) || (tn.legacy && !tn.url))));
  }

  notice(t, tn) {
    const key = `${tn.provider}-${tn.port}`;
    if (tn.newer) {
      // This version never acts on it, so there is no Stop button that could only refuse.
      return v_flex().gap(6).p(10).rounded(8).bg(t.raised).border(1).border_color(t.ansi3)
        .child(div().text_size(11).text_color(t.ansi3).child(`Sharing :${tn.port}: ${tn.error}`))
        .when(Boolean(tn.url), (el) => el.child(div().text_size(10.5).text_color(t.dim).truncate().child(tn.url)))
        .child(div().text_size(10.5).text_color(t.dim).child("This version cannot stop it."));
    }
    if (tn.running && tn.url) {
      // A watched share stops on its own after the grace period; an older version's only with Stop sharing.
      return v_flex().gap(4)
        .child(div().text_size(10.5).text_color(t.ansi3).child(tn.watch
          ? `Still sharing :${tn.port}, but its process has exited. Stops in a few seconds unless it comes back.`
          : `Still sharing :${tn.port}, but nothing is listening on it any more. It was started by an older version and only stops with Stop sharing.`))
        .child(this.link(t, tn));
    }
    const ended = !tn.running && tn.ended;
    // Try again ties the new share to the listener shown now (pinned), and only when it is mine.
    const owner = ended ? listenerPin(this.data?.processes, tn.port) : null;
    const text = ended ? `Stopped sharing :${tn.port}: ${tn.ended}`
      : tn.uncertain ? `Sharing :${tn.port}: ${tn.error}`
      : `Sharing :${tn.port} (started by an older version; its link is unknown).`;
    const color = ended ? t.ansi1 : t.ansi3;
    return v_flex().gap(6).p(10).rounded(8).bg(t.raised).border(1).border_color(color)
      .child(div().text_size(11).text_color(color).child(text))
      .when(Boolean(tn.url), (el) => el.child(div().text_size(10.5).text_color(t.dim).truncate().child(tn.url)))
      .child(h_flex().gap(6).justify_end()
        .when(Boolean(owner), (el) => el.child(this.button(t, `again-${key}`, this.busy.has(key) ? "Starting…" : "Try again", {
          bg: t.solid, fg: t.solid_foreground, border: t.solid, onClick: (_e, cx) => this.startTunnel(tn.provider, tn.port, owner, cx) })))
        .child(ended
          ? this.button(t, `dismiss-${key}`, "Dismiss", { onClick: (_e, cx) => this.stopTunnel(tn, cx) })
          : this.button(t, `nstop-${key}`, this.busy.has(`stop:${key}`) ? "Stopping…" : "Stop sharing", {
            fg: t.ansi1, border: t.ansi1, bg: t.surface, onClick: (_e, cx) => this.stopTunnel(tn, cx) })));
  }

  // Reads only the tunnel state (no port scan). Skipped while a full load is in flight.
  async pollTunnels(cx) {
    if (this.loading || this.polling) return;
    this.polling = true;
    try {
      const r = await exec("python3", [script(), "tunnel", "status"]);
      if (r.code !== 0) throw new Error((r.stderr || "port-manager.py failed").trim().slice(0, 200));
      const tunnels = JSON.parse(r.stdout).tunnels;
      if (this.data) this.data.tunnels = tunnels;
    } finally {
      this.polling = false;
      if (this.again) this.load(cx);
    }
  }

  tunnel(provider, port) {
    return (this.data?.tunnels ?? []).find((x) => x.provider === provider && x.port === port);
  }

  // Starts a tunnel tied to `owner`, the listener pinned when the user chose to share ({ pid, start }):
  // port-manager.py refuses if the port's listener is no longer that process. Then polls until it
  // has a link or has failed.
  startTunnel(provider, port, owner, cx) {
    const key = `${provider}-${port}`;
    if (this.busy.has(key) || this.busy.has(`stop:${key}`)) return;
    if (!owner || !Number.isInteger(owner.pid) || !Number.isInteger(owner.start)) {
      this.share_msg = `Could not share :${port}: its process's start time could not be read`;
      this.note = { bad: true, text: this.share_msg };
      cx.notify();
      return;
    }
    this.busy.add(key);
    this.share_msg = null;
    cx.notify();
    cx.spawn(async (cx) => {
      try {
        const r = JSON.parse((await exec("python3", [script(), "tunnel", "start", provider, String(port), String(owner.pid), String(owner.start)])).stdout);
        // A refusal (nothing listening, the service exited while starting, …) is shown in the
        // share section and in the bottom bar, which is visible even when that section is closed.
        if (!r.ok) {
          this.share_msg = `Could not share :${port}: ${r.why}`;
          this.note = { bad: true, text: this.share_msg };
        }
        for (let i = 0; i < 30 && r.ok; i++) {
          await sleep(cx, 1000);
          await this.pollTunnels(cx);
          cx.notify();
          const tn = this.tunnel(provider, port);
          if (!tn || tn.url || !tn.running) break;
        }
      } catch (e) {
        this.share_msg = String(e.message ?? e);
      } finally {
        this.busy.delete(key);
        cx.notify();
      }
    });
  }

  // Stops (or dismisses) a share; ignored while a start or stop of it is in flight. `tn` is the share
  // as rendered: its id goes along, so a share restarted since (by another panel or the CLI) is
  // refused by port-manager.py rather than stopped.
  stopTunnel(tn, cx) {
    const { provider, port, id } = tn;
    const key = `stop:${provider}-${port}`;
    if (this.busy.has(key) || this.busy.has(`${provider}-${port}`)) return;
    this.busy.add(key);
    cx.notify();
    cx.spawn(async (cx) => {
      let restarted = false;
      try {
        const args = ["tunnel", "stop", provider, String(port), ...(typeof id === "string" && id ? [id] : [])];
        const r = JSON.parse((await exec("python3", [script(), ...args])).stdout);
        // The bottom bar is visible even when the share section is closed.
        if (!r.ok) this.note = { bad: true, text: `Could not stop sharing :${port}: ${r.why}` };
        restarted = Boolean(r.restarted);
      } catch (e) {
        this.note = { bad: true, text: `Could not stop sharing :${port}: ${String(e.message ?? e)}` };
      } finally {
        this.busy.delete(key);
      }
      if (restarted) {
        this.load(cx); // show the share as it is now
      } else {
        try { await this.pollTunnels(cx); } catch (_e) { /* the next refresh shows it */ }
      }
      cx.notify();
    });
  }

  signIn(cx) {
    if (this.busy.has("signin")) return;
    this.busy.add("signin");
    cx.spawn(async (cx) => {
      try {
        const r = JSON.parse((await exec("python3", [script(), "tunnel", "login", "devtunnel"])).stdout);
        this.share_msg = r.ok ? "Finish signing in in your browser, then press Start again." : `Could not sign in: ${r.why}`;
      } catch (e) {
        this.share_msg = `Could not sign in: ${String(e.message ?? e)}`;
      } finally {
        this.busy.delete("signin");
        cx.notify();
      }
    });
  }

  visible() {
    const rows = (this.data?.processes ?? [])
      .map((p) => ({ ...p, ports: p.ports.filter((x) => this.showUdp || x.proto === "TCP") }))
      .filter((p) => p.ports.length)
      .filter((p) => this.showSystem || !isSystem(p));
    const q = this.query.trim().toLowerCase().replace(/^:/, "");
    if (!q) return rows;
    return rows.filter((p) => q.split(/\s+/).every((w) =>
      [p.name, String(p.pid), p.cwd, p.command, ...p.ports.map((x) => String(x.port))].some((f) => (f || "").toLowerCase().includes(w))));
  }

  // Sends exactly the pins of the open confirm dialog (as reconciled with the latest refresh):
  // PID:PORT:START, which kill re-checks right before signalling. Never re-read from newer data.
  run(cx) {
    const pending = this.pending;
    if (!pending || this.busy.has("kill")) return;
    const { sig, pins } = pending;
    this.pending = null;
    this.busy.add("kill");
    cx.spawn(async (cx) => {
      try {
        const r = await exec("python3", [script(), "kill", sig, ...targets(pins)]);
        const res = JSON.parse(r.stdout).results;
        const bad = res.filter((x) => !x.ok);
        const done = res.length - bad.length;
        this.note = bad.length
          ? { bad: true, text: `${done ? `Stopped ${done}; c` : "C"}ould not stop ${bad.map((x) => `${x.pid} (${x.why})`).join(", ")}` }
          : { bad: false, text: `${sig === "KILL" ? "Force killed" : "Stopped"} ${res.length} process${res.length > 1 ? "es" : ""}` };
      } catch (e) {
        this.note = { bad: true, text: String(e.message ?? e) };
      } finally {
        for (const pin of pins) this.selected.delete(keyOf(pin));
        this.busy.delete("kill");
      }
      cx.notify();
      await sleep(cx, 700);
      this.load(cx);
    });
    cx.notify();
  }

  button(t, id, label, { bg, fg, border, onClick }) {
    return div().id(id).h(24).px(9).flex().items_center().rounded(6).text_size(11.5).flex_none().cursor_pointer()
      .bg(bg ?? t.raised).text_color(fg ?? t.foreground).border(1).border_color(border ?? t.border)
      .hover((s) => s.opacity(0.85)).on_click(onClick).child(label);
  }

  toggle(t, id, label, on, flip) {
    return div().id(id).h(22).px(8).flex().items_center().rounded(11).text_size(11).flex_none().cursor_pointer()
      .border(1).border_color(on ? t.accent : t.border).text_color(on ? t.accent : t.muted)
      .on_click((_e, cx) => { flip(); cx.notify(); }).child(label);
  }

  copy(text, id, cx) {
    cx.write_to_clipboard(text);
    this.copied = id;
    cx.notify();
    cx.timer.after(1800, (cx) => { if (this.copied === id) { this.copied = null; cx.notify(); } });
  }

  // QR codes are always dark on white, whatever the theme, so phones can read them.
  qr(rows, cell) {
    const runs = (row) => {
      const out = [];
      for (let i = 0; i < row.length;) {
        let j = i;
        while (j < row.length && row[j] === row[i]) j++;
        out.push([row[i] === "1", j - i]);
        i = j;
      }
      return out;
    };
    return v_flex().p(cell * 3).rounded(6).bg("#ffffff").flex_none()
      .children(rows.map((row) => h_flex().h(cell).children(runs(row).map(([dark, n]) => div().w(n * cell).h(cell).flex_none().bg(dark ? "#000000" : "#ffffff")))));
  }

  link(t, tn, cx) {
    const id = `link-${tn.provider}-${tn.port}`;
    return v_flex().gap(6).p(8).rounded(6).bg(t.raised).border(1).border_color(t.ansi2)
      .child(h_flex().gap(6).items_center()
        .child(div().text_size(10.5).text_color(t.ansi2).flex_none().child(`Sharing :${tn.port}`))
        .child(div().flex_1())
        .child(div().text_size(10.5).text_color(tn.provider === "cloudflared" ? t.ansi3 : t.dim).flex_none()
          .child(tn.provider === "cloudflared" ? "Public" : "Private")))
      .child(div().id(`${id}-url`).text_size(11.5).text_color(t.accent).cursor_pointer().truncate()
        .on_click((_e, cx) => cx.open_url(tn.url)).child(tn.url))
      .when(this.showQr && Boolean(tn.qr), (el) => el.child(h_flex().justify_center().child(this.qr(tn.qr, 4))))
      .child(h_flex().gap(6).justify_end()
        .child(this.button(t, `${id}-qr`, this.showQr ? "Hide QR" : "QR", { onClick: (_e, cx) => { this.showQr = !this.showQr; cx.notify(); } }))
        .child(this.button(t, `${id}-copy`, this.copied === id ? "Copied" : "Copy", { onClick: (_e, cx) => this.copy(tn.url, id, cx) }))
        .child(this.button(t, `${id}-open`, "Open", { onClick: (_e, cx) => cx.open_url(tn.url) }))
        .child(this.button(t, `${id}-stop`, this.busy.has(`stop:${tn.provider}-${tn.port}`) ? "Stopping…" : "Stop sharing", { fg: t.ansi1, border: t.ansi1, bg: t.surface, onClick: (_e, cx) => this.stopTunnel(tn, cx) })));
  }

  sharePanel(t, p) {
    const tcp = p.ports.filter((x) => x.proto === "TCP");
    const port = tcp.some((x) => x.port === this.share.port) ? this.share.port : tcp[0].port;
    // The listener pinned when Share was opened, not whatever holds the port by the time of the click.
    const owner = { pid: this.share.pid, start: this.share.start };
    const tools = this.data?.tools ?? {};
    return v_flex().gap(8).p(10).rounded(6).bg(t.raised)
      .child(div().text_size(11.5).font_semibold().text_color(t.foreground).child(`Share port ${port} with other devices`))
      .when(tcp.length > 1, (el) => el.child(h_flex().gap(4).flex_wrap().children(tcp.map((x) =>
        this.toggle(t, `sp-${p.pid}-${x.port}`, String(x.port), x.port === port, () => { this.share = { ...owner, port: x.port }; })))))
      .children(SHARE.map((s) => {
        const have = Boolean(tools[s.key]);
        const tn = this.tunnel(s.key, port);
        const atTop = tn?.newer || tn?.uncertain || (tn?.running && tn.legacy && !tn.url); // see notice()
        const starting = this.busy.has(`${s.key}-${port}`) || (tn?.running && !tn.url);
        const sharing = Boolean(tn?.running && tn.url);
        const failed = tn && !tn.running && !tn.uncertain && !tn.newer;
        let action;
        if (!have) action = this.button(t, `inst-${p.pid}-${s.key}`, this.copied === `i-${s.key}` ? "Copied" : "Copy install command", { onClick: (_e, cx) => this.copy(s.install, `i-${s.key}`, cx) });
        else if (sharing) action = div().text_size(11).text_color(t.ansi2).child("Sharing");
        else if (atTop) action = div().text_size(11).text_color(t.ansi3).child("See the top of the list");
        else if (starting) action = div().text_size(11).text_color(t.muted).child("Starting…");
        else action = this.button(t, `start-${p.pid}-${s.key}`, failed ? "Try again" : "Start sharing", { bg: t.solid, fg: t.solid_foreground, border: t.solid, onClick: (_e, cx) => this.startTunnel(s.key, port, owner, cx) });
        return v_flex().gap(4)
          .child(h_flex().gap(8).items_center()
            .child(div().flex_1().text_size(12).text_color(have ? t.foreground : t.muted).child(s.name))
            .child(div().text_size(10.5).text_color(have ? t.ansi2 : t.dim).flex_none().child(have ? "Installed" : "Not installed")))
          .child(div().text_size(10.5).text_color(s.warn ? t.ansi3 : t.muted).child(s.visibility))
          .child(div().text_size(10.5).text_color(t.dim).child(have ? s.note : `Install first: ${s.install}`))
          .when(failed, (el) => el.child(div().text_size(10.5).text_color(t.ansi1).child(tn.ended ? `Stopped sharing: ${tn.ended}` : `Could not start: ${tn.error}`)))
          .when(failed && tn.needs_login && s.key === "devtunnel", (el) => el.child(h_flex().justify_end()
            .child(this.button(t, "signin", "Sign in", { onClick: (_e, cx) => this.signIn(cx) }))))
          .child(h_flex().justify_end().child(action));
      }))
      .when(Boolean(this.share_msg), (el) => el.child(div().text_size(10.5).text_color(t.ansi3).child(this.share_msg)))
      .child(div().text_size(10.5).text_color(t.dim).child("Traffic passes through the provider's servers. Links stay up until you stop sharing or the shared process exits."));
  }

  chip(t, text, color) {
    return div().h(18).px(6).flex().items_center().rounded(5).text_size(11).flex_none().bg(t.raised).text_color(color).child(text);
  }

  row(t, p) {
    const sys = isSystem(p);
    const sel = this.selected.has(keyOf(p));
    const exposed = p.ports.some((x) => x.scope !== "local");
    const where = p.cwd && p.cwd !== "/" ? p.cwd : "";
    const cmd = (p.command || "").replace(/\s+/g, " ").slice(0, 140);
    const box = p.mine
      ? div().id(`sel-${p.pid}`).size(15).flex_none().rounded(4).border(1).border_color(sel ? t.accent : t.input).bg(sel ? t.accent : t.surface)
          .flex().items_center().justify_center().text_size(10).text_color(t.accent_foreground).cursor_pointer()
          .on_click((_e, cx) => this.toggleSelect(p, cx))
          .child(sel ? "✓" : "")
      : div().size(15).flex_none();
    return v_flex().gap(6).p(10).rounded(8).bg(t.surface).border(1).border_color(sel ? t.accent : t.border)
      .child(h_flex().gap(8).items_center()
        .child(box)
        .child(h_flex().gap(4).flex_1().min_w(0).flex_wrap()
          .children(p.ports.map((x) => this.chip(t, `${x.port}${x.proto === "UDP" ? " udp" : ""}`, x.scope === "local" ? t.foreground : t.ansi3))))
        .child(div().text_size(11).text_color(t.dim).flex_none().child(`pid ${p.pid}`)))
      .child(h_flex().gap(6).items_center()
        .child(div().text_size(12.5).font_semibold().text_color(t.foreground).truncate().child(p.name))
        .child(div().flex_1())
        .when(exposed, (el) => el.child(div().text_size(10.5).text_color(t.ansi3).flex_none().child("open to network")))
        .when(sys, (el) => el.child(div().text_size(10.5).text_color(t.dim).flex_none().child("system")))
        .when(!p.mine, (el) => el.child(div().text_size(10.5).text_color(t.dim).flex_none().child("other user"))))
      .when(Boolean(where), (el) => el.child(div().text_size(11).text_color(t.muted).truncate().child(where)))
      .child(div().text_size(10.5).text_color(t.dim).truncate().child(cmd))
      .children((this.data?.tunnels ?? []).filter((tn) => tn.running && tn.url && !tn.newer && p.ports.some((x) => x.port === tn.port)).map((tn) => this.link(t, tn)))
      .when(this.sharing(p), (el) => el.child(this.sharePanel(t, p)))
      .when(p.mine, (el) => el.child(h_flex().gap(6).items_center()
        .child(div().flex_1().text_size(10.5).text_color(t.dim).child(p.running ? `running ${p.running}` : ""))
        .when(p.ports.some((x) => x.proto === "TCP"), (el) => el.child(this.button(t, `share-${p.pid}`, "Share", {
          bg: this.sharing(p) ? t.selected : t.raised, onClick: (_e, cx) => this.toggleShare(p, cx) })))
        .child(this.button(t, `stop-${p.pid}`, "Stop", { onClick: (_e, cx) => this.ask("TERM", [pinOf(p)], cx) }))
        .child(this.button(t, `force-${p.pid}`, "Force kill", { fg: t.ansi1, border: t.ansi1, bg: t.surface, onClick: (_e, cx) => this.ask("KILL", [pinOf(p)], cx) }))));
  }

  // The share panel is open on this card, for this very process (pid and start time).
  sharing(p) {
    return Boolean(this.share) && this.share.pid === p.pid && this.share.start === p.start &&
      p.ports.some((x) => x.proto === "TCP");
  }

  // Opening Share pins the listener the user is looking at.
  toggleShare(p, cx) {
    if (this.sharing(p)) {
      this.share = null;
    } else {
      const port = p.ports.find((x) => x.proto === "TCP").port;
      const pin = pinOf(p, port);
      if (pin) this.share = { pid: pin.pid, start: pin.start, port };
      else this.note = { bad: true, text: `Cannot share pid ${p.pid}: its start time could not be read` };
    }
    this.share_msg = null;
    cx.notify();
  }

  bar(t, rows) {
    if (this.pending) {
      // Everything here comes from the pins, i.e. what the user picked, not from newer data.
      const { sig, pins } = this.pending;
      const list = pins.map((p) => `${p.name} :${p.port} (pid ${p.pid})`);
      const sys = pins.some((p) => p.sys);
      return v_flex().gap(8).p(12).flex_none().border_t(1).border_color(t.border).bg(t.raised)
        .child(div().text_size(12.5).font_semibold().text_color(sig === "KILL" ? t.ansi1 : t.foreground)
          .child(`${sig === "KILL" ? "Force kill" : "Stop"} ${pins.length} process${pins.length > 1 ? "es" : ""}?`))
        .child(div().text_size(11).text_color(t.muted).child(list.slice(0, 4).join(" · ") + (list.length > 4 ? ` · +${list.length - 4} more` : "")))
        .when(sig === "KILL", (el) => el.child(div().text_size(10.5).text_color(t.dim).child("Force kill cannot be caught: the process loses unsaved work.")))
        .when(sys, (el) => el.child(div().text_size(10.5).text_color(t.ansi3).child("Includes a macOS system process.")))
        .child(h_flex().gap(8).justify_end()
          .child(this.button(t, "cancel", "Cancel", { onClick: (_e, cx) => { this.pending = null; cx.notify(); } }))
          .child(this.button(t, "confirm", sig === "KILL" ? "Force kill" : "Stop", {
            bg: sig === "KILL" ? t.ansi1 : t.solid, fg: sig === "KILL" ? t.background : t.solid_foreground, border: sig === "KILL" ? t.ansi1 : t.solid,
            onClick: (_e, cx) => this.run(cx) })));
    }
    const n = this.selected.size;
    const mine = rows.filter((p) => p.mine).map((p) => pinOf(p)).filter(Boolean);
    const all = mine.length > 0 && mine.every((pin) => this.selected.has(keyOf(pin)));
    const stopping = this.busy.has("kill");
    return h_flex().gap(8).h(44).px(12).flex_none().items_center().border_t(1).border_color(t.border)
      .child(div().flex_1().text_size(11.5).text_color(n || this.note ? t.foreground : t.dim).truncate()
        .child(stopping ? "Stopping…" : this.note ? this.note.text : n ? `${n} selected` : "Select processes to stop several at once"))
      .child(this.button(t, "all", all ? "None" : "Select all", {
        onClick: (_e, cx) => { this.selected = all ? new Map() : new Map(mine.map((pin) => [keyOf(pin), pin])); this.note = null; this.reconcile(); cx.notify(); } }))
      .when(n > 0, (el) => el
        .child(this.button(t, "stop-sel", "Stop", { onClick: (_e, cx) => this.ask("TERM", [...this.selected.values()], cx) }))
        .child(this.button(t, "kill-sel", "Force kill", { fg: t.ansi1, border: t.ansi1, bg: t.surface, onClick: (_e, cx) => this.ask("KILL", [...this.selected.values()], cx) })));
  }

  render() {
    const t = theme();
    const rows = this.visible();
    const refresh = this.button(t, "refresh", this.loading ? "…" : "Refresh", { onClick: (_e, cx) => this.load(cx) });
    return v_flex().id("port-manager-root").size_full().bg(t.surface)
      .child(h_flex().h(42).px(14).flex_none().items_center().gap(8).border_b(1).border_color(t.border)
        .child(div().text_size(13).font_semibold().text_color(t.foreground).child("Port Manager"))
        .child(div().flex_1().text_size(11).text_color(t.dim).child(this.data ? `${rows.length} ${this.query.trim() ? "shown" : "listening"}` : "Loading…"))
        .child(refresh))
      .child(h_flex().px(12).pt(10).flex_none()
        .child(Input.new(this.input).h(30).flex_1().min_w(0).px(10).rounded(8).border_1().border_color(t.input).bg(t.background).text_size(12).text_color(t.foreground)))
      .child(h_flex().gap(6).px(14).py(8).flex_none().items_center()
        .child(this.toggle(t, "t-udp", "UDP", this.showUdp, () => { this.showUdp = !this.showUdp; }))
        .child(this.toggle(t, "t-sys", "System", this.showSystem, () => { this.showSystem = !this.showSystem; }))
        .child(div().flex_1())
        .child(div().text_size(10.5).text_color(t.dim).child("Updates every 10s")))
      .child(v_flex().id("port-manager-list").flex_1().min_h(0).overflow_y_scrollbar().gap(8).px(12).pb(12)
        .when(Boolean(this.data) && this.data.version !== VERSION, (el) => el.child(div().p(10).rounded(8).border(1).border_color(t.ansi3).text_size(11.5).text_color(t.ansi3)
          .child(`Mixed versions: panel ${VERSION}, script ${this.data.version ?? "older"}. Replace the whole mods/port-manager/ folder with one copy; see "Updating" in the README.`)))
        .when(Boolean(this.error), (el) => el.child(div().p(10).rounded(8).border(1).border_color(t.ansi1).text_size(11.5).text_color(t.ansi1).child(`Couldn't read ports: ${this.error}`)))
        .children(this.notices().map((tn) => this.notice(t, tn)))
        .children(rows.map((p) => this.row(t, p)))
        .when(this.data && !rows.length, (el) => el.child(div().p(16).text_size(12).text_color(t.dim).child(this.query.trim() ? `No match for "${this.query.trim()}".` : "Nothing is listening."))))
      .child(this.bar(t, rows));
  }
}
