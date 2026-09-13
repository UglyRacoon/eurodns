# EuroDNS — Smart DNS for unblocking geo-restricted services

**EuroDNS** is a self-hosted **Smart DNS** + **transparent SNI proxy** that restores
access to geo-restricted services (Xbox Live, Gemini, ChatGPT, Claude, Spotify,
Twitch, Notion, JetBrains, Supercell games and more) — **without a VPN**, without
a client app, and **without slowing down** normal traffic.

It is a functional clone of the *xbox-dns.ru* concept, rebuilt with a modern,
fully-scripted stack you can deploy on **any** Linux server in a few minutes.

> 🔧 **The usual reason a Google service still says "not supported in your country".**
> It is almost always **coverage**, not your server's location. Google's
> availability/region check does **not** happen on `gemini.google.com` — the page makes a
> second request to `*.googleapis.com` (and assets on `gstatic.com` / `googleusercontent.com`,
> YouTube on `youtube.com`/`googlevideo.com`). If your Smart DNS only rewrites
> `google.com`, that region call hits Google **directly from the client's real (RU) IP** →
> blocked. EuroDNS maps the **whole Google family** (see [`domains.txt`](domains.txt)), so the
> region check also egresses from the EU box. `deploy.sh` + `build.sh` apply this automatically.
>
> > As a *secondary* check, confirm your server's **egress IP** itself is an allowed region:
> > open `https://gemini.google.com` **on the server** (or `whoer.net`) — if *that* loads
> > unblocked, your IP/provider is fine and any remaining block is purely coverage. (Cheap
> > reseller ranges can occasionally be geo-mislabelled, but this is rare and not the common cause.)

---

## How it works

```
 client (Xbox / phone / PC)                EuroDNS server (EU)               real service
        │  ① "what's gemini.google.com?" │                                  │
        ├──────────────DNS───────────────►│  dnsmasq answers = PROXY_IP ─────┤
        │  ② TLS connect, SNI=gemini…     │                                  │
        ├──────────:443 (ssl_preread)────►│  nginx stream forwards RAW TLS ─►│  (sees EU IP ✔)
        │                                  │  to the REAL gemini.google.com   │
```

1. **DNS rewrite.** `dnsmasq` returns the *proxy's own public IP* for every domain in
   [`domains.txt`](domains.txt); all other names are resolved normally (Cloudflare/Google
   upstreams).
2. **Transparent SNI proxy.** `nginx` in `stream` mode reads the TLS **SNI** via
   `ssl_preread` and forwards the raw TCP session to the *real* backend. Because the
   connection to the backend is opened **from the EU server**, the backend sees an
   EU source IP and serves the un-restricted content. **No MITM, no certificate
   pinning issues** — the client's own TLS verification still passes end-to-end.
3. **Encrypted DNS.** The same server offers **DNS-over-HTTPS** and **DNS-over-TLS**
   (a small Python backend + `stunnel`) so clients can hide their DNS from the ISP.

Everything normal to your traffic stays direct — only the geo domains are routed
through the proxy, so you keep full line speed.

---

## What's in the box

```
eurodns/
├─ deploy.sh                     # ⭐ one-shot installer for any Linux (Debian/Ubuntu) server
├─ uninstall.sh                  # cleanly reverses deploy.sh (services, configs, cert, dirs)
├─ build.sh                      # regenerates dnsmasq + nginx maps from domains.txt
├─ domains.txt                   # the list of geo-restricted domains to unblock
├─ backend/
│  └─ smartdns_backend.py        # DoH (/dns-query) + self-test API (/api/selftest)
├─ conf/                         # nginx / dnsmasq / stunnel templates (tokens substituted)
│  ├─ dnsmasq.conf
│  ├─ stream-map.conf            # map SNI -> backend (incl. generated geo-map.inc)
│  ├─ stream-server.conf         # transparent proxy on :443
│  ├─ site-http.conf             # website + DoH :80->:443(8448) server
│  └─ stunnel-dot.conf           # DoT :853 -> local :53
├─ systemd/                      # service units (backend + DoT)
├─ hooks/renew-hook.sh           # certbot deploy hook (reload nginx + DoT)
└─ site/                         # the public website (index, setup, test, terms, privacy, iOS profile)
   ├─ index.html
   ├─ setup.html                 # per-device connection guide
   ├─ test/index.html            # device-side connectivity check
   ├─ terms.html · privacy.html
   └─ ios/eurodns.mobileconfig   # one-tap DoH profile for iPhone/iPad
```

---

## Requirements

* A Linux server: **Debian 12** or **Ubuntu 22.04+** (systemd + `apt`).
* `root` access.
* Free ports: **TCP/UDP 53**, **TCP 80, 443, 853**.
* A hostname that points (A record) at the server's IPv4. If you don't have a
  domain, `deploy.sh` falls back to a free **`*.sslip.io`** name automatically —
  that is enough for a valid Let's Encrypt certificate.
