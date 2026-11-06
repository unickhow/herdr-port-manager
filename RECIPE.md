# Port Manager

A right-side panel that lists every port this computer is listening on, who holds it, and lets
you stop one process or several at once. It can also share a port with other devices through a
tunnel, with one click.

## What you see

- Header: "Port Manager", a count ("10 listening"), a Refresh button.
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
  while the panel is hidden; refreshes never overlap (one asked for meanwhile runs right after).
- Selecting a card, Select all and every Stop / Force kill button pin each process as shown:
  `{pid, port, start}` (the first port on its card, START its start time in epoch seconds; `pin.js`).
  The confirmation is built from the pins and confirm sends exactly them, as `PID:PORT:START`. Each
  refresh drops every pin whose pid now has another start time, is gone, or no longer holds the
  pinned port, from the selection and from an open confirmation (cancelled when nothing is left), and
  the bar says "N processes exited or changed and were deselected". A process whose start time could
  not be read cannot be selected.
- **Stop** sends SIGTERM (the process can clean up). **Force kill** sends SIGKILL. Every stop asks
  first, inline in the bottom bar, naming the processes; Force kill adds a warning that unsaved work
  is lost, a macOS system process another. Other users' processes show "other user" and have no
  checkbox or buttons.
- Immediately before signalling each target, `port-manager.py kill` re-checks with `ps` and a
  targeted `lsof` that the pid still has that start time, is still yours and still holds that port;
  otherwise it refuses ("process changed since it was listed", "no longer holds this port").
- Share pins the card's process (pid and start time; the port picker only changes the port). Start
  sharing, and **Try again** in a "Stopped sharing" notice (shown only when one of your processes
  listens on that TCP port, which it then pins), send `tunnel start PROVIDER PORT OWNER_PID
  OWNER_START`. It is refused when nothing listens there, when that pid no longer listens there or
  has another start time, and when the port is still shared by a tunnel tied to a previous listener
  (that one stops on its own after `GRACE`). If a refresh shows the pinned process changed, the share
  section closes and the bar says so.
- A tunnel runs detached under a small supervisor (`port-manager.py _run`), so it survives closing
  the panel or Herdr. The supervisor records the listener (pid and start time), checks every 3 s that
  it still holds the port, and once it has been gone for 15 s (`GRACE`) stops the tunnel and records
  "service exited". It caps the log, and stops and reaps the tool on every way out, including errors.
  It answers the starting process with one line, `ok` once its state is saved and the tool started,
  or the reason it gave up; a start that gets no `ok` within 15 s stops the supervisor's whole process
  group and forgets its state, so nothing runs unrecorded.
- `tunnel start` and `tunnel stop` hold one exclusive `flock` (`STATE/.lock`, never removed,
  close-on-exec so no supervisor or tool inherits it) for their whole check, spawn / signal and wait.
  Another start or stop waits up to 10 s, then answers "another start or stop of a share is in
  progress". Listing (`list`, `tunnel status`) never waits: it forgets a finished tunnel only if it
  gets the lock at once and the state, re-read, is still gone.
