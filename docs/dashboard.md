# Operational dashboard

The dashboard at `/dashboard` is a read-only operations console for the local AETHER runtime
(README §20). It shows real persistent state only. The single write it can trigger is an
approval or denial, which the browser sends to the token-guarded API endpoint.

## Design

- Dark mode is the default. A light theme follows `prefers-color-scheme` until the theme
  toggle (sidebar, or `t`) pins a choice. Only the theme choice is stored, in `localStorage`
  under `aether.theme`.
- One accent colour (teal) is used for links, focus and selection. Status is conveyed by
  semantic tones (ok, warn, error, info, neutral). Every tone and text colour meets WCAG AA
  contrast in both themes. Status is always written as text as well, never shown by colour
  alone.
- There are no gradients, glass effects, web fonts or third-party assets. The type stack is the
  system UI font with tabular numerals, and code and IDs use the system monospace font.
- Layout: a fixed sidebar with navigation, API health, the theme toggle and the shortcuts help.
  A top bar holds the page filter and the live-update indicator with pause and resume. Below
  900px the sidebar collapses into a top strip and tables scroll horizontally.

## Pages

| Page | Shows |
| --- | --- |
| Overview | Headline counts, agent heartbeats (stale after 120 s), active objectives with progress, the task-state mix and the 10 latest errors. |
| Objectives | Every objective with status, priority and task progress. |
| Objective | Description and criteria, an interactive task DAG, requirements, research findings, capabilities with verification evidence, security decisions, authorization requests and the final MUSE message. |
| Tasks | All tasks, filterable by state and agent. Selecting a row opens the task drawer. |
| Security | Pending approvals with Approve and Deny buttons, plus WARDEN's decisions. |
| Resources | CPU, RAM and disk sparklines with hover readouts, and recent measurements. |
| Providers | Today's (UTC) usage against provider and per-model daily budgets. Bars turn amber at 70% and red at 90%. |
| Events | The event log, newest first, filterable by event type. |

The task drawer opens from any row that names a task: tasks, overview agents, security
decisions, events, graph nodes and the outline. It shows the task's facts, prerequisites,
execution runs, verification evidence, security decisions and event timeline. The open task
is kept in the URL as `#task=<uuid>`, so the view can be linked or reloaded.

The task DAG is laid out in layers from left to right: prerequisites on the left, dependants
on the right. Hovering a node highlights its edges and neighbours. Clicking a node, or pressing
Enter on it, opens the drawer. The Graph/Outline switch shows the nested list instead, and that
list is also what the page renders without JavaScript.

## Keyboard

| Keys | Action |
| --- | --- |
| `/` | Filter rows on this page; `Esc` clears it |
| `?` | Show the shortcuts dialog |
| `g` then `h` `o` `t` `s` `r` `p` `e` | Go to Overview, Objectives, Tasks, Security, Resources, Providers, Events |
| `j` / `k` | Move the row cursor down or up |
| `Enter` | Open the task in the row under the cursor |
| `r` | Refresh now |
| `t` | Toggle the theme |
| `Esc` | Close the drawer |

Shortcuts are ignored while typing in a field or when a modifier key is held.

## Live updates

Each page renders completely on the server and works without JavaScript. With JavaScript
enabled, the page polls its JSON view every 10 seconds and redraws in place, which keeps the
sort order, the filter, the focused row and the scroll position. Polling behaves as follows:

- It pauses while the tab is hidden and refreshes immediately when the tab becomes visible
  again if the data is stale.
- A redraw is skipped when the data has not changed, and is deferred while you are typing,
  selecting text or waiting on an approval request.
- After a failed request it backs off exponentially (up to 2 minutes). An error banner offers
  "Retry now", and the last good data stays on screen.
- Ages ("12s ago") are computed against the server clock and tick every second. The absolute
  UTC time is shown on hover.

## JSON views

These are read-only and unauthenticated, like the API's own reads. They return the same
sanitized data as the HTML pages, plus `view`, `generated_at` and `stale_heartbeat_seconds`.
Responses are sent with `Cache-Control: no-store`.

| Path | View |
| --- | --- |
| `/dashboard/data/overview` | `overview` |
| `/dashboard/data/objectives` | `objectives` |
| `/dashboard/data/objectives/{id}` | `objective` |
| `/dashboard/data/tasks?state=&agent=` | `tasks` (also `states`, `filters`) |
| `/dashboard/data/tasks/{id}` | `task` (task with dependencies, runs, events; objective; tests; decisions) |
| `/dashboard/data/security` | `security` |
| `/dashboard/data/resources` | `resources` |
| `/dashboard/data/providers` | `providers` (also `thresholds`) |
| `/dashboard/data/events?type=` | `events` (also `filters`) |

## Security

- The content security policy is `script-src 'self'; style-src 'self'` with `default-src 'none'`.
  Pages contain no inline scripts, inline styles or event-handler attributes.
- The server escapes every value. In the browser, `Aether.h()` (in `core.js`) builds the DOM
  only with `createElement` and text nodes, so stored text is never parsed as markup. It also
  refuses `on*` attributes and any link that is not a same-origin path or fragment. Tests check
  that the scripts never use `innerHTML`, `eval` or similar APIs.
- The API token is typed into the Security page and kept in `sessionStorage` (`aether.apiToken`)
  for that tab only. It is sent only in the `Authorization` header of
  `POST /authorizations/{id}/respond`, and is never placed in URLs, logs or local storage.
  "Forget token" removes it.
- Approve and Deny appear only once the request has been delivered to a person. The API then
  checks the token, the audience and the request state, and the dashboard reports 401, 403 and
  409 responses in plain language.

## Assets and caching

Static files are served from an allow-list (`dashboard.css`, `theme.js`, `core.js`,
`dashboard.js`). They load from the same origin with no CDN and no build step. Pages reference
them as `/dashboard/static/<name>?v=<content hash>`:

- A matching version is served with `Cache-Control: public, max-age=31536000, immutable`.
- Any other request is served with `no-cache`.
- Every response carries an `ETag`, and conditional requests receive `304`.

HTML pages and JSON views are always `no-store`.

`theme.js` loads in the head so the saved theme applies before first paint. `core.js` contains
the pure helpers: DOM building, formatting, sorting and graph layout. `dashboard.js` holds the
page renderers and interactions.

## Tests

`tests/api/test_dashboard.py` covers the server-rendered pages. `tests/api/test_dashboard_ui.py`
covers the page chrome, asset caching, the JSON views, the task detail view and static script
checks. It also runs `core.js` in V8 to check DOM safety, graph layout and formatting parity
with the server. Those V8 tests run when `mini-racer` is installed:

```powershell
uv run --with mini-racer pytest -q tests/api/test_dashboard_ui.py
```
