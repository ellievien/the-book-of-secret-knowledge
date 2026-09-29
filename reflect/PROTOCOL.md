# Reflect protocol (version 1)

The iPhone app talks to the desktop helper over one WebSocket on the local
network. There is no cloud relay and nothing is forwarded to the internet.

## Discovery

The helper advertises a Bonjour/mDNS service:

| Field | Value |
|---|---|
| Service type | `_reflect._tcp.local.` |
| Instance name | the computer's name, e.g. `Ellie's MacBook Pro` |
| Port | `47800` (or the next free port up to `47809`) |
| TXT `id` | helper id: random hex string, stable across restarts |
| TXT `name` | computer name |
| TXT `os` | `windows` or `macos` |
| TXT `v` | protocol version (`1`) |
| TXT `port` | the WebSocket port |
| TXT `ips` | comma-separated IPv4 addresses (fallback if resolution fails) |

The phone resolves the service and connects to `ws://<address>:<port>/`.

## Connection rules

* Only private, link-local and loopback source addresses are accepted
  (`10/8`, `172.16/12`, `192.168/16`, `169.254/16`, `fe80::/10`, ...).
* Requests that carry an `Origin` header (web browsers) are refused with 403.
* At most 4 simultaneous connections.
* A connection must authenticate or pair within 5 minutes, and is closed
  after 10 failed attempts.
* No permessage-deflate: frames are JPEG and do not compress.

## Messages

Text messages are JSON objects with a `type` field. Binary messages are video
frames. All text is UTF-8.

### Helper → phone

**`hello`**, sent immediately after the WebSocket opens:

```json
{"type":"hello","version":1,"id":"5f0c…","name":"DESKTOP-1234","os":"windows",
 "nonce":"<base64 of 32 random bytes>","pairing_open":true}
```

`pairing_open` tells the phone whether a pairing code is currently accepted.

**`pair`**, the answer to a pairing attempt:

```json
{"type":"pair","ok":true,"device_id":"9c1e2a4b5d6f7a8b","token":"<base64 of 32 bytes>"}
{"type":"pair","ok":false,"error":"bad_code","message":"That code is not right. …"}
```

`error` is one of `bad_code`, `locked` (too many wrong codes: wait 30 s; a
new code is shown on the computer), or `pairing_closed` (the user must choose
"Pair a new phone" in the tray menu).

**`auth`**, the answer to an authentication attempt:

```json
{"type":"auth","ok":true,"name":"iPhone"}
{"type":"auth","ok":false,"error":"unauthorized","message":"This phone is no longer paired …"}
```

**`status`**, sent after authentication and whenever something changes:

```json
{"type":"status","claude_running":true,"window_found":true,"streaming":true,
 "phone_mode":true,"phone_mode_active":true,"window":{"w":440,"h":956},
 "fps":15,"message":""}
```

| Field | Meaning |
|---|---|
| `claude_running` | `false` means the Claude desktop app is not running; `message` is then `"Claude desktop not running"` |
| `window_found` | a Claude window is being mirrored |
| `streaming` | `false` when the user chose Stop in the tray menu |
| `phone_mode` | the phone-mode preference |
| `phone_mode_active` | the window is currently resized to phone shape |
| `window` | mirrored window size in logical pixels (points on macOS), or `null` |
| `message` | human-readable problem, or `""` |

**`pong`** echoes a `ping`'s `t`. **`error`** reports a bad message:
`{"type":"error","code":"not_authenticated","message":"…"}`.

### Phone → helper

**`pair`**, the first time only:

```json
{"type":"pair","code":"123456","device":"iPhone"}
```

**`auth`**, on every later connection:

```json
{"type":"auth","device_id":"9c1e2a4b5d6f7a8b","proof":"<hex HMAC-SHA256(token, nonce)>"}
```

`proof` is the lowercase hex HMAC-SHA256 whose key is the 32 token bytes and
whose message is the 32 nonce bytes from this connection's `hello`. The token
itself is never sent again after pairing, and a proof cannot be replayed on
another connection.