- Each supervisor, before it reads or writes anything, takes a non-blocking `flock` on
  `STATE/PROVIDER-PORT.run` (`0600`, close-on-exec, never removed: one small file per provider and
  port) and holds it for its whole life; the kernel drops it however the supervisor ends. If another
  supervisor of that share holds it, it answers `busy` and exits having started nothing, and the
  start reports "another start of this share is still in progress". So even when a starting process
  dies before its supervisor has saved its state, a later start cannot add a second supervisor. It
  also refuses if the state already names another live process (an older version's tunnel). A
  supervisor never takes `STATE/.lock` and a start never waits on a `.run` lock, only (at most
  15 s) for the supervisor's answer. A start that gives up stops only the supervisor it spawned.
- `tunnel status` and `list` give each share an `id`: its supervisor's pid and start time
  (`PID-EPOCH`; `PID` for a first-version state). The panel's Stop sharing and Dismiss send the `id`
  of the share as it was shown, and `tunnel stop` then refuses ("this share was restarted since it
  was shown") without signalling or deleting anything if the share recorded now is another run; the
  panel shows that and reloads. Without an `id` it stops whatever is recorded, as before.
- Stopping checks with `ps`, immediately before each signal, that the pid is still the tunnel: a
  current state's pid must be `python … port-manager.py _run PROVIDER PORT` with the recorded start
  time. It sends SIGTERM to its process group, waits up to 8 s, then SIGKILL, and forgets the tunnel
  only once it is gone; otherwise the bottom bar says why and it stays listed.
- Notices at the top of the list: "Stopped sharing" (Try again, Dismiss); a share whose process has
  exited but which is still up during the grace period ("Still sharing", with its link); a share
  whose state cannot be told, or an older version's share without a known link (Stop sharing); a
  newer version's share (the message only, "This version cannot stop it.").
- While sharing starts, the panel polls `tunnel status` (state files only, no port scan) once a
  second; polls and refreshes never overlap. Start, Stop sharing and Sign in ignore repeated clicks
  while one is in flight, and only one kill runs at a time.

### State format and compatibility

Each share is `~/.cache/herdr-port-manager/tunnels/PROVIDER-PORT.json`: `v` (schema version, now 2),
`provider`, `port`, `pid` (the supervisor), `pid_start` (its `ps` lstart), `started`, `log`,
`owner_pid`, `owner_start`, and once known `url`, then `ended`. The mod is installed on machines
that may still run tunnels from older versions, so:

- Readers ignore fields they do not know.
- Newer versions keep recognising older states: without `pid_start` (the first version), `pid` is
  the tool itself (`cloudflared tunnel … --url http://localhost:PORT`, `devtunnel host -p PORT`) or
  an earlier supervisor (`… port-manager.py _run PROVIDER PORT …`). Positively one of those for the
  port: listed and stoppable (it does not stop on its own). The pid now another program: forgotten.
  The provider's executable with other arguments, or `ps` unreadable: kept and shown with the reason
  and a Stop sharing button, which refuses rather than signal something that is not clearly the share.
- A state whose process may still be alive is never deleted. A state with a newer `v` is never
  stopped, deleted or overwritten by an older version, nor is anything signalled because of it:
  listing shows it at the top with "this share was made by a newer version of Port Manager; update
  the mod folder to manage it" and no Stop button; `tunnel stop`, `tunnel start` and a supervisor
  of that share refuse with the same reason.

## Built from

- `mods/port-manager/main.js`: the panel (`gpui-kit`, `gpui-base`, `herdr`: `exec`, `appHome`, `theme`).
- `mods/port-manager/port-manager.py`: stdlib Python, run through `exec("python3", …)`.
  - `list`: JSON from `lsof` and `ps`, plus which sharing tools exist and the running tunnels.
  - `kill TERM|KILL PID:PORT:START …`: signal processes (START as listed, epoch seconds).
  - `tunnel start cloudflared|devtunnel PORT [OWNER_PID OWNER_START]`, `tunnel stop cloudflared|devtunnel
    PORT [ID]`, `tunnel status`, `tunnel login devtunnel`. Anything else is refused as invalid (a 16-hex
    token after `tunnel stop`, sent by an earlier panel, is accepted and treated as no ID).
  - `_run PROVIDER PORT OWNER_PID OWNER_START`: internal, the supervisor a tunnel runs under.
- `mods/port-manager/pin.js`: pure helpers the panel imports (pinning and reconciling picked processes).
- `mods/port-manager/qr.py`: a small QR encoder (stdlib, byte mode, error correction M, up to about 210 characters).
- `mods/port-manager/mod.json`: `{ "place": "right", "size": 380 }`.
- `mods/port-manager/port-manager.svg`: an icon, only for a toolbar button (see below).

## Install notes for Herdr's agent

1. Copy `mods/port-manager/` into `~/.config/herdr-app/mods/port-manager/`. Herdr initially docks it
   on the right according to `mod.json`. Inspect the active layout and its toolbar before finishing:
   if it already has a shared side panel for Files, Changes or Usage, put a `port-manager` block
   inside that panel's stack and remove the extra automatically docked block. Follow the existing
   toolbar's panel-button logic: select this stack child and show the panel, or hide the panel
   when its active button is clicked again. Include `port-manager` in any list used to restore
   the panel per space. Use a personal layout and toolbar clone when the originals are shipped.
   In a layout without a shared panel, keep the right-side block and provide a toggle targeting
   its actual block ID. For a pane-based layout, open it with
   `herdr-app do pane.split --direction right --mod port-manager`.
2. Requires `python3` (the macOS one is fine). Nothing else is needed for the panel itself.
3. **Do not install the sharing tools for the user.** Tell them which of `cloudflared` and `devtunnel`
   are missing and how to install them (`brew install cloudflared`, `brew install --cask devtunnel`),
   and install only if they ask. The mod never installs anything.
4. For a toolbar button, use `mods/port-manager/port-manager.svg` and the label "Port Manager".
   When replacing an older install, update any old button's mod name, icon path and target;
   do not leave a button that still targets a removed block. Verify opening, closing and switching
   between the existing panels, and run `herdr-app check` on every changed mod.
5. Remove with: delete `~/.config/herdr-app/mods/port-manager/`, stop any shares (**Stop sharing**), and
   optionally delete `~/.cache/herdr-port-manager/`.
6. **Updating is a replace, not an add.** Herdr never updates an added mod. When the user already has
   `port-manager`, replace the whole `mods/port-manager/` folder with the new files (a copy beside
   it would be a second mod). Never mix files from two versions: `VERSION` in `port-manager.py` and
   `VERSION` in `main.js` must be equal, and the panel shows a "Mixed versions" warning when they are
   not. Bump both whenever the panel and the script's CLI must change together (a changed argument,
   a changed output the panel reads). Shares started by an older version keep running; see
   "State format and compatibility".

## Sharing details

- Cloudflare runs `cloudflared tunnel --no-autoupdate --url http://localhost:PORT --http-host-header localhost:PORT`.
  Dev Tunnels runs `devtunnel host -p PORT`.
- The link is read from the tool's output and saved by the supervisor, so it survives log trimming. Cloudflare's output
  format is confirmed against the real tool. The Dev Tunnels parsing follows its documented output
  and is not yet confirmed against a real run.
- Dev Tunnels needs a one-time sign-in. If starting fails because of it, the card shows the error and a
  **Sign in** button (`devtunnel user login`, opens the browser); then press Start again.
- Vite and some dev servers refuse unknown Host headers. The Cloudflare command rewrites the header to
  `localhost`, which avoids that. Dev Tunnels has no such option: add the `*.devtunnels.ms` host to
  the server's allowed hosts if it answers "Blocked request".
- Tunnel state, logs and lock files live in `~/.cache/herdr-port-manager/tunnels/` (folder `0700`, files `0600`).
  The supervisor caps each log at about 30 KB (first 10 KB verbatim, then the latest 20 KB),
  rewriting it every second when it changes, whether or not the panel is open. A failed or ended
  attempt nobody retries or dismisses is removed after an hour.

## Watch out

- Sharing sends traffic through Cloudflare's or Microsoft's servers. A Cloudflare quick tunnel is public
  to anyone with the link: do not share a dev server with logged-in sessions or admin pages.
- The panel shows full command lines, which can contain tokens passed as arguments. They stay on this
  computer (nothing is sent anywhere) but will be visible in screenshots.
- Identity checks compare start times with one-second resolution, and each check and its signal are
  two steps, so a tiny window between them remains. Tunnels from the first version (no supervisor)
  do not stop on their own and their logs are no longer trimmed: stop them and share again.
- `lsof` only shows your own processes' sockets without admin rights, so root daemons may be missing.
- Only listening TCP and bound UDP sockets are listed, not outgoing connections.

## Done when

- Start `python3 -m http.server 48765`: the panel shows port 48765 within 10 seconds (or press Refresh).
- Select it, press Stop, confirm: it disappears and the bar says "Stopped 1 process".
- Press Share: each tool shows "Installed" or "Not installed" correctly. With one installed, Start sharing
  shows a link and QR code within a few seconds, Open loads the page, and Stop sharing makes the link
  stop answering and removes the green box.
