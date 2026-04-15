# line-bridge

LINE Messaging API bridge for Caddie. Single service, two surfaces:

1. **MCP** at `/mcp` — tools callable by other agents/skills:
   - `notify(text)` — push to the default user_id (Casper)
   - `push_message(user_id, text)` — push to any user
   - `reply_message(reply_token, text)` — reply within a webhook handler window

2. **Webhook** at `/webhook` — receives LINE events, verifies HMAC signature,
   forwards user messages to `claude-runner /chat`, pushes the reply back via
   Line Reply API. On first message from an unknown user, writes the captured
   userId to `/data/user_ids.jsonl` so the operator can promote it into 1P.

## Env

| var | purpose |
|---|---|
| `LINE_CHANNEL_ACCESS_TOKEN` | long-lived bot token |
| `LINE_CHANNEL_SECRET` | used for webhook signature verification |
| `LINE_DEFAULT_USER_ID` | default push target for `notify()` |
| `CLAUDE_RUNNER_URL` | e.g. `http://host.docker.internal:38095` |
| `DEFAULT_WORKSPACE` | claude-runner workspace for /chat (default `training`) |
| `DEFAULT_MODEL` | claude model (default `sonnet`) |

## Port

38106
