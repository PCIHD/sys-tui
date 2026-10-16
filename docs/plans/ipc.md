# IPC: connection map (phase 1) + systui agent (phase 2)

> **Status (2026-10-16):**
> - **Phase 1 is done.** The `8 IPC` tab ships with this file, and its code is in `telemetry/ipc.py`, `telemetry/sockdiag.py`, `render/ipc.py` and `tests/test_ipc.py`.
>   - Changes from this plan: rates are `in_bps`/`out_bps`, not `rx`/`tx`. Same-name processes fold into one `group:<name>` row. Arcs are capped unless a node is selected.
>   - Not yet checked: `./systui-root --tab ipc`, and VM/nspawn detection on a host that has them.
> - **Phase 2 is not started.** Pick it up from the "Phase 2" section below.

## Context

systui shows this machine's hardware: board, CPU, memory, storage, network, processes and sensors. It doesn't show **who talks to whom**. That covers pipes and Unix sockets between processes, traffic between the host and its podman containers, other systems running on this machine (VMs, nspawn machines, distrobox/toolbox), and machines on the LAN.

The user chose **both**:
1. A connection map observed from this machine, built now.
2. A systui-to-systui agent, designed now and built next. It lets one systui pull telemetry from VMs and other LAN hosts. It also names the remote end of a connection that both sides can see.

**Scope:** this session implements Phase 1 only. Phase 2 is a design to execute after Phase 1 is reviewed, so approving this plan does not approve building a LAN-listening agent now.

Facts from probing this machine (Bazzite 44, rootless podman 5 with pasta, no libvirt, machined empty):
- Rootless containers aren't visible as host interfaces. Their netns is readable without root through `/proc/<ctr-pid>/net/{dev,tcp,tcp6}`, and `/proc/<pid>/cgroup` contains `libpod-<id>.scope`.
- `ss -x`/`ss -ti` work unprivileged. That means sock_diag gives Unix peers and TCP `tcp_info` byte counters without root.
- `/run/user/1000/podman/podman.sock` is active and serves the docker-compatible API. `/run/podman` (rootful) needs root.
- About 1000 Unix sockets and about 555 PIDs. IPC collection must be lazy and throttled.

**Defaults (taken from repo conventions, not asked):**
- Pixel-art panel + DataTable + detail panel, like the Board and Processes tabs.
- No new dependencies. Netlink is done in pure Python, and HTTP uses stdlib `http.client`/`http.server`.
- Unprivileged by default. `./systui-root` also sees other users' fds and rootful containers.
- Every value gets a ● live / ◐ derived / ○ spec tag.
- Traffic uses the ▲ cyan (towards this host) and ▼ orange (away) convention.

## Step 0 — worktree

`EnterWorktree` (name `ipc`), then `uv sync` inside it. `systui-root` needs `$ROOT/.venv`.

---

## Phase 1 — connection map (tab `8 IPC`)

