# Board management API

All paths below are relative to the Board server. A reverse proxy may add a prefix such as `/clawteam`.

`GET /api/capabilities` returns `api_version` and `features`. Clients must check for `task_create_options` before sending priority or dependencies to the create endpoint: older servers silently ignore these fields. Other feature names are `task_update`, `message_send`, `message_broadcast`, `agent_spawn`, and `agent_list`.

## Requests and responses

Management requests use `Content-Type: application/json`, a JSON object, and a body no larger than 1 MiB. Unknown fields and incorrect types return 400. The team must already exist; the API does not silently create teams. Identifiers follow the existing letters/digits/dot/underscore/hyphen rules.

| Method | Path | JSON fields |
| --- | --- | --- |
| POST | `/api/team/{team}/task` | Required `subject`; optional `description`, `owner`, `priority`, `blocks`, `blocked_by` |
| PATCH or POST | `/api/team/{team}/task/{id}` | At least one of `subject`, `description`, `owner`, `status`, `priority`, `add_blocks`, `add_blocked_by`; optional `caller` (default `board`) and `force` (default false) |
| POST | `/api/team/{team}/messages` | Required `to`, `content`; optional `from` (default `board`), `key` |
| POST | `/api/team/{team}/broadcast` | Required `content`; optional `from` (default `board`), `key` |
| POST | `/api/team/{team}/agents/spawn` | See launch request below |
| GET | `/api/team/{team}/agents` | Registered members, their runtime backend and `alive` state; null means unknown |

Task statuses are `pending`, `in_progress`, `completed`, and `blocked`. Priorities are `low`, `medium`, `high`, and `urgent`. Dependency fields are arrays of task IDs. Updates use the same task store as the CLI, including lock handling and dependency resolution. Empty strings can clear owner or description; null leaves an optional field unchanged. `caller` and message `from` are coordination labels, not authenticated identities.

Task creation retains `{"status":"ok","task_id":"..."}` and adds a `task` object. Update returns `{"status":"ok","task":{...}}`. Message send returns `{"status":"ok","message":{...}}`; the message has a `requestId` for querying history. Broadcast returns `status`, `count`, and `recipients`. Sending does not consume any inbox.

## Launch an agent

```json
{
  "agent_name": "reviewer",
  "backend": "tmux",
  "profile": "configured-profile",
  "repo": "/absolute/server/repository",
  "task": "Review the pending changes",
  "workspace": true,
  "skip_permissions": false,
  "keepalive": true
}
```

Provide a configured `profile` or an explicit `command` array. Paths, binaries, profiles, and credentials belong to the server environment. `repo` must be an existing absolute directory; `workspace: true` additionally requires a Git repository. Backends are `tmux`, `subprocess`, and `wsh`. Optional fields include `agent_type` (default `general-purpose`), `resume` and `replace` (both false). Booleans must be JSON booleans. `replace` explicitly permits replacing an existing live agent; it is never automatic.

The Board runs its installed ClawTeam launcher in an isolated local subprocess with an argv list, without SSH or a shell command string. This preserves profile resolution, member registration, workspace setup, prompt generation, session resume, and launcher rollback behavior. In JSON mode, launcher diagnostics go to stderr. The API does not accept arbitrary CLI flags or environment-variable overrides.

A successful launch returns the native `status: spawned`, `backend`, `agentName`, `agentId`, and `message`. This acknowledges launch, not completion of the assigned work. Query `/agents` for runtime state and the team snapshot for work results.

## Errors and access boundary

Errors return a JSON `error`; validation errors also include field details without echoing input values. Status codes are 401 for missing or incorrect API keys, 400 for invalid input, 403 for a cross-origin browser request, 404 for missing team/task/recipient, 409 for a lock or launch conflict, 413 for body size, 415 for content type, and 500/502/504 for storage/launcher/launch-timeout failures.

External clients use `/openapi/` in place of `/api/` for the management endpoints above. Set `CLAWTEAM_BOARD_API_KEY` in the Board server environment and send `Authorization: Bearer <key>`. Every `/openapi/` request requires a valid key; an unset or empty server key disables external access. Missing or incorrect keys return 401 before any operation. Keys in query strings are not accepted. Use HTTPS for network access.

The browser dashboard continues to use `/api/` without API key authentication. Expose only `/openapi/` through the public reverse proxy; keep `/api/` and the dashboard restricted to the trusted network or existing deployment authentication. The external namespace exposes only the documented management operations, not prompts, terminals, events, or the fetch proxy. Browser management calls must be same-origin. Non-browser HTTP clients may omit `Origin`. Write responses do not expose wildcard CORS. Keep the Board behind the deployment's access controls, as with its existing writable terminals.

Launches are serialized within one Board process. A 60-second launcher timeout returns 504; an agent may already have started. Failed or interrupted writes are never automatically retried. Query task, message, or runtime state before retrying, including after an HTTP client timeout. Broadcast delivery may be partial if storage fails during fan-out.

## Verification

```bash
python -m pytest tests/test_board_management.py tests/test_board.py tests/test_spawn_cli.py -q
CLAWTEAM_HTTP_CLIENT=/path/to/ClawTeam-Docker/skills/clawteam/scripts/clawteam_cli.py \
  python -m pytest tests/test_board_management.py -q
```

Tests use temporary stores. The launch test runs a local Python process, without an LLM or a deployed team. The optional client test exercises the separately maintained HTTP skill against a real local Board server.

## API key configuration

```bash
export CLAWTEAM_BOARD_API_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
clawteam board serve --host 127.0.0.1 --port 8080
```

Clients send the same key as a Bearer token. Store it in the deployment secret configuration rather than source control. For example, with the key loaded into the client environment:

```bash
curl -H "Authorization: Bearer $CLAWTEAM_BOARD_API_KEY" \
  http://127.0.0.1:8080/openapi/capabilities
```
