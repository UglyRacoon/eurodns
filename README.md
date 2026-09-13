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
        ├──────────────DNS───────────────►│ resolver answers = PROXY_IP ─────┤
        │  ①' HTTPS/SVCB hint alpn=h2     │  + SVCB(alpn=h2, hint=PROXY_IP)   │
        │  ② TLS connect, SNI=gemini…     │                                  │
        ├──────────:443 (ssl_preread)────►│  nginx stream forwards RAW TLS ─►│  (sees EU IP ✔)
        │                                  │  to the REAL gemini.google.com   │
```

1. **DNS rewrite + SVCB hint.** The authoritative resolver (a small `python` service on
   `:53`) returns the *proxy's own public IP* for every domain in
   [`domains.txt`](domains.txt). Critically, for those names it also synthesises a
   **`HTTPS`/`SVCB` record** (`alpn=h2` + `ipv4hint`/`ipv6hint` = proxy IP). That record
   is what makes a **plain "just set the DNS" setup work**: the client's browser/OS reads
   it and connects straight to our IP over **HTTP/2**, so QUIC (UDP/443), ECH and any
   cached Google `Alt-Svc`/`HTTPS` hints never get a chance to bypass the proxy. All
   other names are forwarded to an upstream resolver normally.
2. **Transparent SNI proxy.** `nginx` in `stream` mode reads the TLS **SNI** via
   `ssl_preread` and forwards the raw TCP session to the *real* backend. Because the
   connection to the backend is opened **from the EU server**, the backend sees an
   EU source IP and serves the un-restricted content. **No MITM, no certificate
   pinning issues** — the client's own TLS verification still passes end-to-end.
3. **Encrypted DNS.** The same server offers **DNS-over-HTTPS** and **DNS-over-TLS**
   (`stunnel` → `:53`); those paths also carry the SVCB hint from step 1, so Private-DNS
   / DoH clients work identically to plain DNS.

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
├─ resolver/
│  └─ smartdns-resolver.py       # authoritative :53 — A/AAAA + SVCB(alpn=h2, hints=proxy IP) for geo domains, forwards the rest to :5353
├─ conf/                         # nginx / dnsmasq / stunnel templates (tokens substituted)
│  ├─ dnsmasq.conf               # local forwarder on 127.0.0.1:5353 (the resolver owns public :53)
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
/opt/smartdns/build.sh && systemctl restart eurodns-resolver dnsmasq && nginx -t && systemctl reload nginx
```
The resolver re-reads `domains.txt` on start, so it is what actually needs the restart.

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
   # and the client must also be served the SVCB hint that forces Chrome onto the proxy:
   kdig @<your-IPv4> -t HTTPS generativelanguage.googleapis.com +short   # -> 1 . alpn="h2" ipv4hint=<your-IPv4> ...
   ```
   (Older `dig` prints `FORMERR` on `ipv4hint`/`ipv6hint` even though the record is valid —
   use `kdig`/`getdnsapi` or a real browser to inspect it.) If
   `generativelanguage.googleapis.com` returns a real Google IP, add it (it is already in
   `domains.txt`); without it Gemini keeps showing "not supported…".
3. **The egress is seen as EU by Google** — confirm once, then forget it: open
   `https://gemini.google.com` **on the server itself** (or `whoer.net`). Unblocked there =
   your provider/IP is clean, and any block on a client is purely a coverage gap. You can
   also see the tunnel reach Google's real edge (`server: ... HTTPServer2`, low `dur`):

   ```bash
   curl -sI --resolve generativelanguage.googleapis.com:443:<your-IPv4> \
        https://generativelanguage.googleapis.com/ | grep -iE '^HTTP/|^server:'
   ```

---

## Why "just enter the DNS" is enough (the SVCB mechanism)

The old problem with a pure TCP `ssl_preread` SNI proxy is that it can't *force* the client
to use the proxy for everything. Modern browsers prefer **HTTP/3 (QUIC / UDP 443)** and keep
**ECH** / `Alt-Svc` / `HTTPS` hints cached from earlier direct connections to Google. A plain
`address=/domain/IP` answer wasn't enough: a warm browser would QUIC straight to Google's
*real* IP and never touch our proxy — so the region check saw the client's real (RU) IP.

EuroDNS fixes this at the **DNS layer itself**, the same way commercial Smart-DNS (e.g.
xbox-dns.ru) do. For every proxied name it returns, alongside `A`/`AAAA`, a synthesised
**`HTTPS`/`SVCB` record**: `alpn=h2` with `ipv4hint`/`ipv6hint` = our proxy IP. The client
reads that authoritative hint and connects **directly to our IP over HTTP/2**, *before* any
QUIC / ECH / cached-Google-IP behaviour can take effect. So you enter the DNS — plain IPv4,
Private DNS (DoT) or Chrome secure-DNS (DoH) — and Gemini/Google/YouTube just work in a real
browser, with **no flags, no QUIC-off, no cache flush, no profile/CA install**.

What a client should point at EuroDNS (all three are now equivalent):

* **Wi‑Fi / router static DNS = `147.45.114.74`** — most reliable; every app + browser honors it.
* **Private DNS (DoT) = `smartdns.<host>`** — works in Chrome and Google apps too.
* **Chrome secure-DNS DoH = `https://<host>/dns-query`** — for locked-down networks.

Only if a *specific* long-used app misbehaves is a one-off "Clear host cache"
(`chrome://net-internals/#dns`) + restart helpful — it is no longer required.

---

## Operations

* **Status:** `systemctl status eurodns-resolver dnsmasq nginx smartdns-backend eurodns-dot`
* **Logs:** `journalctl -u eurodns-resolver -u smartdns-backend -u eurodns-dot -f`; nginx
  access log for the SNI proxy at `/var/log/nginx/stream-access.log`.
* **Resolver owns public :53; dnsmasq is a loopback forwarder on `127.0.0.1:5353`.**
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
| Google "unsupported in your country" **on a phone** but fine on the server | The server/IP is fine — it's the client path. 99% the device simply **isn't using your DNS**: point **Wi‑Fi/router static DNS = `147.45.114.74`** or **Private DNS (DoT) = `smartdns.<host>`** (both now force HTTP/2 via the SVCB hint, so QUIC/ECH/cache are no longer an issue). Verify: `dig @<IPv4> generativelanguage.googleapis.com` and `kdig @<IPv4> -t HTTPS …` both point at your IP. |
| Works on desktop, not phone in Chrome | Chrome is now told to use our IP via the `HTTPS`/`SVCB` record, so plain Wi‑Fi DNS or Private DNS is enough. If a specific already-warm app still misbehaves, "Clear host cache" in `chrome://net-internals/#dns` once. |

---

## License

MIT — see [LICENSE](LICENSE).