### Data model: `src/systui/telemetry/__init__.py`
Add these next to the other dataclasses, following the same style:
- `IpcNode`: `id` (`host`, `proc:<pid>`, `ctr:<id12>`, `vm:<name>`, `peer:<ip>`, `wan`), `kind` (host|process|container|vm|peer|wan), `name`, `sub` (image / runtime / MAC…), `addrs`, `pids`, `netns`, `ports`, `rx_bps`, `tx_bps`, `state`, `prov` (live|derived).
- `IpcLink`: `a`, `b` (node ids), `kind` (pipe|unix|tcp|udp|listen), `a_end`/`b_end` (path or `ip:port`), `pid_a`/`pid_b`, `state`, `rx_bps`/`tx_bps` (TCP only, from `a`'s side), `prov`.
- `IpcStat`: `nodes`, `links`, `counts` per kind, `note` (for example "other users' sockets need root"), `age_s`.
- `SystemSnapshot.ipc: IpcStat | None = None`. In `to_dict`, cap `ipc.links` at 100, sorted by bytes. Otherwise `--json` balloons.

### Collectors
New `src/systui/telemetry/sockdiag.py` holds pure-Python `NETLINK_SOCK_DIAG`. Parsing is split from I/O so tests can feed it canned bytes.
- `unix()` → `(ino, peer_ino, type, state, path)` via `UDIAG_SHOW_NAME|UDIAG_SHOW_PEER`.
- `inet(family, proto)` → `(ino, src, dst, state, uid, bytes_acked, bytes_received)` via `INET_DIAG_INFO`. In `struct tcp_info`, `bytes_acked` is at offset 120 and `bytes_received` at 128; check against `linux/tcp.h`.
- Fallbacks: `/proc/net/unix` (paths, no peers), and `psutil.net_connections` or `/proc/net/tcp` (no bytes). Set `note` accordingly.

New `src/systui/telemetry/ipc.py` defines `IpcTracker(fs, diag=sockdiag)` with `collect(rates, now) -> IpcStat`.
- The netlink functions are injected so fake-`/proc` tests never mix in the real host's sockets.
- File reads go through `SysFS`, so tests can use fake trees as in `tests/test_telemetry.py`.
- The fd walk uses `os.scandir(fs.p("proc"))` and `os.readlink` directly. `SysFS` has no readlink, and the natural sort in `SysFS.glob` over thousands of fd paths would break the time budget.

Steps:
1. **fd inventory.** Readlink `socket:[ino]` / `pipe:[ino]` under `/proc/<pid>/fd` to build an inode→pids map. For pipes, read `fdinfo` `flags` (octal, `& 3`) so a link has a direction (writer → reader). Count unreadable PIDs for `note`.
2. **Pipes and Unix sockets.**
   - Drop self-links, where one pid holds every end. These are mostly event-loop wakeup pipes.
   - Each remaining pipe inode links its pid set. Pipes with the same pid set are aggregated into one edge with a count.
   - Each Unix pair (ino ↔ peer_ino) becomes a process↔process link. If an end has no readable pid, it goes to `(other user)` on the host node.
3. **TCP/UDP (host netns).** Use inet_diag. Per-socket rates come from the existing `Rates`, with keys `ipc:<ino>:rx|tx`, and dead keys are pruned like `ProcTracker` does. Classify the remote end:
   - loopback → match the twin socket → process↔process
   - a published port → container. Matching uses the API's published-port list, because rootless ports go through `rootlessport` and this machine runs pasta with `-t none`.
   - in a container subnet → container. This only fires for rootful bridges; rootless host sockets never see `10.89.x` remotes.
   - inside a local subnet (`psutil.net_if_addrs` netmask) → peer
   - otherwise → `wan`
4. **Containers.** Find them every IPC poll from the cgroup scan done in step 1: `libpod-<id>.scope` and `docker-<id>.scope`. Fetch metadata from the docker-compatible REST API over a Unix socket: `GET /containers/json`, with stdlib `http.client` subclassed for AF_UNIX and a 0.5 s timeout. Do this **only when the set of container IDs changes, or every 60 s**, so podman's socket activation doesn't keep restarting the service. The metadata gives name, image, state, networks/IPs, ports, and labels for distrobox/toolbox.
   - Sockets tried in order: `/run/user/$UID/podman/podman.sock` (use `$SUDO_UID` under `systui-root`), then `/run/podman/podman.sock`, then `/var/run/docker.sock`.
   - Fallback for names is `~/.local/share/containers/storage/overlay-containers/containers.json`, else the short id.
   - Traffic: sum non-`lo` interfaces in `/proc/<ctr-pid>/net/dev` → rates (●). Dedupe pods by netns inode.
   - Container↔container links: match addr pairs from `/proc/<pid>/net/tcp{,6}` across netns (●, no bytes).
   - Host→container in rootless mode (`rootlessport`): a host socket to `127.0.0.1:<published port>` gives a host proc → container link (◐).
   - Host-network containers (distrobox/toolbox) share host sockets, so their PIDs, mapped through cgroups, link up automatically.
5. **VMs and machines.**
   - Read `/run/systemd/machines/*` for name, CLASS and LEADER. This covers nspawn and libvirt.
   - Also look for `qemu-system-*`/`qemu-kvm` processes, taking the name from `-name guest=`.
   - Map tap/vnet to its VM through the tun fd's `fdinfo` `iff:` line. Rates come from that interface's counters in `/sys/class/net`.
6. **LAN peers.**
   - Read `/proc/net/arp`, keeping only complete entries (flag 0x2).
   - Mark the router from the default route in `/proc/net/route`.
   - Add any TCP remote inside a local subnet.
   - Names come from reverse DNS on a background thread pool with a cache, marked ◐. Never block the poll thread on it.
7. **Throttling.**
   - inet_diag and netdev run every IPC poll; they're cheap and keep rates smooth.
   - The fd walk, Unix diag, ARP and machined run every 3rd poll. In between, reuse their output, the same way `HwmonReader` reuses slow chips (`telemetry/sensors.py:40`). The first poll after the tab becomes active is always a full walk.
   - Target: under 30 ms per IPC poll on this machine. Check it with a timer in a test-like script.

### Source wiring
- `LinuxSource.__init__` (`telemetry/__init__.py:427`): add `self.ipc = IpcTracker(self.fs)` and `self.want_ipc = False`. In `collect()`: `if self.want_ipc: snap.ipc = self.ipc.collect(rates, now)`.
- The app sets `source.want_ipc` on tab activation, and in `SysTuiApp.__init__` sets it to `initial_tab == "ipc"`, so `--tab ipc` doesn't open on "waiting". The CLI sets it for `--json` and for `--view ipc`.
- `LinuxSource.close()` also shuts down the rDNS thread pool.
- `telemetry/demo.py`: add a synthetic `IpcStat`, so the tab, tests and PNGs work everywhere:
  - host processes: firefox, python3, pipewire, gnome-shell, code
  - containers: `llm-litellm-db-1` postgres and `llm-llamacpp-1`
  - a distrobox `fedora-dev`
  - a QEMU VM `win11` on `vnet0`
  - peers: router 192.168.1.1, `nas` .20, `laptop` .34
  - `wan`
  - links with waveform rates

### Rendering: new `src/systui/render/ipc.py`
`IpcMap(Panel)` is `animated = True` and records `self.boxes[node_id]` while drawing, the same way `BoardScene.boxes` does.
- **Layout** is a stable deterministic column order, sorted by id rather than traffic so nodes don't jump.
  - A **"this machine"** frame on the left holds a host box. The host box has a grid of top-N process chips, chosen by bytes then link count with hysteresis, plus a `+k more` chip.
  - Next to the host box, a column of container and VM boxes. Each shows its badge (podman / distrobox / toolbox / qemu / nspawn), name, and ▲▼ rate.
  - Outside the frame: a LAN peers column, with the router on top linking out to `wan` at the right edge.
- **Links** are aggregated per node pair.
  - TCP and netdev links are two-lane traces, cyan ▲ for inbound and orange ▼ for outbound. Particle speed follows bytes, reusing the board's trace and particle look (`render/board.py`).
  - Pipe and Unix links (no counters) are dim dotted lines inside the host box.
- A legend line and `note` go in the footer, and `_waiting(cv)` shows until the first IPC sample.
- Reuse `pixboard.canvas` (`line_points`, `polyline_points`, `mix`, `ramp`) and `pixboard.charts` (`IN`, `OUT`, `human_bytes`, `human_bits`, `area`, `text_right`).

### App: `src/systui/app.py`
- `TABS` + ("ipc", "IPC") → key `8`. Add `SYS_TAB_LABELS["ipc"] = "8 IPC"`, and the same label in `GPU_TAB_LABELS`.
- GPU mode: `on_tabbed_content_tab_activated` must handle `ipc`, or the key is silently ignored. Set `self._pending_tab = "ipc"` and call `action_unzoom()`. In `on_board_view_transitioned`, apply `_pending_tab` **after** `_set_chrome`, because `_set_chrome` forces `active="board"`.
- Pane layout: `Horizontal`.
  - Left (1fr): an `IpcView` PanelView with the `IpcMap`, and below it a `DataTable#ipc-links`. Columns: Kind, From, To, State, ▲ in, ▼ out, tag.
  - Right: `#ipc-side`, width 58, sharing `#side`'s CSS rule. It holds a `Static#ipc-detail` inside `VerticalScroll`. The id must not be `#side`, because `_apply` sizes the GPU panel from `query_one("#side")` and would pick up the hidden IPC column.
- New `IpcView(PanelView)` in `widgets.py`. `on_click` maps the click to a `panel.boxes` entry and posts `IpcView.Selected(node_id)`. Reuse `BoardView._cid_at`'s border and offset handling (`widgets.py:240`) for the mapping.
- Selecting a node filters the table to that node's links and fills the detail panel with tagged rows, styled like `DetailPanel` (`widgets.py:319`, `PROV_STYLE`):
  - container: image, state, networks/IPs, ports, PIDs, netns, rates
  - VM: class, leader, tap, rates
  - peer: IP, MAC, rDNS, router, connections
  - process: pid, user, cmd, socket and pipe counts
- Selecting a table row highlights both endpoints on the map.
- `_apply`: when `active == "ipc"`, refresh the table and detail, keeping the cursor position the way `_refresh_procs` does. Header hint: `8 IPC · click a node`.

### CLI, history and README
- `cli.py`: add `ipc` to `--tab` choices and `VIEWS`, and a `"ipc": IpcMap()` entry in `render_view`. Set `want_ipc` for `--json`, `--png` and `--once` when relevant.
- `history.py`: when `snap.ipc` is present, push `ipc:<node>:rx|tx` for container, vm, peer and wan nodes only, so the detail panel can show a sparkline. `proc:<pid>` nodes are skipped because `History` never prunes keys, and process churn would leak memory.
- `README.md`: add a Keys row for `8`, a Data sources row for IPC, and Known limits:
  - no byte counters for Unix sockets or pipes
  - other users' fds need root
  - rootless container↔host mapping through published ports is ◐
  - IPv6 neighbours aren't listed (`/proc/net/arp` is IPv4)

### Tests
- New `tests/test_ipc.py` with fake trees under `tmp_path`, built with symlinks to `pipe:[123]` / `socket:[456]`:
  - pipe pairing and direction from fdinfo
  - Unix peer join
  - `libpod-` cgroup → container
  - netdev rates through `Rates`
  - ARP drops incomplete entries
  - machined file parse
  - remote classification (loopback / container subnet / LAN / wan)
  - sock_diag parsers on canned `struct.pack` messages
  - container API JSON → nodes from a canned payload
  - `to_dict` cap
- `tests/test_render.py`: `IpcMap` with demo data at 3 sizes. No errors, every node in `boxes`, and no two boxes overlap.
- `tests/test_app.py`: extend the tab loop with `("8", "ipc")`. Assert that `#ipc-links` has rows, that clicking a container box filters the table, and that pressing `8` in GPU mode lands on the IPC tab.

---

## Phase 2 — systui agent (after phase 1 is merged)

- **Snapshot wire format.** New `telemetry/wire.py` with `snapshot_from_dict(d)`. It rebuilds nested dataclasses through `dataclasses.fields` and `typing.get_type_hints`, ignores unknown keys, and is tagged with `schema: 1`. JSONL replay of saved recordings comes for free.
- **Agent.** `systui --serve [unix://PATH | tcp://HOST:PORT | vsock://CID:PORT]` runs headless with `LinuxSource(want_ipc=True)` on `ThreadingHTTPServer`, and is **read-only**.
  - Endpoints: `GET /v1/hello` (version, schema, hostname, board, caps) and `GET /v1/snapshot` (full `to_dict`). There's also `GET /v1/stream` as chunked NDJSON at the poll interval.
- **Exposure rules.**
  - The default bind is `unix://$XDG_RUNTIME_DIR/systui.sock` (mode 0600).
  - TCP needs an explicit address **and** a bearer token. The token is generated into `~/.config/systui/token` (0600) and printed once.
  - Optional `--tls-cert/--tls-key` uses stdlib `ssl`, and the client pins the fingerprint on first use. The README recommends `ssh -L` as the simplest secure LAN path.
  - Over TCP, command lines are redacted by default (`--no-redact` to keep them).
- **VMs.** Run the agent in the guest. The host connects over vsock (no guest network setup) or the VM's NAT IP. For containers the agent is optional, since the host already sees them from phase 1.
- **Discovery.** The agent sends a UDP beacon (hostname, port, TLS fingerprint) every 5 s, and only on TCP binds. The client listens, and phase-1 peer nodes heard from get a `systui` badge.
- **Client.**
  - `RemoteSource(url, token)` implements `collect()` and `close()`, like `LinuxSource`.
  - `systui --connect HOST[:PORT]` starts on a remote host directly.
  - Inside the app, `m` opens a host switcher modal listing local, discovered and `~/.config/systui/hosts.toml` hosts. Switching swaps `self.source`, runs `identify()` again, resets `History`, and calls `await self.recompose()`, because the Storage and Network panes are built per snapshot.
  - Pressing Enter on a peer node that has an agent switches to that host.
- **Cross-host join.** If both ends report the same TCP 4-tuple, the remote's process name and pid label the link (●).
- **Tests.**
  - `snapshot_from_dict(to_dict(demo))` round-trips.
  - The agent on a temp Unix socket serves `/v1/hello` and `/v1/snapshot`.
  - TCP without a token gives 401.
  - `RemoteSource` drives the app in `run_test`.

## Verification
1. `uv run pytest`: the whole suite passes, including the new IPC tests.
2. `uv run systui --demo --tab ipc`: the map, table and detail all work, and node click and row click both work. Also `uv run systui --demo --png ipc.png --view ipc --size 170x50` and look at the PNG.
3. Live, unprivileged: `uv run systui --tab ipc`. Expect `llm-litellm-db-1` with its 10.89.0.x links and netns ▲▼ rates, the router and LAN peers from ARP, `claude`/`node` TCP to `wan`, and pipewire Unix links. The note should say other users' fds are hidden.
4. `./systui-root --tab ipc`: root-owned daemons now show sockets, and the rootless containers are still found through `$SUDO_UID`.
5. Timing: IPC collect stays under about 30 ms per poll. Time `IpcTracker.collect` in a scratch script. The UI stays smooth at a 0.25 s interval.
6. `uv run systui --json | jq '.ipc.links | length'` returns 100 or fewer.
7. Phase 2: run `systui --serve` in a distrobox and `systui --connect unix://…`. Then do TCP with a token between two LAN machines and check that a missing token is refused.
