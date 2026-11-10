# 0011 — Authenticated project identity for cross-host hub calls

Date: 2026-11-09
Status: accepted (owner approved the four-part design)
Supersedes: ADR 0010

## Context

ADR 0010 trusted the Docker network and Tailscale because every hub caller
was on infrastructure we controlled. Consumers are now moving onto other
machines. Network access still matters, but it no longer tells the hub which
project is calling. Without project identity, one caller could release
another project's channel or send through its Discord webhook identity.

The API and callback architecture stays. Discord webhooks are not an
unlimited-rate replacement: Discord rate-limits them too, and they do not
replace inbound routing, commands, voice, or the hub's ownership boundary.

## Decision

### 1. A credential identifies each project

Every project receives a distinct bearer token. The hub configuration maps
that token to a non-secret project id such as `socratic-partner`:

```http
Authorization: Bearer <project-token>
```

`GET /health` remains public for supervisors. All other API routes require a
token once enforcement is enabled. A missing/invalid credential gets `401`;
a valid project attempting another project's operation gets `403`.

A separate operator token is reserved for neutral operational tooling such
as the shared lab claim/restore helper. It is not a consumer credential.
Tokens are environment secrets, never source, logs, diagnostics, or URLs.

### 2. Ownership is enforced, not declared

Registrations and command routes persist their project id. The authenticated
project—not a request-supplied callback URL—is the identity used for
registration, command, interaction-followup, and voice ownership.

Operations on a registered channel or its threads/posts are limited to the
owning project. This prevents sending through another project's webhook
identity. Ordinary bot-path operations on unregistered channels remain
available to authenticated projects. Existing permission flags and Discord
permissions still apply; authentication does not grant destructive consent.

`POST /registrations` still returns the newly created Discord `webhook_url`
to its caller. `GET /registrations` omits that secret and shows consumers
only their own registrations. The operator can list all registrations for
lab lifecycle work, also without webhook URLs.

Voice identity applies equally to HTTP calls and `WS /voice/stream`. The
WebSocket token travels in the handshake's Authorization header, never a
query parameter. Existing voice response shapes stay unchanged.

### 3. The hub signs callbacks

All hub-to-consumer callbacks use the same delivery path and carry:

```http
X-Discord-Hub-Timestamp: <unix-seconds>
X-Discord-Hub-Signature: v1=<hex-hmac>
X-Discord-Hub-Project: <project-id>
```

The signature is HMAC-SHA256 using the project token over
`"<timestamp>.<exact-raw-body>"`. Consumers use constant-time comparison
and reject timestamps outside their configured clock-skew window (five
minutes recommended). JSON must be verified as raw bytes before parsing.

This covers messages, edits, commands, autocomplete, forum events, voice
state, and reaction markers. Delivery remains best-effort with the existing
timeouts and no retry queue.

### 4. Tailscale remains the network boundary

Tag the hub and consumer/lab machines. ACLs allow consumer tags to reach
only the hub API port and the hub to reach only consumer callback ports.
No public listener or consumer-to-consumer callback access is introduced.
Docker networking remains for colocated services; application identity is
still required there.

## Migration without cutting off current projects

`HUB_AUTH_MODE` has three states:

- `disabled` (default): existing pre-migration behavior. No project tokens
  are required. New deployments must not treat this as the final state.
- `optional`: valid tokens identify projects; missing tokens retain legacy
  behavior; invalid tokens are rejected. Known-project callbacks are signed.
- `required`: bearer credentials are enforced. Startup refuses if persisted
  registrations or commands still lack owners.

Before `required`, `HUB_LEGACY_CALLBACK_PROJECTS` explicitly maps existing
callback URLs to project ids. Both JSON and Postgres state are backfilled.
There is deliberately no first-caller adoption: that would let the wrong
project claim existing state.

Rollout order:

1. Deploy compatible code and secrets; backfill existing ownership.
2. Run `optional` while each consumer adds its API token and callback check.
3. Update the neutral lab helper and operational alert caller with the
   operator credential.
4. Verify no anonymous callers or unowned state remain.
5. Switch to `required`.

The compatibility modes are rollout controls, not a second long-term API.

## Lab policy is unchanged

The shared lab helper can inspect, claim, release, and explicitly restore a
registration using the operator credential and persisted non-secret project
id. Dev-agent keeps `/devlab`, its laptop callback over Tailscale, sandbox
repository, and isolated `data-lab/` state. These are regression acceptance
checks for dev-agent, not a universal project testing command.

No ephemeral, TTL, yielding, or auto-reclaim registration mode is added.
Temporary ownership remains consumer/ops policy.

## Consequences

- Each consumer needs one short migration session before enforcement.
- Hub configuration now holds application secrets in addition to Discord's.
- Existing JSON command state upgrades to a versioned representation;
  Postgres adds nullable command-route project identity in revision
  `0002_command_project_identity`.
- `required` cannot safely start with ambiguous legacy state. This is an
  intentional fail-closed check with non-secret channel/command identifiers.
- Token rotation requires coordinated configuration updates; overlap tokens
  are deferred until an actual rotation need demonstrates the requirement.
