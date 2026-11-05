import { View, div } from "gpui-kit";
import { v_flex, h_flex, InputState, Input } from "gpui-base";
import { exec, appHome, theme } from "herdr";

// Ports panel: lists what is listening on this computer, who holds it, and stops it on request.
// Everything goes through ports.py; nothing leaves this machine.

const REFRESH_MS = 10000;
const script = () => `${appHome()}/mods/ports/ports.py`;

// Optional ways to share a port. The panel never installs them: it only checks they exist.
const SHARE = [
  { key: "cloudflared", name: "Cloudflare Tunnel", visibility: "Public: anyone with the link", warn: true,
    install: "brew install cloudflared", note: "No account. New random URL on every start." },
  { key: "devtunnel", name: "Dev Tunnels", visibility: "Private: only you, signed in", warn: false,
    install: "brew install --cask devtunnel", note: "Sign in once. Other devices sign in with the same account." },
];

// The shell has no setTimeout; wait with Herdr's own timer.
const sleep = (cx, ms) => new Promise((ok) => cx.timer.after(ms, ok));

const isSystem = (p) => /^\/(System|usr\/(libexec|sbin)|sbin)\b/.test(p.command || "");

export default class Ports extends View {
  init(_props, cx) {
    this.data = null;
    this.error = null;
    this.loading = false;
    this.selected = new Set();
    this.pending = null; // { sig: "TERM" | "KILL", pids: number[] }
    this.note = null;
    this.showUdp = false;
    this.showSystem = false;
    this.query = "";
    this.share = null; // { pid, port }
    this.copied = null;
    this.busy = new Set();
    this.showQr = true;
    this.share_msg = null;
    this.input = InputState.new({ placeholder: "Filter by port, name, pid or folder" });
    this.input.on("change", (_e, cx) => { this.query = this.input.value(); cx.notify(); });
    this.load(cx);
    cx.timer.every(REFRESH_MS, (cx) => this.load(cx));
  }

  load(cx) {
    if (this.loading) return;
    this.loading = true;
    cx.spawn(async (cx) => {
      try {
        await this.fetch();
        this.error = null;
      } catch (e) {
        this.error = String(e.message ?? e);
      } finally {
        this.loading = false;
        cx.notify();
      }
    });
  }

  async fetch() {
    const r = await exec("python3", [script(), "list"]);
    if (r.code !== 0) throw new Error((r.stderr || "ports.py failed").trim().slice(0, 200));
    this.data = JSON.parse(r.stdout);
    const alive = new Set(this.data.processes.map((p) => p.pid));
    for (const pid of [...this.selected]) if (!alive.has(pid)) this.selected.delete(pid);
    if (this.pending) this.pending.pids = this.pending.pids.filter((pid) => alive.has(pid));
    if (this.pending && !this.pending.pids.length) this.pending = null;
  }

  // Shares whose port no longer has a listener (the dev server stopped, or the panel was reopened).
  // They stay public until stopped, so they must stay visible.
  orphans() {
    const held = new Set((this.data?.processes ?? []).flatMap((p) => p.ports.map((x) => x.port)));
    return (this.data?.tunnels ?? []).filter((tn) => tn.running && tn.url && !held.has(tn.port));
  }

  tunnel(provider, port) {
    return (this.data?.tunnels ?? []).find((x) => x.provider === provider && x.port === port);
  }

  // Starts a tunnel, then polls until it has a link or has failed.
  startTunnel(provider, port, cx) {
    this.busy.add(`${provider}-${port}`);
    this.share_msg = null;
    cx.notify();
    cx.spawn(async (cx) => {
      try {
        const r = JSON.parse((await exec("python3", [script(), "tunnel", "start", provider, String(port)])).stdout);
        if (!r.ok) this.share_msg = r.why;
        for (let i = 0; i < 30 && r.ok; i++) {
          await sleep(cx, 1000);
          await this.fetch();
          cx.notify();
          const tn = this.tunnel(provider, port);
          if (!tn || tn.url || !tn.running) break;
        }
      } catch (e) {
        this.share_msg = String(e.message ?? e);
      } finally {
        this.busy.delete(`${provider}-${port}`);
        cx.notify();
      }
    });
  }

