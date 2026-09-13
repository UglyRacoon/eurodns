# chain-eu-exit — give Google an EU/US exit (Option A)

## Why this exists
The Smart‑DNS box's own IPv4 `147.45.114.74` (AS50053) **and** its IPv6
`2a0d:6c2:17:51b::` are geolocated **as Russia by Google** — verified directly:
`curl -4/-6 https://www.google.com/` both return `intl/ru/` / `prefdom=RU`. Free
Cloudflare WARP egress from this box is also `intl/ru`. So IP‑geo‑gated Google
services (Gemini "isn't supported in your country", some `googleapis` AI
endpoints) stay blocked no matter how the DNS/DoT/DoH/SVCB/QUIC transport is
fixed — the *source IP* is the gate.

The only reliable fix is to send Google traffic out an IP Google geo‑reads as a
supported region: a second, clean VPS in a reputable EU datacenter (Hetzner /
Scaleway / OVH / …) or a US box, or a residential EU/US proxy. xbox‑dns.ru
simply has such exits — that is the whole difference.

## What the script does
`deploy-eu-exit.sh` brings up a WireGuard link to the second box and
policy‑routes **only Google's IP ranges** (`google-cidrs.txt`) through it. The
EU box MASQUERADEs that traffic out its own NIC, so Google sees the EU box's IP.
Nothing else changes: SSH, the Smart‑DNS resolver/DoT/DoH, and every non‑Google
destination still use this box directly. The phone keeps "just entered DNS".

The QUIC relay (`quic-relay/`) DNATs inbound UDP/443 to a Google front‑end IP,
and those GFE ranges (142.250/15, 142.251/16, 216.58/16, 172.217/16, …) are all
inside `google-cidrs.txt`, so TCP *and* QUIC egress compose through the same EU
exit.

## Run it (on the Smart‑DNS box)
```
apt-get install -y wireguard-tools sshpass
./deploy-eu-exit.sh <EU_HOST> <EU_USER> '<EU_PASS>' 51820     # or '-' for key auth
# verify:
curl -4 https://www.google.com/ | grep -o 'intl/[a-z]*/'      # should NOT be intl/ru
./deploy-eu-exit.sh down                                       # reversible
```
Then re‑open `gemini.google.com` on the phone — no other client change.

If the check still shows `ru`, the second box's ASN is also RU‑tagged: use a
different provider/region (an actual EU or US datacenter IP).
