# Ports

A right-side panel that lists every port this computer is listening on, who holds it, and lets
you stop one process or several at once. It can also share a port with other devices through a
tunnel, with one click.

## What you see

- Header: "Ports", a count ("10 listening"), a Refresh button.
- A search box. Type words to narrow the list; each word must match a port, name, pid, folder or
  command (`5173`, `:5173`, `my-app node`). The count then reads "N shown".
- Two filter chips: **UDP** (off by default) and **System** (off by default, hides macOS daemons).
- One card per process, sorted by lowest port:
  - port chips (a chip turns yellow when that port is open to the network, not just this machine),
  - process name and pid,
  - working folder (`~` shortened) and the full command line, truncated,
  - how long it has been running,
  - a checkbox, and **Share**, **Stop** and **Force kill** buttons.
- Bottom bar: "N selected", **Select all** / **None**, and Stop / Force kill for the selection.

### Share

**Share** (on every card with a TCP port) opens a section in the card, with a port picker when the
process has several ports, listing two ways to open the port from another device:

- **Cloudflare Tunnel**: a public `https://….trycloudflare.com` link, no account, a new URL on every
  start. Marked "Public: anyone with the link".
- **Dev Tunnels** (the service behind VS Code's port forwarding): private to the signed-in owner.

Each shows "Installed" or "Not installed". Installed: one **Start sharing** button. Not installed:
**Copy install command** (`brew install cloudflared`, `brew install --cask devtunnel`).

Start sharing shows "Starting…", then a green box on the card with the link, a QR code
(black on white in any theme; **Hide QR** / **QR** toggles it), **Copy**, **Open** (default
browser) and **Stop sharing**. Clicking the link opens it too.

## How it behaves

- Refreshes every 10 seconds and on Refresh (about 0.15 s of CPU each time). It keeps refreshing
  while the panel is hidden. Selections survive refreshes; a process that exits drops out of them.
- **Stop** sends SIGTERM (the process can clean up). **Force kill** sends SIGKILL.
- Every stop asks first, inline in the bottom bar, naming the processes. Force kill adds a warning
  that unsaved work is lost; a macOS system process adds another.
- Processes owned by another user show "other user" and have no checkbox or buttons.
- Before signalling, `ports.py kill` re-reads the port table and only acts on a pid that still holds
  the port it was shown with, so a recycled pid is never hit.
- Tunnels are started detached, so they keep running when the panel or Herdr is closed, until
  **Stop sharing**. If the shared port stops listening, the link stays visible in a "Still sharing"
  box at the top of the list so it can still be stopped.
- Stopping a tunnel only signals a pid that is still that tool (checked with `ps`), never a recycled one.

## Built from

- `mods/ports/main.js`: the panel (`gpui-kit`, `gpui-base`, `herdr`: `exec`, `appHome`, `theme`).
- `mods/ports/ports.py`: stdlib Python, run through `exec("python3", …)`.
  - `list`: JSON from `lsof` and `ps`, plus which sharing tools exist and the running tunnels.
  - `kill TERM|KILL PID:PORT …`: signal processes.
  - `tunnel start|stop cloudflared|devtunnel PORT`, `tunnel login devtunnel`.
- `mods/ports/qr.py`: a small QR encoder (stdlib, byte mode, error correction M, up to about 210 characters).
- `mods/ports/mod.json`: `{ "place": "right", "size": 380 }`.
- `mods/ports/ports.svg`: an icon, only for a toolbar button (see below).

## Install notes for Herdr's agent

1. Copy `mods/ports/` into `~/.config/herdr-app/mods/ports/`. Herdr docks it on the right by itself
   the first time it sees `mod.json`. In a layout that ignores `place`, open it with
   `herdr-app do pane.split --direction right --mod ports`.
2. Requires `python3` (the macOS one is fine). Nothing else is needed for the panel itself.
3. **Do not install the sharing tools for the user.** Tell them which of `cloudflared` and `devtunnel`
   are missing and how to install them (`brew install cloudflared`, `brew install --cask devtunnel`),
   and install only if they ask. The mod never installs anything.
4. Optional: if the user's layout has a top bar with panel buttons, offer to add a "Ports" button
   (it can use `ports.svg` and `layout.toggle` on the block that holds this mod). Ask first; it edits their layout.
5. Remove with: delete `~/.config/herdr-app/mods/ports/`, stop any shares (**Stop sharing**), and
   optionally delete `~/.cache/herdr-port-manager/`.

## Sharing details

- Cloudflare runs `cloudflared tunnel --no-autoupdate --url http://localhost:PORT --http-host-header localhost:PORT`.
  Dev Tunnels runs `devtunnel host -p PORT`.
- The link is read from the tool's output and saved, so it survives log trimming. Cloudflare's output
  format is confirmed against the real tool. The Dev Tunnels parsing follows its documented output
  and is not yet confirmed against a real run.
- Dev Tunnels needs a one-time sign-in. If starting fails because of it, the card shows the error and a
  **Sign in** button (`devtunnel user login`, opens the browser); then press Start again.
- Vite and some dev servers refuse unknown Host headers. The Cloudflare command rewrites the header to
  `localhost`, which avoids that. Dev Tunnels has no such option: add the `*.devtunnels.ms` host to
  the server's allowed hosts if it answers "Blocked request".
- Tunnel state and logs live in `~/.cache/herdr-port-manager/tunnels/` (folder `0700`, logs `0600`).
  Logs are emptied when they pass 256 KB. A failed attempt nobody retries is removed after an hour.

## Watch out

- Sharing sends traffic through Cloudflare's or Microsoft's servers. A Cloudflare quick tunnel is public
  to anyone with the link: do not share a dev server with logged-in sessions or admin pages.
- The panel shows full command lines, which can contain tokens passed as arguments. They stay on this
  computer (nothing is sent anywhere) but will be visible in screenshots.
- `lsof` only shows your own processes' sockets without admin rights, so root daemons may be missing.
- Only listening TCP and bound UDP sockets are listed, not outgoing connections.

## Done when

- Start `python3 -m http.server 48765`: the panel shows port 48765 within 10 seconds (or press Refresh).
- Select it, press Stop, confirm: it disappears and the bar says "Stopped 1 process".
- Press Share: each tool shows "Installed" or "Not installed" correctly. With one installed, Start sharing
  shows a link and QR code within a few seconds, Open loads the page, and Stop sharing makes the link
  stop answering and removes the green box.
