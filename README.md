# Herdr Ports

A side panel for the [Herdr](https://herdr.dev) app that lists every port your computer is
listening on, shows which process holds it (name, pid, folder, command line), and lets you stop one
process or several at once, politely (Stop) or not (Force kill). It can also share a port with your
phone or another computer, with one click and a QR code.

![Ports](preview.png)

## Install

In the Herdr app, ask its agent:

```
Read ~/.config/herdr-app/AGENTS.md, then add this mod: https://github.com/unickhow/herdr-port-manager
```

The agent copies `mods/ports/` into your Herdr folder and docks it on the right. Or clone the repo
and point the agent at the local folder:

```sh
git clone https://github.com/unickhow/herdr-port-manager.git
```

```
Read ~/.config/herdr-app/AGENTS.md, then add the mod in the folder you cloned it to
```

Requires `python3` (the macOS one is fine). Details for the agent are in [RECIPE.md](RECIPE.md).

To remove it, delete `~/.config/herdr-app/mods/ports/`, stop any shares first, and optionally delete
`~/.cache/herdr-port-manager/`.

## Using it

- Type in the search box to filter by port, name, pid or folder. **UDP** and **System** show the
  hidden ones.
- **Stop** asks the process to quit (SIGTERM). **Force kill** ends it immediately (SIGKILL). Tick
  several cards to stop them together. Everything asks first and names what will be stopped.
- A yellow port chip means that port is open to the network, not only this computer.
- The list updates every 10 seconds.

## Share a port with other devices

**Share** on a card gives two ways to open that port from a phone or another computer, like VS
Code's port forwarding:

| | Cloudflare Tunnel | Dev Tunnels |
|---|---|---|
| Who can open it | anyone with the link | only you, signed in |
| Account | none | GitHub or Microsoft |
| URL | new each time | can stay the same |
| Install | `brew install cloudflared` | `brew install --cask devtunnel` |

Both are **optional** and are never installed for you: if one is missing the panel shows
"Not installed" and copies the install command for you to run. Once installed it is one click:
**Start sharing**, wait a few seconds, and the link appears on the card with a QR code to scan, plus
**Copy**, **Open** and **Stop sharing**.

- Dev Tunnels needs a one-time sign-in; the card shows a **Sign in** button when it is needed.
- A tunnel keeps running when you close the panel or Herdr, until you press **Stop sharing**. If the
  shared dev server stops, the link stays listed at the top as "Still sharing" so you can stop it.
- Some dev servers (Vite) reject unknown host names. Cloudflare works because the Host header is
  rewritten to `localhost`. With Dev Tunnels, allow `*.devtunnels.ms` in the server's allowed hosts if
  you see "Blocked request".

## Privacy and safety

- The panel itself makes no network requests. It runs `lsof` and `ps` and reads nothing else.
- Sharing sends traffic through Cloudflare's or Microsoft's servers, and only starts when you press
  Start sharing. A Cloudflare link is public to anyone who has it: don't share a dev server with
  logged-in sessions or admin pages.
- Only your own processes can be stopped. Before signalling, it checks the pid still holds the port you
  were shown, so a reused pid is never hit. Tunnels are stopped the same careful way.
- Full command lines are shown; they can include tokens passed as arguments. They never leave your
  computer, but keep them out of screenshots.
- Tunnel logs and state are in `~/.cache/herdr-port-manager/` (private to your user, trimmed at 256 KB).

## Limitations

- macOS only (relies on macOS `lsof` and `ps`). Tested on Herdr 0.1.13.
- Other users' and root processes may not appear and cannot be stopped.
- Lists listening TCP and (optionally) bound UDP, not outgoing connections.
- The Dev Tunnels link detection follows its documented output but has not been confirmed against a
  real run; Cloudflare has.
- It refreshes every 10 seconds (about 0.15 s of CPU each) even while the panel is hidden.

## Files

```
RECIPE.md              what the mod looks like and how it works (the brief Herdr's agent builds from)
preview.png            screenshot
mods/ports/main.js     the panel
mods/ports/ports.py    lists ports, stops processes, runs tunnels (stdlib Python)
mods/ports/qr.py       QR code encoder for the share link (stdlib Python)
mods/ports/mod.json    where it docks
mods/ports/ports.svg   icon for an optional toolbar button
```

## License

[MIT](LICENSE)
