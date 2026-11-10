# Cross-host authentication rollout

This is the operator checklist for ADR 0011. Keep the current API and
callbacks; this change adds identity and proof, not a new transport or quota.

## The short version

- Each project gets its own password-like API token.
- The hub uses it to protect channel/command/voice ownership.
- The hub signs callbacks so the project can prove who sent them.
- Tailscale limits which machines can connect in the first place.

There is no new per-project request limit.

## 1. Prepare configuration

Generate independent random tokens outside Git. Put them only in the hub and
consumer environment files:

```dotenv
HUB_AUTH_MODE=optional
HUB_PROJECT_TOKENS={"socratic-partner":"...","teaching-agent":"...","dev-agent":"..."}
HUB_OPERATOR_TOKEN=...
```

Map each existing callback exactly once so old registrations and commands
retain their real owner:

```dotenv
HUB_LEGACY_CALLBACK_PROJECTS={"http://consumer:9000/discord":"socratic-partner"}
```

Do not print these values in shell logs, Discord, diagnostics, or commits.
Project ids are safe diagnostics; tokens and Discord webhook URLs are not.

## 2. Start in optional mode

Optional mode keeps tokenless callers working while signed callbacks and
authenticated calls are introduced. A wrong token is rejected rather than
silently treated as an anonymous caller.

At startup the hub writes the mapped project ids into its normal JSON or
Postgres state. It does not change consumer configuration.

## 3. Migrate each caller

Each consuming project must:

1. Send `Authorization: Bearer <its-token>` on every hub HTTP request.
2. Send the same header in the `/voice/stream` WebSocket handshake.
3. Read the callback body as raw bytes, check the timestamp, and compare the
   HMAC signature before parsing or acting on the event.
4. Keep its callback URL private to the intended Tailscale/Docker network.

The operational alert sender and neutral `ops/lab/lab.py` helper use the
operator token. Restoring a registration through the operator API includes
the snapshotted non-secret `project_id`; no Discord webhook URL is needed or
accepted. Restoration creates a fresh Discord webhook.

## 4. Apply Tailscale rules

In the tailnet policy, label the hub and consumer/lab machines. Permit:

- Consumer/lab tags → hub TCP `8100`.
- Hub tag → the documented consumer callback TCP ports.

Deny other paths by default and keep the ports off public interfaces. Do not
store tailnet secrets or private addresses in this public repository.

## 5. Enforce

After all callers are migrated, set:

```dotenv
HUB_AUTH_MODE=required
```

The hub refuses to start in this mode if any saved registration or command
still lacks an owner. Add the correct explicit callback mapping rather than
registering over it or letting a caller adopt it.

`GET /health` stays public. Everything else, including the voice WebSocket,
requires a valid project or operator credential.

## Lab acceptance

Follow `ops/LAB.md`. For dev-agent, retain `/devlab`, the laptop callback,
sandbox repository, and isolated `data-lab/` state. Verify authenticated
claim, signed command/message delivery, release, and production health.
Always release the shared channel. Restore a previous owner only from the
explicit snapshot. No lab TTL or automatic takeover is part of the hub.
