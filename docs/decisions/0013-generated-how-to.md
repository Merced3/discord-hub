# 0013 — Courier-owned generated #how-to

**Status:** accepted

## Context

The server copied project command documentation into `#how-to` by hand. Those
messages drifted whenever a project added, removed, or changed a command. The
owner wants Courier to own the entire channel content and every declaration
change, including a small description change, to be reflected automatically.

The hub already persists project-owned command declarations and authenticated
project identity. It must not expose callback or webhook URLs while producing
public documentation.

## Decision

`#how-to` is generated exclusively from the authenticated, redacted command
view. Courier generates the introduction and one project section per project,
with continued messages when needed. It renders names, descriptions, options,
types, required flags, ranges, and autocomplete markers; it does not invent
project workflows, examples, or setup guidance.

Generated message IDs and rendered-content fingerprints persist in JSON locally
or Postgres in production. Reconciliation runs after successful command syncs
and whenever Discord reports the hub ready:

- unchanged content is not edited;
- only the changed project is edited;
- extra chunks are created and surplus chunks deleted;
- removed/empty projects have generated entries deleted;
- missing stored messages are recreated.

Enabling `HOW_TO_CHANNEL_ID` explicitly authorizes one initial purge of legacy
messages in that dedicated channel. Thereafter reconciliation touches only
stored generated IDs. State advances after Discord operations succeed, so a
restart retries interrupted work. Help-delivery failure is logged and retried;
it does not roll back an already successful slash-command sync.

`GET /commands` exposes the same redacted view: a project sees only itself and
the operator sees all projects. Callback URLs, webhook URLs, and credentials
are never included. Unowned legacy declarations are not published.

`#about` has different ownership: it is stable, human-approved orientation
(where/why), not generated command documentation. `#announcements` records
notable changes. Caretaker proposals for `#about` require explicit approval.

## Consequences

- Consumer projects improve generated help by improving their declarations.
- New projects and future `/ops` declarations need no renderer changes.
- Rich hand-written workflows no longer live in `#how-to`; project docs remain
  the place for guidance beyond command shape.
- The first enablement needs Discord Manage Messages for legacy cleanup and
  normal Send Messages/Manage Messages for generated edits and chunk deletion.
