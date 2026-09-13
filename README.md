# EuroDNS — Smart DNS for unblocking geo-restricted services

**EuroDNS** is a self-hosted **Smart DNS** + **transparent SNI proxy** that restores
access to geo-restricted services (Xbox Live, Gemini, ChatGPT, Claude, Spotify,
Twitch, Notion, JetBrains, Supercell games and more) — **without a VPN**, without
a client app, and **without slowing down** normal traffic.

It is a functional clone of the *xbox-dns.ru* concept, rebuilt with a modern,
fully-scripted stack you can deploy on **any** Linux server in a few minutes.

> ⚠️ **Important — the IP you unblock with matters.** A Smart DNS only works if the
> server's **egress IP** is one the target service considers an *allowed* region.
> Some low-cost "EU" hosts (e.g. certain VDSka / reseller ranges) are geo-flagged as
> Russia by Google specifically, so Google services (Gemini, YouTube) stay blocked
> even though Cloudflare/MaxMind see the same IP as the Netherlands. **Choose a host
> whose ASN Google trusts** (netcup, Hetzner, OVH, DigitalOcean, Scaleway, AWS, …).
> This is a property of the IP, not of the software.

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

A device-side check is what actually proves the unblock. Two ways:

1. On the device, open `https://<host>/test/` — it shows the country the *device's*
   requests appear from (via a geo API that is itself proxied).
2. From a client using the service:

   ```bash
   # DNS rewrite present?
   dig @<your-IPv4> gemini.google.com +short        # -> <your-IPv4>

   # what does Google's OWN geo say about the egress? (the real gatekeeper)
   curl -s --resolve gemini.google.com:443:<your-IPv4> https://www.google.com/ \
        | grep -oE 'google\.ru|hl=ru&' | head -1     # empty = good; google.ru = your ASN is geo-flagged
   ```

If step 2 prints `google.ru`/`hl=ru` from your EU server, your IP is flagged by Google
regardless of country — move the egress to another provider (the software is fine).

---

## Operations

* **Status:** `systemctl status dnsmasq nginx smartdns-backend eurodns-dot`
* **Logs:** `journalctl -u smartdns-backend -u eurodns-dot -f`; nginx access log for the
  SNI proxy at `/var/log/nginx/stream-access.log`.
* **Certificate renewal:** handled by `certbot`'s systemd timer; the deploy hook
  (`/etc/letsencrypt/renewal-hooks/deploy/00-eurodns.sh`) reloads nginx + DoT.
* **Config lives on the server** under `/opt/smartdns` (env), `/etc/nginx`, `/etc/dnsmasq.d`,
  `/etc/stunnel`; the website under `/var/www/smartdns`.

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
| Google still says "unsupported in your country" | Your **egress IP ASN is geo-flagged as RU by Google**. Different provider needed (see top warning). |
| Works on desktop, not phone in Chrome | Chrome ignores system Private DNS — set the DoH URL inside Chrome's *Secure DNS*. |

---

## License

MIT — see [LICENSE](LICENSE).