* **An IP whose ASN the target services trust** (see the warning at the top).

---

## Quick start (one command)

```bash
git clone https://github.com/UglyRacoon/eurodns.git
cd eurodns
sudo ./deploy.sh --host dns.yourdomain.com --email you@yourdomain.com
```

`deploy.sh` is idempotent (safe to re-run). With no arguments it auto-detects the
public IPv4, generates an `sslip.io` management hostname, installs packages,
renders all configs, obtains a Let's Encrypt certificate, and starts everything.

### Options

| Flag / env | Meaning | Default |
|---|---|---|
| `--host DOMAIN` | website + DoH/DoT hostname (must resolve to this server) | `smartdns-<ip>.sslip.io` |
| `--email ADDR` | Let's Encrypt contact (enables renewal notices) | none |
| `--ipv6 ADDR` | public IPv6 to serve DNS on | auto-detect |
| `--no-ipv6` | disable IPv6 entirely | off |
| `--brand NAME` | word used across the site | `EuroDNS` |

Example (custom brand, no IPv6):

```bash
sudo ./deploy.sh --host dns.example.com --email ops@example.com --brand MyDNS --no-ipv6
```

### After deploy — endpoints to configure on devices

The script prints them, but they are:

| Method | Value |
|---|---|
| Plain DNS (IPv4) | `<your-IPv4>` |
| Plain DNS (IPv6) | `<your-IPv6>` (if enabled) |
| DNS-over-HTTPS | `https://<host>/dns-query` |
| DNS-over-TLS | `<host>` (port 853) |
| Website / guide | `https://<host>/setup.html` |
| Device check | `https://<host>/test/` |
| iOS profile | `https://<host>/ios/eurodns.mobileconfig` |

---

## Adding / removing unblocked services

Edit `domains.txt` (one second-level domain per line — subdomains are covered
automatically), then:

```bash
/opt/smartdns/build.sh && systemctl restart dnsmasq && nginx -t && systemctl reload nginx
```

---

## Verifying it works (real, not just "IP looks EU")

A device-side check is what actually proves the unblock. Three ways, most→least decisive:

1. **On the device**, open `https://<host>/test/` — it shows the country the *device's*
   requests appear from (via a geo API that is itself proxied).
2. **Coverage from the client** — the *region* call must route through the box too:

   ```bash
   # BOTH must return the proxy IP — googleapis is the one that gates Gemini:
   dig @<your-IPv4> gemini.google.com                  +short   # -> <your-IPv4>
   dig @<your-IPv4> generativelanguage.googleapis.com  +short   # -> <your-IPv4>  (NOT a 172.217.x RU edge)
   ```
   If `generativelanguage.googleapis.com` returns a real Google IP, add it (it is
   already in `domains.txt`); without it Gemini keeps showing "not supported…".
3. **The egress is seen as EU by Google** — confirm once, then forget it: open
   `https://gemini.google.com` **on the server itself** (or `whoer.net`). Unblocked there =
   your provider/IP is clean, and any block on a client is purely a coverage gap. You can
   also see the tunnel reach Google's real edge (`server: ... HTTPServer2`, low `dur`):

   ```bash
   curl -sI --resolve generativelanguage.googleapis.com:443:<your-IPv4> \
        https://generativelanguage.googleapis.com/ | grep -iE '^HTTP/|^server:'
   ```

---

## Why it works **from the server** but not **on the phone** (deep dive)

You can open `gemini.google.com` fine on the EU box, yet the same service fails on a
RU phone — even though both are "EuroDNS". The provider/IP is **not** the issue. The
difference is the **client's DNS + transport path**, which a TCP-SNI Smart DNS can't
control by itself:

1. **Coverage (the only server-side bug, now fixed).** Google's region check does not run
   on `gemini.google.com` — the page makes a second request to `*.googleapis.com`
   (`generativelanguage.googleapis.com`, `*.clients6.google.com`, …) plus `gstatic` /
   `googleusercontent` / YouTube. If only `google.com` is proxied, that region call reaches
   Google **directly from the client's real (RU) IP** → "unsupported". EuroDNS now maps the
   whole Google family (incl. `googleapis`, `gstatic`, `googleusercontent`, `youtube`,
   `google.ru`, `google.dev`). `dig @IP generativelanguage.googleapis.com` must return the
   proxy IP; verify after any change.

2. **Chrome / Google apps ignore Android "Private DNS" (DoT).** This is the #1 reason a
   phone "uses" your DNS but stays blocked: Chrome and the Google/Gemini/YT apps resolve via
   their own resolver, so they never query your DoT. Set the DNS as the **Wi‑Fi static DNS
   server (147.45.114.74)** or the **router DNS** (used by every app), and/or set **Chrome →
   Settings → Security → Use secure DNS → Custom → `https://smartdns.147-45-114-74.sslip.io/dns-query`**.