  stopTunnel(provider, port, cx) {
    cx.spawn(async (cx) => {
      await exec("python3", [script(), "tunnel", "stop", provider, String(port)]);
      await this.fetch();
      cx.notify();
    });
  }

  signIn(cx) {
    cx.spawn(async (cx) => {
      await exec("python3", [script(), "tunnel", "login", "devtunnel"]);
      this.share_msg = "Finish signing in in your browser, then press Start again.";
      cx.notify();
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

  run(sig, pids, cx) {
    const rows = this.data.processes.filter((p) => pids.includes(p.pid));
    const targets = rows.map((p) => `${p.pid}:${p.ports[0].port}`);
    this.pending = null;
    cx.spawn(async (cx) => {
      try {
        const r = await exec("python3", [script(), "kill", sig, ...targets]);
        const res = JSON.parse(r.stdout).results;
        const bad = res.filter((x) => !x.ok);
        this.note = bad.length
          ? { bad: true, text: `Could not stop ${bad.map((x) => `${x.pid} (${x.why})`).join(", ")}` }
          : { bad: false, text: `${sig === "KILL" ? "Force killed" : "Stopped"} ${res.length} process${res.length > 1 ? "es" : ""}` };
        for (const pid of pids) this.selected.delete(pid);
      } catch (e) {
        this.note = { bad: true, text: String(e.message ?? e) };
      }
      cx.notify();
      await sleep(cx, 700);
      this.loading = false;
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
        .child(this.button(t, `${id}-stop`, "Stop sharing", { fg: t.ansi1, border: t.ansi1, bg: t.surface, onClick: (_e, cx) => this.stopTunnel(tn.provider, tn.port, cx) })));
  }

  sharePanel(t, p) {
    const tcp = p.ports.filter((x) => x.proto === "TCP");
    const port = tcp.some((x) => x.port === this.share.port) ? this.share.port : tcp[0].port;
    const tools = this.data?.tools ?? {};
    return v_flex().gap(8).p(10).rounded(6).bg(t.raised)
      .child(div().text_size(11.5).font_semibold().text_color(t.foreground).child(`Share port ${port} with other devices`))
      .when(tcp.length > 1, (el) => el.child(h_flex().gap(4).flex_wrap().children(tcp.map((x) =>
        this.toggle(t, `sp-${p.pid}-${x.port}`, String(x.port), x.port === port, () => { this.share = { pid: p.pid, port: x.port }; })))))
      .children(SHARE.map((s) => {
        const have = Boolean(tools[s.key]);
        const tn = this.tunnel(s.key, port);
        const starting = this.busy.has(`${s.key}-${port}`) || (tn?.running && !tn.url);
        const sharing = Boolean(tn?.running && tn.url);
        const failed = tn && !tn.running;
        let action;
        if (!have) action = this.button(t, `inst-${p.pid}-${s.key}`, this.copied === `i-${s.key}` ? "Copied" : "Copy install command", { onClick: (_e, cx) => this.copy(s.install, `i-${s.key}`, cx) });
        else if (sharing) action = div().text_size(11).text_color(t.ansi2).child("Sharing");
        else if (starting) action = div().text_size(11).text_color(t.muted).child("Starting…");
        else action = this.button(t, `start-${p.pid}-${s.key}`, failed ? "Try again" : "Start sharing", { bg: t.solid, fg: t.solid_foreground, border: t.solid, onClick: (_e, cx) => this.startTunnel(s.key, port, cx) });
        return v_flex().gap(4)
          .child(h_flex().gap(8).items_center()
            .child(div().flex_1().text_size(12).text_color(have ? t.foreground : t.muted).child(s.name))
            .child(div().text_size(10.5).text_color(have ? t.ansi2 : t.dim).flex_none().child(have ? "Installed" : "Not installed")))
          .child(div().text_size(10.5).text_color(s.warn ? t.ansi3 : t.muted).child(s.visibility))
          .child(div().text_size(10.5).text_color(t.dim).child(have ? s.note : `Install first: ${s.install}`))
          .when(failed, (el) => el.child(div().text_size(10.5).text_color(t.ansi1).child(`Could not start: ${tn.error}`)))
          .when(failed && tn.needs_login && s.key === "devtunnel", (el) => el.child(h_flex().justify_end()
            .child(this.button(t, "signin", "Sign in", { onClick: (_e, cx) => this.signIn(cx) }))))
          .child(h_flex().justify_end().child(action));
      }))
      .when(Boolean(this.share_msg), (el) => el.child(div().text_size(10.5).text_color(t.ansi3).child(this.share_msg)))
      .child(div().text_size(10.5).text_color(t.dim).child("Traffic passes through the provider's servers. Links stay up until you stop sharing."));
  }

  chip(t, text, color) {
    return div().h(18).px(6).flex().items_center().rounded(5).text_size(11).flex_none().bg(t.raised).text_color(color).child(text);
  }

  row(t, p) {
    const sys = isSystem(p);
    const sel = this.selected.has(p.pid);
    const exposed = p.ports.some((x) => x.scope !== "local");
    const where = p.cwd && p.cwd !== "/" ? p.cwd : "";
    const cmd = (p.command || "").replace(/\s+/g, " ").slice(0, 140);
    const box = p.mine
      ? div().id(`sel-${p.pid}`).size(15).flex_none().rounded(4).border(1).border_color(sel ? t.accent : t.input).bg(sel ? t.accent : t.surface)
          .flex().items_center().justify_center().text_size(10).text_color(t.accent_foreground).cursor_pointer()
          .on_click((_e, cx) => { sel ? this.selected.delete(p.pid) : this.selected.add(p.pid); cx.notify(); })
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
      .children((this.data?.tunnels ?? []).filter((tn) => tn.running && tn.url && p.ports.some((x) => x.port === tn.port)).map((tn) => this.link(t, tn)))
      .when(this.share?.pid === p.pid && p.ports.some((x) => x.proto === "TCP"), (el) => el.child(this.sharePanel(t, p)))
      .when(p.mine, (el) => el.child(h_flex().gap(6).items_center()
        .child(div().flex_1().text_size(10.5).text_color(t.dim).child(p.running ? `running ${p.running}` : ""))
        .when(p.ports.some((x) => x.proto === "TCP"), (el) => el.child(this.button(t, `share-${p.pid}`, "Share", {
          bg: this.share?.pid === p.pid ? t.selected : t.raised,
          onClick: (_e, cx) => { this.share = this.share?.pid === p.pid ? null : { pid: p.pid, port: p.ports.find((x) => x.proto === "TCP").port }; cx.notify(); } })))
        .child(this.button(t, `stop-${p.pid}`, "Stop", { onClick: (_e, cx) => { this.pending = { sig: "TERM", pids: [p.pid] }; cx.notify(); } }))
        .child(this.button(t, `force-${p.pid}`, "Force kill", { fg: t.ansi1, border: t.ansi1, bg: t.surface, onClick: (_e, cx) => { this.pending = { sig: "KILL", pids: [p.pid] }; cx.notify(); } }))));
  }

  bar(t, rows) {
    const names = (pids) => this.data.processes.filter((p) => pids.includes(p.pid)).map((p) => `${p.name} :${p.ports[0].port}`);
    if (this.pending) {
      const { sig, pids } = this.pending;
      const list = names(pids);
      const sys = this.data.processes.some((p) => pids.includes(p.pid) && isSystem(p));
      return v_flex().gap(8).p(12).flex_none().border_t(1).border_color(t.border).bg(t.raised)
        .child(div().text_size(12.5).font_semibold().text_color(sig === "KILL" ? t.ansi1 : t.foreground)
          .child(`${sig === "KILL" ? "Force kill" : "Stop"} ${pids.length} process${pids.length > 1 ? "es" : ""}?`))
        .child(div().text_size(11).text_color(t.muted).child(list.slice(0, 4).join(" · ") + (list.length > 4 ? ` · +${list.length - 4} more` : "")))
        .when(sig === "KILL", (el) => el.child(div().text_size(10.5).text_color(t.dim).child("Force kill cannot be caught: the process loses unsaved work.")))
        .when(sys, (el) => el.child(div().text_size(10.5).text_color(t.ansi3).child("Includes a macOS system process.")))
        .child(h_flex().gap(8).justify_end()
          .child(this.button(t, "cancel", "Cancel", { onClick: (_e, cx) => { this.pending = null; cx.notify(); } }))
          .child(this.button(t, "confirm", sig === "KILL" ? "Force kill" : "Stop", {
            bg: sig === "KILL" ? t.ansi1 : t.solid, fg: sig === "KILL" ? t.background : t.solid_foreground, border: sig === "KILL" ? t.ansi1 : t.solid,
            onClick: (_e, cx) => this.run(sig, pids, cx) })));
    }
    const n = this.selected.size;
    const mine = rows.filter((p) => p.mine).map((p) => p.pid);
    return h_flex().gap(8).h(44).px(12).flex_none().items_center().border_t(1).border_color(t.border)
      .child(div().flex_1().text_size(11.5).text_color(n ? t.foreground : t.dim).truncate()
        .child(this.note ? this.note.text : n ? `${n} selected` : "Select processes to stop several at once"))
      .child(this.button(t, "all", this.selected.size === mine.length && n ? "None" : "Select all", {
        onClick: (_e, cx) => { this.selected = this.selected.size === mine.length ? new Set() : new Set(mine); this.note = null; cx.notify(); } }))
      .when(n > 0, (el) => el
        .child(this.button(t, "stop-sel", "Stop", { onClick: (_e, cx) => { this.pending = { sig: "TERM", pids: [...this.selected] }; this.note = null; cx.notify(); } }))
        .child(this.button(t, "kill-sel", "Force kill", { fg: t.ansi1, border: t.ansi1, bg: t.surface, onClick: (_e, cx) => { this.pending = { sig: "KILL", pids: [...this.selected] }; this.note = null; cx.notify(); } })));
  }

  render() {
    const t = theme();
    const rows = this.visible();
    const refresh = this.button(t, "refresh", this.loading ? "…" : "Refresh", { onClick: (_e, cx) => this.load(cx) });
    return v_flex().id("ports-root").size_full().bg(t.surface)
      .child(h_flex().h(42).px(14).flex_none().items_center().gap(8).border_b(1).border_color(t.border)
        .child(div().text_size(13).font_semibold().text_color(t.foreground).child("Ports"))
        .child(div().flex_1().text_size(11).text_color(t.dim).child(this.data ? `${rows.length} ${this.query.trim() ? "shown" : "listening"}` : "Loading…"))
        .child(refresh))
      .child(h_flex().px(12).pt(10).flex_none()
        .child(Input.new(this.input).h(30).flex_1().min_w(0).px(10).rounded(8).border_1().border_color(t.input).bg(t.background).text_size(12).text_color(t.foreground)))
      .child(h_flex().gap(6).px(14).py(8).flex_none().items_center()
        .child(this.toggle(t, "t-udp", "UDP", this.showUdp, () => { this.showUdp = !this.showUdp; }))
        .child(this.toggle(t, "t-sys", "System", this.showSystem, () => { this.showSystem = !this.showSystem; }))
        .child(div().flex_1())
        .child(div().text_size(10.5).text_color(t.dim).child("Updates every 10s")))
      .child(v_flex().id("ports-list").flex_1().min_h(0).overflow_y_scrollbar().gap(8).px(12).pb(12)
        .when(Boolean(this.error), (el) => el.child(div().p(10).rounded(8).border(1).border_color(t.ansi1).text_size(11.5).text_color(t.ansi1).child(`Couldn't read ports: ${this.error}`)))
        .children(this.orphans().map((tn) => v_flex().gap(4)
          .child(div().text_size(10.5).text_color(t.ansi3).child(`Still sharing :${tn.port}, but nothing is listening on it any more`))
          .child(this.link(t, tn))))
        .children(rows.map((p) => this.row(t, p)))
        .when(this.data && !rows.length, (el) => el.child(div().p(16).text_size(12).text_color(t.dim).child(this.query.trim() ? `No match for "${this.query.trim()}".` : "Nothing is listening."))))
      .child(this.bar(t, rows));
  }
}
