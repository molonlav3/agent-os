# Remote Access

The gateway controls chat, tools, sessions, channels, approvals and
configuration, all with your provider credentials. It therefore listens on
loopback (`127.0.0.1:18791`) by default, and the safest way to reach it from
another machine is to keep it there and bring the connection to it.

Use this page when you want the Web UI, the desktop app, the CLI or an MCP
client on one machine to drive a gateway running on another.

## Pick a Path

| Path | Gateway bind | Who authenticates | Good for |
| --- | --- | --- | --- |
| [SSH tunnel](#ssh-tunnel) | loopback | your SSH key | any host you can SSH into; nothing to install |
| [Tailscale Serve](#tailscale-serve) | loopback | your tailnet identity, plus the gateway token | your own devices, from anywhere, over HTTPS |
| [Reverse proxy](#reverse-proxy-with-trusted-proxy-mode) | loopback or LAN | the proxy (SSO, mTLS, basic auth) | a team entry point you already run |
| [Token on a LAN bind](#token-on-a-lan-or-public-bind) | `0.0.0.0` or a LAN IP | the gateway token | a trusted home or lab network |

Whatever you choose, run `agentos doctor` afterwards: it reports the risky
combinations (`gateway.bind_no_auth`, `gateway.allowed_origins_wildcard`,
`gateway.cors_wildcard_with_credentials`, `gateway.trusted_proxy_unset`,
`gateway.auth_rate_limit_off`, `gateway.token_short`) with the command that
fixes each.

## SSH Tunnel

The universal path. The gateway stays on loopback on the remote host; SSH
forwards a local port to it:

```sh
ssh -N -L 18791:127.0.0.1:18791 user@host
```

Then use the gateway as if it were local:

```text
http://127.0.0.1:18791/control/
```

```sh
agentos gateway status --gateway ws://127.0.0.1:18791/ws
```

`-N` opens no shell; leave the command running for as long as you need the
tunnel. To keep it up across drops, use your SSH client's keep-alive options
or a tool such as `autossh`. If port 18791 is taken locally, forward another
one (`-L 18800:127.0.0.1:18791`) and open `http://127.0.0.1:18800/control/`.

No AgentOS configuration changes: to the gateway the browser is a loopback
peer, so the default `auth.mode = "none"` keeps working. Set a token anyway
if other people have accounts on the remote host.

## Tailscale Serve

[Tailscale Serve](https://tailscale.com/kb/1312/serve) publishes a loopback
service to your tailnet over HTTPS with a `*.ts.net` certificate. The gateway
keeps its loopback bind:

```sh
tailscale serve --bg --https=443 http://127.0.0.1:18791
```

The console is then at `https://<machine>.<tailnet>.ts.net/control/`. Two
settings make it work from a browser on another device:

1. Enforce a token. Serve connects from loopback, so without one every
   device on your tailnet would be admitted as if it sat at the keyboard:

   ```sh
   agentos config set auth.mode token
   agentos config set auth.token "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
   ```

2. Allow the tailnet origin. On a loopback bind the gateway accepts browser
   requests only from loopback origins (the guard against cross-site
   WebSocket hijacking and DNS rebinding); add the Serve origin explicitly:

   ```sh
   agentos config set control_ui.allowed_origins '["https://<machine>.<tailnet>.ts.net"]'
   ```

Restart the gateway, open the console URL, enter the token under Overview ›
Gateway connection › Token and press Connect.
`tailscale serve status` shows what is published; `tailscale serve --https=443
off` takes it down. Do not use Tailscale **Funnel** for the gateway: it
publishes the port to the whole internet.

## Reverse Proxy with Trusted-Proxy Mode

When a reverse proxy already authenticates your users (SSO, mTLS, basic
auth), let it be the only thing that can reach the gateway and tell the
gateway to trust it:

```toml
host = "127.0.0.1"            # or the interface the proxy connects from

[auth]
mode = "trusted-proxy"
trusted_proxy = "127.0.0.1"   # IPs or CIDR blocks, comma-separated

[control_ui]
allowed_origins = ["https://agent.example.com"]
```

In this mode a request is admitted only when the *transport peer* (the
address the TCP connection comes from, never a header) is listed in
`auth.trusted_proxy`. Anything else gets `401`. The mode is accepted on a
non-loopback bind for that reason; with an empty `trusted_proxy` nothing is
admitted and `doctor` reports `gateway.trusted_proxy_unset`.

The proxy must forward WebSocket upgrades on `/ws` and should append the
client address to `X-Forwarded-For`. The gateway reads that header only from
a trusted peer and walks it right to left, skipping trusted hops, so the
first address your proxies did not add is the client used for rate limiting.
An address that names the gateway host itself (loopback or one of its own
interfaces) is treated as spoofed and ignored. List every proxy hop, for
example `trusted_proxy = "10.0.0.0/8"` behind a load balancer.

A minimal Caddy site:

```text
agent.example.com {
    forward_auth ... # your authentication
    reverse_proxy 127.0.0.1:18791
}
```

A minimal nginx location:

```nginx
location / {
    proxy_pass http://127.0.0.1:18791;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

## Token on a LAN or Public Bind

Binding beyond loopback requires an enforced auth mode; the gateway refuses
to start with `auth.mode = "none"` on `0.0.0.0` or a LAN address:

```sh
agentos config set auth.mode token
agentos gateway run --listen 0.0.0.0
```

A token is generated at startup when none is set. Clients send it as
`Authorization: Bearer <token>` (HTTP) or in the WebSocket `connect`
request; it is never accepted in a query string. Wrong tokens are throttled
per client: ten failures in a minute lock that client out for five minutes
(`429` with `Retry-After` over HTTP, `AUTH_RATE_LIMITED` over the
WebSocket). Tune it under `[auth.rate_limit]`; keep it on for any
non-loopback bind.

Only do this on a network you trust. The traffic is plain HTTP unless you
also set `[tls] certfile` and `keyfile`.

## Rotating the Token

Generate a new token, restart, then update every client:

```sh
agentos config set auth.token "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
agentos gateway restart
```

Use at least 24 random characters (`doctor` warns below that as
`gateway.token_short`). Clients holding the old token start getting
`UNAUTHORIZED`, and repeated attempts count against the failed-auth limit, so
update scripts and the desktop app's gateway settings promptly.

## What Never to Do

- Do not bind `0.0.0.0` with `auth.mode = "none"`. The startup guard refuses
  it; do not work around the guard.
- Do not forward the gateway port to the internet from your router, or
  publish it with a public tunnel, without an authenticating proxy in front.
- Do not put `"*"` in `control_ui.allowed_origins`, and do not combine
  `cors.allowed_origins = ["*"]` with `cors.allow_credentials = true` on a
  non-loopback bind.
- Do not use `trusted-proxy` mode unless the listed addresses really are
  your proxies, and the gateway port is reachable only through them.
- Do not put the token in a URL; use the header.

Read next:

- [`gateway.md`](gateway.md)
- [`http-api.md`](http-api.md)
- [`configuration.md`](configuration.md)
- [`troubleshooting.md`](troubleshooting.md)

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/use-agent-os/agent-os/issues/new?template=docs_report.yml)