3. **HTTP/3 / QUIC bypasses a TCP-only proxy.** Google serves almost everything over `h3`
   (QUIC/UDP 443). Our proxy is TCP (`ssl_preread`), so QUIC to our IP lands on whatever
   already holds UDP 443 on the box (here: hysteria) → fails; worse, Chrome caches
   `Alt-Svc`/`SVCB` hints pointing at **Google's real IPs** and then connects QUIC **directly
   to Google (RU)**, bypassing the DNS entirely. **Fix on the device:** disable QUIC
   (`chrome://flags/#enable-quic` → Disabled) and **flush Chrome's cache**
   (`chrome://net-internals/#dns` → Clear host cache; `#sockets` → Flush socket pools), then
   reopen. (ECH has the same effect: a cached ECH config encrypts the SNI so `ssl_preread`
   can't route it. EuroDNS returns NODATA for `HTTPS`/`SVCB` on mapped names so clients don't
   *learn* ECH — but a pre-existing cache only clears by flushing.)

**Net:** the server is identical in both cases — it's the client. The recipe that makes a
phone work: **router/ Wi‑Fi DNS = 147.45.114.74** (or Chrome secure-DNS DoH) **+ disable
QUIC + clear Chrome caches**. A fresh device/router (no Google-IP cache) needs only the
DNS; an already-warm phone needs the cache flush too. This is exactly why another provider
"just worked" — it was applied as the real DNS the browser honors.

---

## Operations

* **Status:** `systemctl status dnsmasq nginx smartdns-backend eurodns-dot`
* **Logs:** `journalctl -u smartdns-backend -u eurodns-dot -f`; nginx access log for the
  SNI proxy at `/var/log/nginx/stream-access.log`.
* **Certificate renewal:** handled by `certbot`'s systemd timer; the deploy hook
  (`/etc/letsencrypt/renewal-hooks/deploy/00-eurodns.sh`) reloads nginx + DoT.
* **Config lives on the server** under `/opt/smartdns` (env), `/etc/nginx`, `/etc/dnsmasq.d`,
  `/etc/stunnel`; the website under `/var/www/smartdns`.

### Uninstall
```bash
sudo ./uninstall.sh              # removes services + configs + cert + /opt|/var/www dirs
```
Flags: `--yes` (no prompt), `--purge-packages` (also apt-purge nginx/dnsmasq/stunnel/certbot —
only if nothing else on the box uses them), `--keep-cert`, `--keep-data`. It stops and disables
`smartdns-backend` and `eurodns-dot`, deletes the nginx site/stream include (and the managed
line from `nginx.conf`, restoring the stock default site), removes the dnsmasq geo config and
stunnel conf, deletes the certbot deploy hook and (optionally) the certificate.

### Coexisting with an existing SNI fronting
`deploy.sh` writes a **standalone** `stream{}` include set
(`/etc/nginx/eurodns-stream.conf` → `stream-enabled/`). If the server already runs another
`stream{}` block (e.g. your own proxy fronting), don't run the stream part blindly —
merge the generated `geo-map.inc` into the existing `map $ssl_preread_server_name`
and add the `server{}` once, so there is exactly **one** `stream{}` in the whole config
(nginx errors on duplicates).

---

## Security & privacy notes

* DNS responses for the mapped domains are public (they're your IP) — that's inherent to
  Smart DNS. Use DoH/DoT to hide *client → server* DNS from the ISP.
* DoT/DoH are only as private as the server; run it on infrastructure you control.
* The Python backend binds `127.0.0.1` only; it is reached through nginx.
* No logs of per-query DNS are enabled by default.
* Respect local law and each platform's ToS. This tool is for legitimate access to
  geo-restricted content; the authors accept no liability for misuse.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| DoH/DoT cert error | `<host>` doesn't resolve to the server, or :80 blocked. Re-run `deploy.sh` after fixing DNS. |
| DNS returns real IP, not yours | Something else owns :53 (resolved/other). Check `ss -lntup \| grep :53`. |
| nginx: `duplicate "stream" directive` | A `stream{}` already exists — see *Coexisting with existing fronting*. |
| Google "unsupported in your country" **on a phone** but fine on the server | The server/IP is fine; it's the client path. 99%: the phone's browser **isn't actually using your DNS** (Android Private DNS is ignored by Chrome/apps) **and/or** it uses **HTTP/3 (QUIC)** which a TCP proxy can't serve. See *"Why it works from the server but not on the phone"*. Fix: set DNS as **Wi‑Fi/router DNS = `147.45.114.74`** (or Chrome secure-DNS DoH), **disable QUIC** (`chrome://flags/#enable-quic` → Disabled), **flush Chrome caches** (`chrome://net-internals/#dns` + `#sockets`). If `dig @IP generativelanguage.googleapis.com` shows the proxy IP, coverage is OK. |
| Works on desktop, not phone in Chrome | Chrome ignores system Private DNS — set the DoH URL inside Chrome's *Secure DNS*. |

---

## License

MIT — see [LICENSE](LICENSE).