Test vector: token = bytes `00…1f`, nonce = bytes `20…3f`, proof =
`62215de7bddcea7e2c4047ff6bb94f8d18262fc8b3f3648134bb7d44158ff84d`.

**`input`**, coordinates are fractions (0–1) of the mirrored image, so they
work at any zoom or screen size. The helper converts them to window pixels.

```json
{"type":"input","kind":"tap","x":0.52,"y":0.31}
{"type":"input","kind":"long_press","x":0.52,"y":0.31}
{"type":"input","kind":"scroll","x":0.5,"y":0.5,"dx":0,"dy":-0.02}
{"type":"input","kind":"type","text":"Hello Claude"}
{"type":"input","kind":"key","key":"enter"}
```

| kind | Desktop action |
|---|---|
| `tap` | left click |
| `long_press` | right click |
| `scroll` | mouse wheel at (x, y). `dx`/`dy` is how far the finger moved, as a fraction of the image; content follows the finger |
| `type` | types the text. A newline becomes Shift+Enter (a new line in Claude's message box, not Send). A tab becomes 4 spaces. At most 20,000 characters |
| `key` | a named key or combination: `enter`, `esc`, `backspace`, `delete`, `tab`, `space`, `up`, `down`, `left`, `right`, `home`, `end`, `page_up`, `page_down`, `f1`–`f12`, or a single character. Modifiers use `+`: `ctrl+v`, `cmd+v`, `shift+enter`. `mod` means Cmd on macOS and Ctrl on Windows |

Before injecting, the helper brings the Claude window to the front. Input
arriving while streaming is stopped is dropped.

**`settings`**, every field is optional:

```json
{"type":"settings","phone_mode":true,"viewport":{"w":402,"h":780},"fps":15,"quality":70}
```

`viewport` is the phone's mirror area in points. In phone mode the helper
makes the window 440 logical pixels wide with this aspect ratio, so the
picture fills the phone's width exactly (default aspect 440×956). If Claude
enforces a larger minimum width, the helper uses that width and keeps the
aspect as close as the screen height allows. `fps` is 1–15 and `quality` is
JPEG quality 30–90.

**`ping`**: `{"type":"ping","t":1727600000123.5}`, answered with `pong`
carrying the same `t`. The app sends one every 2 s and shows the round trip as
latency. Silence for 7 s counts as a lost connection.

### Binary frames

Each binary message is one frame:

| Bytes | Content |
|---|---|
| 0–3 | width in pixels, uint32 big-endian |
| 4–7 | height in pixels, uint32 big-endian |
| 8– | JPEG image (quality 70 by default) |

Frames are sent only after authentication, at most 15 per second, and only
when the picture changed (the helper compares a hash of each capture). A slow
link drops intermediate frames instead of queueing them, so the newest frame
always wins.

## Pairing flow

1. When no phone is paired yet, the helper prints a 6-digit code at start-up
   (console, tray menu and tray notification). Later, "Pair a new phone" in
   the tray menu (or `--pair`) opens a new code for 10 minutes.
2. The phone connects, receives `hello`, has no token for this helper `id`,
   and asks the user for the code.
3. `pair` with the code returns a random 32-byte token. The helper stores it
   in `%APPDATA%\Reflect\state.json` (Windows) or
   `~/Library/Application Support/Reflect/state.json` (macOS). The phone
   stores it in the iOS Keychain under the helper `id`.
4. Codes are single use. Five wrong guesses rotate the code and pause pairing
   for 30 seconds.
5. On every later connection the phone answers `hello` with `auth`.

## Example session

```
phone → connect ws://192.168.1.20:47800/
helper → {"type":"hello",…,"nonce":"…","pairing_open":false}
phone → {"type":"auth","device_id":"…","proof":"…"}
helper → {"type":"auth","ok":true,"name":"iPhone"}
helper → {"type":"status","claude_running":true,…}
phone → {"type":"settings","phone_mode":true,"viewport":{"w":402,"h":780}}
helper → <binary 440x956 JPEG> …
phone → {"type":"input","kind":"tap","x":0.5,"y":0.9}
```
