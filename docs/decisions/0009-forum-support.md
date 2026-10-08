# 0009 — Forum support: posts, tags, lifecycle events

**Status:** accepted by the owner (2026 — same session as drafting).

## Context

A forum channel is a container; each post is a thread whose starter
message is the post body. The hub already has the thread primitives this
builds on: thread creation, registration inheritance for messages inside
threads (the `thread` payload field), scoped thread deletion (ADR 0004),
history reads, and per-project webhook identity (ADR 0002). Forum support
is therefore an extension of the existing routing model, not a new
subsystem.

The owner wants full coverage of what a bot is allowed to do in the forum
arena: read, list, post, comment, react, manage tags, and observe changes
— including caretaker-grade tag administration on the forum channel
itself, which fits the hub's "server caretaker" role (report and, with
explicit permission, change structure) rather than any single project's
messaging needs.

## Decision

### Vocabulary stays Discord-free

The API says "forum", "post", "tag". Consumers never see "thread" or
"starter message". A post ID *is* a thread ID internally, and the
existing thread endpoints remain valid on posts (documented, not hidden).

### Forum channels are registrable

A forum channel can be registered exactly like a text channel. Posts
under it inherit the registration through the existing parent-lookup in
`on_message`, so comments in posts already route to the owning project.
Registration gains two new declared-intent flags, both default `false`,
same precedent as `allow_thread_deletion` (ADR 0004):

- `allow_forum_moderation` — permits retag / rename / archive / lock /
  pin of posts the bot did not create, and tag-set administration on the
  forum channel (below). These require Discord's Manage Threads /
  Manage Channels; the flag is declared intent, not permission — the
  hub never grants itself permissions, and the consumer still confirms
  destructive actions with the owner.
- `forum_events` — the project wants forum lifecycle events POSTed to
  its callback URL (below).

Deregistering the forum revokes both, automatically.

### Read surface

- `GET /server/structure` lists forum channels as `type: "forum"`,
  including `available_tags` (id, name, emoji, moderated flag).
- `GET /forums/{channel_id}/posts` lists posts: id, title, author,
  applied tags, created-at, comment count, and pinned / locked /
  archived flags. Defaults to active posts only; `include_archived=true`
  paginates Discord's archived iterator, bounded by a `limit` parameter
  (default and max documented in api.md) so no request can trigger an
  unbounded crawl of an old forum.
- `GET /forums/posts/{post_id}` returns one post: title, body (the
  starter message), tags, flags.
- Comments are read with the existing `GET /channels/{post_id}/history`.

### Write surface

- `POST /forums/{channel_id}/posts` creates a post: `{title, text,
  tag_ids?}`. Tags applied at creation need only Create Posts, so this
  is ungated beyond normal permissions. Attachments on the starter
  message follow the ADR 0007 pattern (raw bytes, tier-adaptive cap per
  ADR 0008).
- Commenting is the existing `POST /messages` with the post ID as
  `channel_id`; reactions, edits, typing, attachments likewise reuse the
  existing message endpoints unchanged.
- `PATCH /forums/posts/{post_id}` — rename, retag, archive/unarchive,
  lock, pin. Editing tags on a post the bot did not create requires
  Manage Threads, so these actions are gated on the parent forum's
  `allow_forum_moderation` flag; 403 otherwise, exactly the ADR 0004
  failure contract.
- Deleting a post is the existing `DELETE /threads/{post_id}`, still
  gated on `allow_thread_deletion`.

### Tag administration (caretaker)

Creating, editing, and deleting the forum channel's *available tag set*
is server-structure work, not messaging: `POST /forums/{channel_id}/tags`,
`PATCH /forums/{channel_id}/tags/{tag_id}`,
`DELETE /forums/{channel_id}/tags/{tag_id}`. These require Manage
Channels and are gated on the forum's registration carrying
`allow_forum_moderation: true` — an unregistered forum exposes its tag
set read-only via `/server/structure`. The hub performs the operation;
whether a tag *should* exist is the caller's policy.

### Webhook identity (ADR 0002) extends into forums

Discord webhooks post into forums (`thread_name` on execute creates a
post; `thread_id` targets an existing post). So in a registered forum:

- creating a post goes through the forum's registration webhook with the
  project identity, falling back to the plain bot if the webhook send
  fails — same fallback contract as text channels;
- comments in a post under a registered forum go through the webhook
  with `thread_id`;
- attachments on posts/comments keep ADR 0007's webhook path.

One honest limitation: a webhook-created post cannot apply tags in the
same call (Discord applies tags via the thread API, not webhook
execute). When `tag_ids` are present the hub creates the post with the
bot identity (tags applied), skipping the webhook. The response's
`"via"` field already exists to say which path was taken; consumers
that need both identity and tags create tagged and accept bot identity,
or create untagged via webhook and retag with `PATCH` (which is a bot
operation anyway).

### Lifecycle events, including renames

With `forum_events: true` on the parent forum's registration, the hub
POSTs to the project callback:

- `forum.post_created` — new post under the forum (from
  `on_thread_create`), with title, author, tags.
- `forum.post_updated` — from `on_thread_update`, with a `changed`
  list naming what moved: `name` (renamed — the owner's explicit ask),
  `tags`, `archived`, `locked`, `pinned`.
- `forum.post_deleted` — from `on_thread_delete`.
- `forum.channel_updated` — from `on_guild_channel_update` on the forum
  itself: renamed, tag set changed, slowmode / default layout changes.

Renames of posts *and* of the forum channel are therefore both observed.
Events about posts are only routed for posts under the registered forum
(scoped, like message routing). As with all hub events, the payload
carries facts; meaning is the project's policy.

## Why

- **Extension, not subsystem:** posts being threads means routing,
  history, deletion scoping, and identity all reuse proven paths; the
  new code is surface area (endpoints, event translation), not new
  mechanism (PRINCIPLES: the hub knows Discord, projects know meaning).
- **Declared-intent flags per registration:** moderation and tag
  administration stay opt-in at the same place the project claims the
  channel; the hub keeps no per-project config of its own (ADR 0004
  precedent). Same caveat as 0004: no caller authentication exists, so
  the flag is accident-proofing and explicit intent, not a security
  boundary.
- **Bounded archived reads:** archived posts live outside cache behind a
  paginated iterator; a documented `limit` keeps a curious consumer from
  turning one GET into a thousand Discord requests.
- **Webhook limitation stated, not worked around:** the tag-vs-identity
  tradeoff on post creation is a Discord constraint; making it visible
  via `via` keeps the contract honest instead of silently dropping one
  or the other.

## Consequences

- api.md gains a Forum section; `/server/structure` responses grow a
  `forum` type (additive, existing consumers unaffected).
- Registration JSON gains two fields with safe defaults; existing
  registration files load unchanged.
- Phase order: (1) structure + reads, (2) create/comment via bot, (3)
  webhook identity + PATCH + tag administration + events. Each phase is
  independently shippable.
