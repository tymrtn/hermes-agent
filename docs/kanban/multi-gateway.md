# Multi-gateway deployment

Hermes supports multiple gateway processes running concurrently — one per profile
(default, writer, admin, coder, researcher). Each gateway opens its own connection
to platform APIs and delivers messages for its profile's subscribers.

Task subscriptions also cover review feedback. A `changes_requested` review
event is delivered as an actionable review-BLOCK notification. Subscriptions
using `notify+wake` additionally wake the exact originating chat/thread/session
so the controller inspects the existing card and current run; `notify` remains
passive-only and `wake` remains wake-only. Review feedback never creates,
unblocks, requeues, or otherwise mutates a task.

## Single-dispatcher posture

Only one gateway owns the kanban dispatcher. The owning gateway keeps
`kanban.dispatch_in_gateway: true` (the default); every other gateway sets it
to `false`.

**Why this matters:** dispatching is single-owner so multiple gateways do not
race to spawn the same work. Notification delivery is profile-owned instead:
each gateway polls only subscriptions for profiles whose platform adapters it
hosts. The atomic event claim prevents duplicate delivery across watcher
processes.

## Configuration

On the dispatch-owning gateway (typically the `default` profile), no change is
needed. On every other profile gateway, add to `~/.hermes/config.yaml`:

```yaml
kanban:
  dispatch_in_gateway: false
```

Or set the env var: `HERMES_KANBAN_DISPATCH_IN_GATEWAY=false`

## What each gateway does

| Gateway role | dispatch_in_gateway | Opens subscribed board DBs? | Dispatcher | Notifier |
|---|---|---|---|---|
| default (confirmed dispatch-lock owner) | true (default) | yes | yes | owned profiles + legacy unstamped subscriptions |
| writer, admin, coder, etc. | false | yes, when the profile has subscriptions | no | that gateway's owned profiles |

Non-dispatch gateways still deliver messages for their own platform adapters
(Telegram, Discord, etc.). They do not dispatch tasks, and they skip boards
that have no subscriptions owned by their profiles.

## Safe re-enable after an incident

1. Keep every gateway's `kanban.dispatch_in_gateway` set to `false`.
2. Configure the intended owner with conservative limits before enabling it:

   ```yaml
   kanban:
     dispatch_in_gateway: false
     max_in_progress: 4
     max_in_progress_per_profile: 2
   ```

3. Confirm existing `running` tasks across every board. They count toward the
   limits and are preserved; do not reclaim healthy workers to make room.
4. Restart only the intended owner while it is idle, and verify its log still
   says the dispatcher is disabled.
5. Set `kanban.dispatch_in_gateway: true` on that owner, restart only that
   gateway, and verify it holds `.dispatcher.lock` and logs the configured
   limits. Leave all other gateways disabled.
6. Observe one full dispatch interval. The total running count must stay at or
   below `max_in_progress`, and no assignee may exceed
   `max_in_progress_per_profile`, before re-enabling any fleet watchdog.

Rollback is one config change: set `kanban.dispatch_in_gateway: false` on the
owner and restart only that gateway. This stops new claims without terminating
workers already running. Do not delete lock files or kill workers as rollback;
the OS releases locks when the owning gateway exits.
