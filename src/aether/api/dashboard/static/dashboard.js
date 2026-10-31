// Live, keyboard-driven dashboard. Pages are redrawn from the read-only /dashboard/data JSON
// views; every dynamic value reaches the DOM as a text node via Aether.h, never as markup.
(() => {
  "use strict";
  const A = window.Aether;
  if (!A) {
    return;
  }
  const { h, s, text, DASH } = A;
  const TOKEN_KEY = "aether.apiToken";
  const GO = {
    h: "/dashboard",
    o: "/dashboard/objectives",
    t: "/dashboard/tasks",
    s: "/dashboard/security",
    r: "/dashboard/resources",
    p: "/dashboard/providers",
    e: "/dashboard/events",
  };
  const ICONS = {
    copy: "M5.5 5.5h7v8h-7zM3.5 10.5v-8h7",
    retry: "M13 8a5 5 0 1 1-1.5-3.6M13 2.5v2.5h-2.5",
  };
  const NOSORT = new Set(["providers"]);
  const WAITING = ["waiting_for_approval", "waiting_for_verification"];

  const body = document.body;
  const content = document.getElementById("content");
  const live = document.getElementById("live");
  const liveText = document.getElementById("live-text");
  const liveToggle = document.getElementById("live-toggle");
  const apiStatus = document.getElementById("api-status");
  const search = document.getElementById("search");
  const drawer = document.getElementById("drawer");
  const drawerBody = document.getElementById("drawer-body");
  const drawerTitle = document.getElementById("drawer-title");
  const drawerClose = document.getElementById("drawer-close");
  const toasts = document.getElementById("toasts");
  const shortcuts = document.getElementById("shortcuts");
  const tokenInput = document.getElementById("api-token");
  const noteInput = document.getElementById("respond-note");
  const respondStatus = document.getElementById("respond-status");

  const state = {
    page: body.dataset.page || "",
    source: body.dataset.source || "",
    seconds: Math.max(2, Number(body.dataset.refreshSeconds) || 10),
    paused: false,
    timer: null,
    nextAt: 0,
    failures: 0,
    lastError: "",
    inFlight: false,
    again: false,
    updatedAt: null,
    offset: 0,
    snapshot: "",
    data: null,
    sort: {},
    filter: "",
    graphMode: "graph",
    drawerTask: null,
    drawerReturn: null,
    pendingKey: null,
    pendingTimer: null,
  };

  class HttpError extends Error {
    constructor(status) {
      super(`HTTP ${status}`);
      this.status = status;
    }
  }

  const now = () => Date.now() + state.offset;

  // ---------------------------------------------------------------- building blocks

  function icon(name) {
    return s(
      "svg",
      { class: "icon", viewBox: "0 0 16 16", "aria-hidden": "true", focusable: "false" },
      s("path", { d: ICONS[name] }),
    );
  }

  function badge(value) {
    return h("span", { class: `badge s-${A.token(value)}` }, text(value));
  }

  function when(value) {
    const ms = A.parseTime(value);
    if (ms === null) {
      return DASH;
    }
    return h(
      "time",
      { datetime: new Date(ms).toISOString(), title: A.absolute(ms) },
      A.relative((now() - ms) / 1000),
    );
  }

  function shortId(value) {
    if (!value) {
      return DASH;
    }
    const full = String(value);
    return h("code", { class: "id", title: full, "data-copy": full }, full.slice(0, 8));
  }

  function payload(value) {
    const isEmpty =
      !value || (typeof value === "object" && Object.keys(value).length === 0);
    if (isEmpty) {
      return DASH;
    }
    const json = A.pyJson(value).slice(0, 400);
    return h("code", { class: "payload", title: json }, json);
  }

  function code(value) {
    return h("code", null, text(value));
  }

  function muted(value) {
    return h("span", { class: "muted" }, value);
  }

  function empty(message, hint) {
    return h(
      "div",
      { class: "empty" },
      h("p", { class: "empty-title" }, message),
      hint ? h("p", { class: "empty-hint" }, hint) : null,
    );
  }

  function section(title, inner, options = {}) {
    const hasCount = options.count !== undefined && options.count !== null;
    return h(
      "section",
      { class: "panel", id: options.key || null },
      h(
        "div",
        { class: "panel-head" },
        h("h2", null, title),
        hasCount ? h("span", { class: "count" }, String(options.count)) : null,
        options.note ? h("span", { class: "panel-note" }, options.note) : null,
      ),
      inner,
    );
  }

  function pageHead(title, sub) {
    return h(
      "div",
      { class: "page-head" },
      h("h1", null, title),
      sub ? h("p", { class: "page-sub" }, sub) : null,
    );
  }

  // A <data value> child carries the machine-readable sort key for formatted numbers.
  function keyed(value, node) {
    return h("data", { value }, node);
  }

  function table(columns, rows, emptyMessage, options = {}) {
    if (!rows.length) {
      return empty(emptyMessage, options.hint);
    }
    const kinds = columns.map((column) => (Array.isArray(column) ? column[1] : null));
    const head = h(
      "tr",
      null,
      columns.map((column, index) =>
        h("th", { scope: "col", class: kinds[index] }, Array.isArray(column) ? column[0] : column),
      ),
    );
    const bodyRows = rows.map((row) => {
      const item = Array.isArray(row) ? { cells: row } : row;
      return h(
        "tr",
        item.attrs || null,
        item.cells.map((cell, index) => h("td", { class: kinds[index] }, cell)),
      );
    });
    return h(
      "div",
      { class: "table-wrap" },
      h(
        "table",
        { class: "data", "data-table": options.key || null },
        h("thead", null, head),
        h("tbody", null, bodyRows),
      ),
    );
  }

  function taskAttrs(taskId) {
    return taskId ? { "data-task-id": taskId } : null;
  }

  function bar(ratio, label, options = {}) {
    const width = Math.max(0, Math.min(ratio, 1)) * 100;
    const marks = options.marks
      ? [options.thresholds.warn, options.thresholds.critical].map((mark) =>
          s("line", { class: "bar-mark", x1: mark * 100, x2: mark * 100, y1: 0, y2: 8 }),
        )
      : null;
    return s(
      "svg",
      {
        class: `bar t-${options.kind || "progress"}`,
        viewBox: "0 0 100 8",
        preserveAspectRatio: "none",
        role: "img",
        "aria-label": label,
      },
      s("rect", { class: "bar-track", width: 100, height: 8 }),
      s("rect", { class: "bar-fill", width: width.toFixed(1), height: 8 }),
      marks,
    );
  }

  function progress(done, total) {
    const ratio = total ? done / total : 0;
    return h(
      "span",
      { class: "progress" },
      bar(ratio, `${done} of ${total} tasks completed`),
      h("span", { class: "num" }, `${done}/${total}`),
    );
  }

  function objectiveLink(objective) {
    return h(
      "a",
      { href: `/dashboard/objectives/${encodeURIComponent(objective.id)}` },
      text(objective.title),
    );
  }

  function stat(label, value, options = {}) {
    return h(
      "div",
      { class: options.state ? `stat s-${A.token(options.state)}` : "stat" },
      h("span", { class: "stat-label" }, label),
      h("span", { class: "stat-value" }, String(value)),
      options.hint ? h("span", { class: "stat-hint" }, options.hint) : null,
    );
  }

  // ---------------------------------------------------------------- page renderers

  function renderOverview(d) {
    const reference = A.parseTime(d.generated_at) || now();
    let healthy = 0;
    const agentRows = d.agents.map((agent) => {
      const beat = agent.latest_heartbeat;
      const seen = beat ? A.parseTime(beat.observed_at) : null;
      const seconds = seen === null ? null : Math.max(0, (reference - seen) / 1000);
      let status = beat ? agent.status : "no heartbeat";
      if (seconds !== null && seconds > d.stale_heartbeat_seconds) {
        status = "stale";
      } else if (beat && !["degraded", "stopping", "stopped"].includes(status)) {
        healthy += 1;
      }
      const current = beat ? beat.current_task_id : null;
      return {
        attrs: taskAttrs(current),
        cells: [
          h("strong", null, text(agent.agent_id)),
          badge(status),
          beat ? when(beat.observed_at) : muted("never"),
          shortId(current),
          text(agent.responsibility),
        ],
      };
    });
    const active = d.objectives.filter((item) => item.status === "active");
    const objectiveRows = active.map((item) => {
      const stats = item.progress;
      const pct = stats.completion_percent;
      return [
        objectiveLink(item),
        badge(item.status),
        progress(stats.completed_tasks, stats.total_tasks),
        pct === null || pct === undefined ? muted("no tasks") : `${Math.round(pct)}%`,
        text(item.priority),
      ];
    });
    const counts = d.task_counts;
    const errorRows = d.errors.map((item) => [
      when(item.occurred_at),
      text(item.agent),
      code(item.error_type),
      text(item.message),
      badge(item.resolved ? "resolved" : "open"),
    ]);
    const waiting = WAITING.reduce((sum, key) => sum + (counts[key] || 0), 0);
    const failing = (counts.failed || 0) + (counts.blocked || 0);
    const unresolved = d.errors.filter((item) => !item.resolved).length;
    const allHealthy = d.agents.length > 0 && healthy === d.agents.length;
    return [
      pageHead("Overview", "Agents, objectives and task flow from persistent state."),
      h(
        "div",
        { class: "stats" },
        stat("Agents healthy", `${healthy}/${d.agents.length}`, { state: allHealthy ? "ok" : "" }),
        stat("Active objectives", active.length),
        stat("Running tasks", counts.running || 0, { state: "running" }),
        stat("Waiting", waiting, { state: waiting ? "paused" : "", hint: "approval or verification" }),
        stat("Failed or blocked", failing, { state: failing ? "failed" : "" }),
        stat("Open errors", unresolved, { state: unresolved ? "open" : "", hint: "latest 10" }),
      ),
      section(
        "Agents",
        table(
          ["Agent", "Status", "Last heartbeat", "Current task", "Responsibility"],
          agentRows,
          "No agents are registered yet.",
          { hint: "Agents appear here once the runtime registers them.", key: "agents" },
        ),
        { count: agentRows.length, key: "agents" },
      ),
      section(
        "Active objectives",
        table(
          ["Objective", "Status", "Progress", ["Done", "num"], ["Priority", "num"]],
          objectiveRows,
          "No active objectives.",
          {
            hint: "Objectives submitted to the API show here while they are active.",
            key: "active-objectives",
          },
        ),
        { count: objectiveRows.length, key: "active-objectives" },
      ),
      section("Tasks by state", taskMix(counts), {
        count: Object.values(counts).reduce((sum, value) => sum + value, 0),
        key: "task-mix",
      }),
      section(
        "Recent errors",
        table(["When", "Agent", "Type", "Message", "State"], errorRows, "No errors have been recorded.", {
          hint: "Failures and their recovery attempts are listed here.",
          key: "errors",
        }),
        { key: "errors" },
      ),
    ];
  }

  function taskMix(counts) {
    const entries = Object.entries(counts);
    const total = entries.reduce((sum, [, value]) => sum + value, 0);
    let offset = 0;
    const segments = [];
    for (const [state, value] of entries) {
      if (value) {
        const width = (100 * value) / total;
        segments.push(
          s(
            "rect",
            { class: `seg s-${A.token(state)}`, x: offset.toFixed(2), width: width.toFixed(2), height: 8 },
            s("title", null, `${state}: ${value}`),
          ),
        );
        offset += width;
      }
    }
    return [
      total
        ? s(
            "svg",
            {
              class: "mix",
              viewBox: "0 0 100 8",
              preserveAspectRatio: "none",
              role: "img",
              "aria-label": `${total} tasks by state`,
            },
            segments,
          )
        : null,
      h(
        "ul",
        { class: "counts" },
        entries.map(([state, value]) =>
          h(
            "li",
            { class: value ? null : "zero" },
            badge(state),
            " ",
            h("strong", { class: "num" }, String(value)),
          ),
        ),
      ),
    ];
  }

  function renderObjectives(d) {
    const rows = d.items.map((item) => [
      objectiveLink(item),
      badge(item.status),
      text(item.priority),
      progress(item.progress.completed_tasks, item.progress.total_tasks),
      when(item.created_at),
      shortId(item.id),
    ]);
    return [
      pageHead("Objectives"),
      section(
        "All objectives",
        table(
          ["Objective", "Status", ["Priority", "num"], "Progress", "Created", "ID"],
          rows,
          "No objectives have been submitted yet.",
          { hint: "Submit one with POST /objectives using the API token.", key: "objectives" },
        ),
        { count: d.total, key: "objectives" },
      ),
    ];
  }

  function outline(tasks, dependencies) {
    const byId = new Map(tasks.map((task) => [task.id, task]));
    const children = new Map();
    const hasParent = new Set();
    for (const edge of dependencies) {
      const parent = edge.depends_on_task_id;
      const child = edge.task_id;
      if (byId.has(parent) && byId.has(child)) {
        if (!children.has(parent)) {
          children.set(parent, []);
        }
        children.get(parent).push(child);
        hasParent.add(child);
      }
    }
    const shown = new Set();
    const node = (id) => {
      const task = byId.get(id);
      const label = [
        badge(task.state),
        " ",
        h("strong", null, text(task.title)),
        " ",
        muted(`${text(task.assigned_agent)} \u00b7 priority ${text(task.priority)}`),
      ];
      if (shown.has(id)) {
        return h("li", null, label, " ", muted("(listed above)"));
      }
      shown.add(id);
      const nested = (children.get(id) || []).map(node);
      return h("li", taskAttrs(id), label, nested.length ? h("ul", null, nested) : null);
    };
    const roots = tasks.filter((task) => !hasParent.has(task.id)).map((task) => node(task.id));
    const rest = tasks.filter((task) => !shown.has(task.id)).map((task) => node(task.id));
    return [...roots, ...rest];
  }

  function renderObjective(d) {
    const item = d.objective;
    const stats = item.progress;
    const criteria = item.completion_criteria || [];
    const graph = d.tasks.length
      ? [h("div", { id: "task-graph", class: "graph-host" }), h("ul", { class: "dag" }, outline(d.tasks, d.dependencies))]
      : empty("This objective has no tasks.", "ORCHESTR adds tasks as it plans the work.");
    const capabilityRows = d.capabilities.map((row) => {
      const evidence = row.verification_evidence.map((run, index) => [
        index ? h("br") : null,
        `${text(run.suite)}: `,
        badge(run.verdict || run.status),
        ` ${run.passed} passed, ${run.failed} failed, ${run.errors} errors`,
      ]);
      return [text(row.name), badge(row.lifecycle_status), badge(row.verification_status), evidence.length ? evidence : DASH];
    });
    const final = d.final_message;
    return [
      h(
        "nav",
        { class: "crumbs", "aria-label": "Breadcrumb" },
        h("a", { href: "/dashboard/objectives" }, "Objectives"),
        h("span", { "aria-hidden": "true" }, "/"),
        shortId(item.id),
      ),
      h(
        "div",
        { class: "page-head" },
        h("h1", null, text(item.title)),
        h(
          "p",
          { class: "meta" },
          badge(item.status),
          h("span", null, "priority ", h("strong", { class: "num" }, text(item.priority))),
          progress(stats.completed_tasks, stats.total_tasks),
          h("span", null, "created ", when(item.created_at)),
        ),
      ),
      h("p", { class: "lead" }, text(item.description)),
      criteria.length
        ? h(
            "div",
            { class: "criteria" },
            h("h3", null, "Completion criteria"),
            h("ul", null, criteria.map((entry) => h("li", null, text(entry)))),
          )
        : null,
      section("Task graph", graph, { count: d.tasks.length, key: "graph" }),
      section(
        "Requirements",
        table(
          ["Requirement", "Type", "Status", ["Priority", "num"], "Acceptance criteria"],
          d.requirements.map((row) => [
            text(row.title),
            text(row.requirement_type),
            badge(row.status),
            text(row.priority),
            row.acceptance_criteria.length
              ? row.acceptance_criteria.map((entry, index) => [index ? h("br") : null, text(entry)])
              : DASH,
          ]),
          "No requirements recorded for this objective.",
          { key: "requirements" },
        ),
        { count: d.requirements.length },
      ),
      section(
        "Research findings",
        table(
          ["Agent", "Question", "Findings", "Class", ["Confidence", "num"], "Status"],
          d.findings.map((row) => [
            text(row.agent),
            text(row.question),
            text(row.findings),
            badge(row.classification),
            A.number(row.confidence).toFixed(2),
            badge(row.status),
          ]),
          "No research findings recorded for this objective.",
          { key: "findings" },
        ),
        { count: d.findings.length },
      ),
      section(
        "Capabilities and verification",
        table(
          ["Capability", "Lifecycle", "Verification", "Evidence"],
          capabilityRows,
          "No capabilities are linked to this objective.",
          { key: "capabilities" },
        ),
        { count: d.capabilities.length },
      ),
      section(
        "Security decisions",
        table(
          ["When", "Decision", "Operation", "Rationale", "Expires"],
          d.decisions.map((row) => ({
            attrs: taskAttrs(row.task_id),
            cells: [when(row.created_at), badge(row.decision), code(row.operation), text(row.rationale), when(row.expires_at)],
          })),
          "No security decisions for this objective's tasks.",
          { key: "objective-decisions" },
        ),
        { count: d.decisions.length },
      ),
      section(
        "Authorization requests",
        table(
          ["Requested", "Question", "Status", "Responded"],
          d.authorizations.map((row) => [
            when(row.requested_at),
            text(row.question),
            badge(row.status),
            when(row.responded_at),
          ]),
          "No authorization requests for this objective.",
          { key: "authorizations" },
        ),
        { count: d.authorizations.length },
      ),
      section(
        "Final MUSE message",
        final
          ? [
              h("p", { class: "muted" }, when(final.occurred_at), ` via ${text(final.channel)}`),
              h("pre", { class: "message" }, text(final.summary)),
            ]
          : empty("MUSE has not sent a message for this objective yet."),
        { key: "final" },
      ),
    ];
  }

  function filterForm(id, fields) {
    return h(
      "form",
      { method: "get", class: "filters", id },
      fields,
      h("button", { type: "submit" }, "Apply"),
    );
  }

  function renderTasks(d) {
    const selected = d.filters.state || "";
    const form = filterForm("task-filters", [
      h(
        "label",
        null,
        "State ",
        h(
          "select",
          { name: "state" },
          h("option", { value: "" }, "all"),
          d.states.map((item) => h("option", { value: item, selected: item === selected }, item)),
        ),
      ),
      h(
        "label",
        null,
        "Agent ",
        h("input", { name: "agent", maxlength: 24, value: d.filters.agent || "", autocomplete: "off" }),
      ),
    ]);
    const rows = d.items.map((item) => ({
      attrs: taskAttrs(item.id),
      cells: [
        h("a", { href: `/dashboard/objectives/${encodeURIComponent(item.objective_id)}` }, text(item.title)),
        badge(item.state),
        text(item.assigned_agent),
        text(item.priority),
        `${text(item.attempt)}/${text(item.max_attempts)}`,
        item.last_error ? h("span", { class: "error-text" }, item.last_error) : DASH,
        when(item.updated_at),
        shortId(item.id),
      ],
    }));
    return [
      pageHead("Tasks", "Select a row to see its runs, evidence and events."),
      form,
      section(
        "Tasks",
        table(
          ["Task", "State", "Agent", ["Priority", "num"], ["Attempts", "num"], "Last error", "Updated", "ID"],
          rows,
          "No tasks match.",
          { hint: "Clear the filters or wait for ORCHESTR to plan work.", key: "tasks" },
        ),
        { count: d.total, key: "tasks" },
      ),
    ];
  }

  function renderSecurity(d) {
    const pendingRows = d.pending.items.map((item) => {
      const person = item.audience_person_id;
      const attrs = (decision, extra) => ({
        type: "button",
        class: extra,
        "data-respond": decision,
        "data-request-id": item.id,
        "data-person-id": person,
      });
      const actions = person
        ? h(
            "span",
            { class: "actions" },
            h("button", attrs("approved", "approve"), "Approve"),
            h("button", attrs("denied", "deny"), "Deny"),
          )
        : muted("Not delivered to a person yet");
      return [when(item.requested_at), text(item.question), when(item.expires_at), actions];
    });
    const decisionRows = d.decisions.items.map((item) => ({
      attrs: taskAttrs(item.task_id),
      cells: [
        when(item.created_at),
        badge(item.decision),
        code(item.operation),
        text(item.rationale),
        payload(item.scope),
        when(item.expires_at),
      ],
    }));
    return [
      pageHead("Security", "Approvals waiting on a person, and WARDEN's recorded decisions."),
      section(
        "Pending approvals",
        table(["Requested", "Question", "Expires", "Respond"], pendingRows, "Nothing is waiting for approval.", {
          key: "pending",
        }),
        { count: pendingRows.length, key: "pending" },
      ),
      section(
        "Security decisions",
        table(
          ["When", "Decision", "Operation", "Rationale", "Scope", "Expires"],
          decisionRows,
          "No security decisions have been recorded.",
          { key: "decisions" },
        ),
        { count: d.decisions.total, key: "decisions" },
      ),
    ];
  }

  const METRICS = [
    { key: "cpu", name: "CPU", field: "cpu_percent", low: 0, high: 100, format: (v) => `${A.number(v).toFixed(1)}%` },
    { key: "ram", name: "Available RAM", field: "available_ram_bytes", format: A.humanBytes },
    { key: "disk", name: "Free disk", field: "free_disk_bytes", format: A.humanBytes },
  ];

  function sparkline(values, low, high) {
    if (!values.length) {
      return empty("No measurements.");
    }
    const width = 300;
    const height = 56;
    const points = A.sparkPoints(values, low, high, width, height);
    const line = points.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
    const area = `M0,${height} L${line.split(" ").join(" L")} L${width},${height} Z`;
    return s(
      "svg",
      {
        class: "spark",
        viewBox: `0 0 ${width} ${height}`,
        preserveAspectRatio: "none",
        role: "img",
        "aria-label": "trend",
      },
      s("path", { class: "spark-area", d: area }),
      s("polyline", { class: "spark-line", points: line }),
    );
  }

  function renderResources(d) {
    const items = d.items;
    if (!items.length) {
      return [
        pageHead("Resources"),
        section(
          "Host resources",
          empty(
            "No resource measurements have been recorded yet.",
            "The runtime samples CPU, RAM and disk while agents run.",
          ),
        ),
      ];
    }
    const latest = items[0];
    const chronological = items.slice().reverse();
    const cards = METRICS.map((metric) => {
      const values = chronological.map((item) => A.number(item[metric.field]));
      return h(
        "div",
        { class: "metric", "data-metric": metric.key },
        h(
          "div",
          { class: "metric-head" },
          h("h3", null, metric.name),
          h("span", { class: "metric-value" }, metric.format(latest[metric.field])),
        ),
        sparkline(values, metric.low, metric.high),
        h(
          "div",
          { class: "metric-range" },
          h("span", null, `min ${metric.format(Math.min(...values))}`),
          h("span", null, `max ${metric.format(Math.max(...values))}`),
        ),
      );
    });
    const rows = items.slice(0, 20).map((item) => [
      when(item.observed_at),
      keyed(A.number(item.cpu_percent), METRICS[0].format(item.cpu_percent)),
      keyed(A.number(item.available_ram_bytes), A.humanBytes(item.available_ram_bytes)),
      keyed(A.number(item.free_disk_bytes), A.humanBytes(item.free_disk_bytes)),
    ]);
    return [
      pageHead("Resources", [
        "Latest measurement ",
        when(latest.observed_at),
        `; trend covers the last ${items.length} of ${d.total} measurements.`,
      ]),
      h("div", { class: "metrics" }, cards),
      section(
        "Recent measurements",
        table(["Observed", ["CPU", "num"], ["Available RAM", "num"], ["Free disk", "num"]], rows, "", {
          key: "measurements",
        }),
        { key: "measurements" },
      ),
    ];
  }

  function budget(used, limit, options) {
    const usedText = options.money ? `$${A.number(used).toFixed(4)}` : A.count(used);
    if (limit === null || limit === undefined) {
      return [h("span", { class: "num" }, usedText), " ", muted("no limit")];
    }
    const limitText = options.money ? `$${A.number(limit).toFixed(2)}` : A.count(limit);
    const ratio = A.number(limit) > 0 ? A.number(used) / A.number(limit) : 1;
    const level = A.tone(ratio, options.thresholds);
    return h(
      "span",
      { class: "budget" },
      bar(ratio, `${A.percent(ratio)} of budget`, { kind: level, marks: true, thresholds: options.thresholds }),
      h("span", { class: "num" }, `${usedText} / ${limitText}`),
      h("span", { class: `pct t-${level}` }, A.percent(ratio)),
    );
  }

  function renderProviders(d) {
    const thresholds = d.thresholds || { warn: 0.7, critical: 0.9 };
    const rows = [];
    const usageCells = (usage, limits) => [
      budget(usage.requests, limits.max_requests_per_day, { thresholds }),
      budget(usage.tokens, limits.max_tokens_per_day, { thresholds }),
      budget(usage.cost_usd, limits.max_cost_usd_per_day, { thresholds, money: true }),
      text(usage.active),
    ];
    for (const item of d.items) {
      rows.push([h("strong", null, text(item.provider)), ...usageCells(item.today, item.budget || {})]);
      for (const model of item.model_budgets) {
        rows.push({
          attrs: { class: "sub" },
          cells: [
            h("span", { class: "sub-row" }, `${text(item.provider)}/${text(model.model)}`),
            ...usageCells(model.today, model.budget || {}),
          ],
        });
      }
    }
    return [
      pageHead(
        "Providers",
        `Bars turn amber at ${A.percent(thresholds.warn)} and red at ${A.percent(thresholds.critical)} of a daily budget.`,
      ),
      section(
        "Today's usage vs budgets (UTC day)",
        table(
          ["Provider", "Requests", "Tokens", "Cost", ["In flight", "num"]],
          rows,
          "No provider usage or budgets are configured.",
          { key: "providers" },
        ),
        { key: "providers", note: d.items.length ? d.items[0].date : "" },
      ),
    ];
  }

  function renderEvents(d) {
    const form = filterForm("event-filters", [
      h(
        "label",
        null,
        "Event type ",
        h("input", { name: "type", maxlength: 64, value: d.filters.type || "", autocomplete: "off" }),
      ),
    ]);
    const rows = d.items.map((item) => ({
      attrs: taskAttrs(item.task_id),
      cells: [
        when(item.created_at),
        code(item.event_type),
        text(item.category),
        text(item.actor),
        [text(item.aggregate_type), " ", shortId(item.aggregate_id)],
        payload(item.payload),
      ],
    }));
    return [
      pageHead("Events", "The append-only event log, newest first."),
      form,
      section(
        "Latest events",
        table(["When", "Type", "Category", "Actor", "Subject", "Payload"], rows, "No events match.", {
          key: "events",
        }),
        { count: d.total, key: "events" },
      ),
    ];
  }

  const RENDERERS = {
    overview: renderOverview,
    objectives: renderObjectives,
    objective: renderObjective,
    tasks: renderTasks,
    security: renderSecurity,
    resources: renderResources,
    providers: renderProviders,
    events: renderEvents,
  };

  // ---------------------------------------------------------------- enhancement

  function enhance(root) {
    enhanceTables(root);
    enhanceCopy(root);
    if (root === content) {
      enhanceGraph();
      enhanceMetrics();
      applyFilter();
    }
    updateTimes(root);
  }

  function enhanceTables(root) {
    root.querySelectorAll("table.data").forEach((tableNode) => {
      const key = tableNode.dataset.table || "";
      Array.from(tableNode.tBodies[0].rows).forEach((row, index) => {
        row.dataset.index = String(index);
        if (row.dataset.taskId) {
          row.tabIndex = 0;
        }
      });
      if (root !== content || NOSORT.has(key) || tableNode.tBodies[0].rows.length < 2) {
        return;
      }
      Array.from(tableNode.tHead.rows[0].cells).forEach((th, column) => {
        if (th.querySelector("button.sort")) {
          return;
        }
        const label = th.textContent;
        const button = h("button", { type: "button", class: "sort", "data-column": column }, label);
        button.setAttribute("aria-label", `Sort by ${label}`);
        th.replaceChildren(button);
      });
      const order = state.sort[key];
      if (order) {
        sortTable(tableNode, order.column, order.direction);
      }
    });
  }

  function sortTable(tableNode, column, direction) {
    const tbody = tableNode.tBodies[0];
    const rows = Array.from(tbody.rows).filter((row) => !row.classList.contains("no-match"));
    const keyed = rows.map((row) => ({
      row,
      index: Number(row.dataset.index),
      key: A.sortKey(row.cells[column]),
    }));
    const sign = direction === "descending" ? -1 : 1;
    keyed.sort((a, b) => {
      if (direction === "none") {
        return a.index - b.index;
      }
      const blankA = A.isBlank(a.key);
      const blankB = A.isBlank(b.key);
      if (blankA !== blankB) {
        return blankA ? 1 : -1;
      }
      return sign * A.compareKeys(a.key, b.key) || a.index - b.index;
    });
    keyed.forEach((item) => tbody.appendChild(item.row));
    Array.from(tableNode.tHead.rows[0].cells).forEach((th, index) => {
      if (index === column && direction !== "none") {
        th.setAttribute("aria-sort", direction);
      } else {
        th.removeAttribute("aria-sort");
      }
    });
  }

  function cycleSort(button) {
    const tableNode = button.closest("table");
    const key = tableNode.dataset.table || "";
    const column = Number(button.dataset.column);
    const current = state.sort[key];
    let direction = "ascending";
    if (current && current.column === column) {
      direction = { ascending: "descending", descending: "none", none: "ascending" }[current.direction];
    }
    state.sort[key] = { column, direction };
    sortTable(tableNode, column, direction);
  }

  function enhanceCopy(root) {
    root.querySelectorAll("code[data-copy]").forEach((node) => {
      const next = node.nextElementSibling;
      if (next && next.classList.contains("copy")) {
        return;
      }
      const value = node.dataset.copy;
      const button = h(
        "button",
        { type: "button", class: "copy", "data-copy-value": value, title: "Copy full ID" },
        icon("copy"),
      );
      button.setAttribute("aria-label", `Copy ID ${value}`);
      node.after(button);
    });
  }

  function updateTimes(root) {
    const reference = now();
    root.querySelectorAll("time[datetime]").forEach((node) => {
      const ms = A.parseTime(node.getAttribute("datetime"));
      if (ms !== null) {
        node.textContent = A.relative((reference - ms) / 1000);
      }
    });
  }

  function applyFilter() {
    const query = state.filter.trim();
    content.querySelectorAll("table.data").forEach((tableNode) => {
      const tbody = tableNode.tBodies[0];
      let shown = 0;
      Array.from(tbody.rows).forEach((row) => {
        if (row.classList.contains("no-match")) {
          return;
        }
        const visible = !query || A.matches(row.textContent, query);
        row.hidden = !visible;
        shown += visible ? 1 : 0;
      });
      let note = tbody.querySelector("tr.no-match");
      if (query && !shown) {
        const message = `No rows match \u201c${query}\u201d.`;
        if (note) {
          note.cells[0].textContent = message;
        } else {
          note = h("tr", { class: "no-match" }, h("td", { colspan: tableNode.tHead.rows[0].cells.length }, message));
          tbody.appendChild(note);
        }
      } else if (note) {
        note.remove();
      }
    });
    content.querySelectorAll("g.node").forEach((node) => {
      node.classList.toggle("dim", Boolean(query) && !A.matches(node.dataset.search || "", query));
    });
  }

  // ---------------------------------------------------------------- task graph

  function enhanceGraph() {
    const host = content.querySelector("#task-graph");
    const d = state.data;
    if (!host || !d || d.view !== "objective" || !d.tasks.length) {
      return;
    }
    const panel = host.closest(".panel");
    const layout = A.layoutGraph(d.tasks, d.dependencies);
    const byId = new Map(d.tasks.map((task) => [task.id, task]));
    const nodes = layout.nodes.map((place) => {
      const task = byId.get(place.id);
      const summary = `${text(task.state)} \u00b7 ${text(task.assigned_agent)} \u00b7 p${text(task.priority)}`;
      const group = s(
        "g",
        {
          class: `node s-${A.token(task.state)}${task.id === state.drawerTask ? " selected" : ""}`,
          transform: `translate(${place.x},${place.y})`,
          tabindex: 0,
          role: "button",
          "data-task-id": task.id,
          "data-search": `${task.title} ${task.state} ${task.assigned_agent}`,
          "aria-label": `${task.title}, ${task.state}, ${text(task.assigned_agent)}. Open details.`,
        },
        s("title", null, `${task.title}\n${summary}`),
        s("rect", { class: "node-box", width: layout.nodeWidth, height: layout.nodeHeight, rx: 3 }),
        s("rect", { class: "node-stripe", width: 3, height: layout.nodeHeight }),
        s("text", { class: "node-title", x: 12, y: 20 }, A.truncate(text(task.title), 30)),
        s("text", { class: "node-sub", x: 12, y: 37 }, A.truncate(summary, 36)),
      );
      return group;
    });
    const edges = layout.edges.map((edge) =>
      s("path", { class: "edge", d: edge.d, "marker-end": "url(#graph-arrow)", "data-from": edge.from, "data-to": edge.to }),
    );
    const svg = s(
      "svg",
      {
        class: "graph",
        width: layout.width,
        height: layout.height,
        viewBox: `0 0 ${layout.width} ${layout.height}`,
        role: "group",
        "aria-label": `Task dependency graph, ${d.tasks.length} tasks in ${layout.layers} stages`,
      },
      s(
        "defs",
        null,
        s(
          "marker",
          { id: "graph-arrow", viewBox: "0 0 8 8", refX: 7, refY: 4, markerWidth: 7, markerHeight: 7, orient: "auto" },
          s("path", { class: "arrow", d: "M0,0 L8,4 L0,8 z" }),
        ),
      ),
      s("g", null, edges),
      s("g", null, nodes),
    );
    host.replaceChildren(svg);
    panel.classList.add("has-graph");
    panel.classList.toggle("show-outline", state.graphMode === "outline");
    const head = panel.querySelector(".panel-head");
    if (!head.querySelector(".panel-tools")) {
      const tools = h(
        "span",
        { class: "panel-tools", role: "group", "aria-label": "Task graph view" },
        h("button", { type: "button", "data-graph-mode": "graph" }, "Graph"),
        h("button", { type: "button", "data-graph-mode": "outline" }, "Outline"),
      );
      head.appendChild(tools);
    }
    head.querySelectorAll("[data-graph-mode]").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.graphMode === state.graphMode));
    });
  }

  function highlightGraph(taskId, on) {
    content.querySelectorAll(".edge").forEach((edge) => {
      const related = edge.dataset.from === taskId || edge.dataset.to === taskId;
      edge.classList.toggle("hot", on && related);
      if (on && related) {
        const other = edge.dataset.from === taskId ? edge.dataset.to : edge.dataset.from;
        const node = content.querySelector(`g.node[data-task-id="${CSS.escape(other)}"]`);
        if (node) {
          node.classList.add("hot");
        }
      }
    });
    if (!on) {
      content.querySelectorAll("g.node.hot").forEach((node) => node.classList.remove("hot"));
    }
  }

  function markSelected() {
    content.querySelectorAll("g.node").forEach((node) => {
      node.classList.toggle("selected", node.dataset.taskId === state.drawerTask);
    });
  }

  // ---------------------------------------------------------------- metrics

  function enhanceMetrics() {
    const d = state.data;
    if (!d || d.view !== "resources" || !d.items.length) {
      return;
    }
    const chronological = d.items.slice().reverse();
    content.querySelectorAll(".metric[data-metric]").forEach((card) => {
      const metric = METRICS.find((item) => item.key === card.dataset.metric);
      const svg = card.querySelector("svg.spark");
      if (!metric || !svg) {
        return;
      }
      let readout = card.querySelector(".metric-readout");
      if (!readout) {
        readout = h("div", { class: "metric-readout", "aria-live": "off" });
        card.appendChild(readout);
      }
      const show = (event) => {
        const box = svg.getBoundingClientRect();
        const ratio = Math.min(Math.max((event.clientX - box.left) / Math.max(box.width, 1), 0), 1);
        const index = Math.round(ratio * (chronological.length - 1));
        const item = chronological[index];
        const x = chronological.length > 1 ? (300 * index) / (chronological.length - 1) : 150;
        let guide = svg.querySelector(".spark-guide");
        if (!guide) {
          guide = s("line", { class: "spark-guide", y1: 0, y2: 56 });
          svg.appendChild(guide);
        }
        guide.setAttribute("x1", x);
        guide.setAttribute("x2", x);
        const ms = A.parseTime(item.observed_at);
        readout.textContent = `${metric.format(item[metric.field])} at ${ms === null ? DASH : A.absolute(ms)}`;
      };
      svg.addEventListener("pointermove", show);
      svg.addEventListener("pointerleave", () => {
        const guide = svg.querySelector(".spark-guide");
        if (guide) {
          guide.remove();
        }
        readout.textContent = "";
      });
    });
  }

  // ---------------------------------------------------------------- live data

  function describeFocus() {
    const active = document.activeElement;
    if (!active || !content.contains(active)) {
      return null;
    }
    const tableNode = active.closest("table.data");
    if (active.matches("tr[data-task-id]")) {
      return { selector: `table[data-table="${CSS.escape(tableNode.dataset.table || "")}"] tr[data-task-id="${CSS.escape(active.dataset.taskId)}"]` };
    }
    if (active.matches("g.node")) {
      return { selector: `g.node[data-task-id="${CSS.escape(active.dataset.taskId)}"]` };
    }
    if (active.matches("button.sort") && tableNode) {
      return { selector: `table[data-table="${CSS.escape(tableNode.dataset.table || "")}"] button.sort[data-column="${active.dataset.column}"]` };
    }
    if (active.matches("a[href]")) {
      return { selector: `a[href="${CSS.escape(active.getAttribute("href"))}"]` };
    }
    return null;
  }

  function isBusy() {
    const active = document.activeElement;
    if (active && content.contains(active) && active.matches("input, select, textarea")) {
      return true;
    }
    if (content.querySelector("button[data-respond]:disabled")) {
      return true;
    }
    const selection = window.getSelection ? window.getSelection() : null;
    return Boolean(
      selection && !selection.isCollapsed && selection.anchorNode && content.contains(selection.anchorNode),
    );
  }

  function render(data) {
    const renderer = RENDERERS[data.view];
    if (!renderer) {
      return;
    }
    const focus = describeFocus();
    const scrolls = new Map();
    content.querySelectorAll(".table-wrap, .graph-host").forEach((node, index) => {
      scrolls.set(index, [node.scrollLeft, node.scrollTop]);
    });
    const fragment = document.createDocumentFragment();
    const nodes = renderer(data);
    (Array.isArray(nodes) ? nodes : [nodes]).forEach((node) => {
      if (node) {
        fragment.appendChild(node);
      }
    });
    content.replaceChildren(fragment);
    enhance(content);
    content.querySelectorAll(".table-wrap, .graph-host").forEach((node, index) => {
      const saved = scrolls.get(index);
      if (saved) {
        [node.scrollLeft, node.scrollTop] = saved;
      }
    });
    if (focus) {
      const target = content.querySelector(focus.selector);
      if (target) {
        target.focus({ preventScroll: true });
      }
    }
  }

  async function loadData() {
    if (!state.source) {
      return;
    }
    const response = await fetch(state.source, {
      cache: "no-store",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) {
      throw new HttpError(response.status);
    }
    const data = await response.json();
    const generated = A.parseTime(data.generated_at);
    if (generated !== null) {
      state.offset = generated - Date.now();
    }
    state.updatedAt = Date.now();
    const snapshot = JSON.stringify(data, (key, value) => (key === "generated_at" ? undefined : value));
    state.data = data;
    if (snapshot === state.snapshot) {
      updateTimes(content);
      return;
    }
    if (isBusy()) {
      return;
    }
    state.snapshot = snapshot;
    render(data);
  }

  async function refreshHealth() {
    let label = "api: unreachable";
    let status = "down";
    try {
      const response = await fetch("/health", { cache: "no-store" });
      const result = await response.json();
      status = response.ok ? "ok" : "down";
      label = `api: ${result.status || status}`;
    } catch (error) {
      /* offline or server stopped: keep the unreachable label */
    }
    apiStatus.textContent = label;
    apiStatus.className = `badge s-${A.token(status)}`;
  }

  async function refresh() {
    if (state.inFlight) {
      state.again = true;
      return;
    }
    clearTimeout(state.timer);
    state.inFlight = true;
    try {
      const [result] = await Promise.allSettled([loadData(), refreshHealth(), refreshDrawer()]);
      if (result.status === "rejected") {
        throw result.reason;
      }
      state.failures = 0;
      setBanner(null);
      live.classList.remove("tick");
      void live.offsetWidth;
      live.classList.add("tick");
    } catch (error) {
      state.failures += 1;
      state.lastError = error instanceof HttpError ? error.message : "network error";
      setBanner(error);
    } finally {
      state.inFlight = false;
      schedule();
      updateLive();
      if (state.again) {
        state.again = false;
        refresh();
      }
    }
  }

  function schedule() {
    clearTimeout(state.timer);
    if (state.paused || document.hidden) {
      return;
    }
    const delay = state.failures ? Math.min(state.seconds * 2 ** state.failures, 120) : state.seconds;
    state.nextAt = Date.now() + delay * 1000;
    state.timer = setTimeout(refresh, delay * 1000);
  }

  function updateLive() {
    let mode = "live";
    let label;
    if (state.paused) {
      mode = "paused";
      label = "Paused";
    } else if (state.failures) {
      mode = "error";
      label = `Update failed \u00b7 retry in ${Math.max(0, Math.ceil((state.nextAt - Date.now()) / 1000))}s`;
    } else if (state.updatedAt === null) {
      mode = "idle";
      label = "Connecting\u2026";
    } else {
      label = `Updated ${A.relative((Date.now() - state.updatedAt) / 1000)}`;
    }
    live.dataset.state = mode;
    liveText.textContent = label;
    live.title = state.updatedAt ? `Last update ${A.absolute(state.updatedAt + state.offset)}` : "";
  }

  let banner = null;
  function setBanner(error) {
    if (!error) {
      if (banner) {
        banner.remove();
        banner = null;
      }
      return;
    }
    const missing = error instanceof HttpError && error.status === 404;
    const age = state.updatedAt ? `Showing data from ${A.relative((Date.now() - state.updatedAt) / 1000)}.` : "";
    const message = missing
      ? "This record no longer exists."
      : `Live updates failed (${state.lastError}). ${age}`.trim();
    const next = h(
      "div",
      { class: "banner error", role: "alert" },
      h("span", { class: "grow" }, message),
      h("button", { type: "button", "data-action": "retry" }, icon("retry"), "Retry now"),
    );
    if (banner) {
      banner.replaceWith(next);
    } else {
      content.before(next);
    }
    banner = next;
  }

  function setPaused(paused) {
    state.paused = paused;
    liveToggle.setAttribute("aria-pressed", String(paused));
    const label = paused ? "Resume live updates" : "Pause live updates";
    liveToggle.setAttribute("aria-label", label);
    liveToggle.title = label;
    if (paused) {
      clearTimeout(state.timer);
    } else {
      refresh();
    }
    updateLive();
  }

  // ---------------------------------------------------------------- task drawer

  function fact(label, value) {
    return [h("dt", null, label), h("dd", null, value)];
  }

  function skeleton() {
    return [h("div", { class: "skeleton" }), h("div", { class: "skeleton short" }), h("div", { class: "skeleton" })];
  }

  function taskTitle(id) {
    const d = state.data;
    const tasks = d && d.view === "objective" ? d.tasks : d && d.view === "tasks" ? d.items : [];
    const match = tasks.find((task) => task.id === id);
    return match ? match.title : null;
  }

  function renderDrawer(d) {
    const task = d.task;
    drawerTitle.textContent = text(task.title);
    const started = (run) => A.parseTime(run.started_at);
    const finished = (run) => A.parseTime(run.finished_at);
    const runRows = task.executions.items.map((run) => [
      when(run.started_at),
      code(run.operation),
      text(run.agent),
      badge(run.status),
      text(run.exit_code),
      started(run) !== null && finished(run) !== null
        ? A.duration((finished(run) - started(run)) / 1000)
        : DASH,
    ]);
    const testRows = d.tests.map((run) => [
      text(run.suite),
      badge(run.verdict || run.status),
      text(run.passed),
      text(run.failed),
      text(run.errors),
      when(run.started_at),
    ]);
    const decisionRows = d.decisions.map((row) => [
      when(row.created_at),
      badge(row.decision),
      code(row.operation),
      text(row.rationale),
    ]);
    const dependencies = task.dependencies.items.map((edge) => {
      const id = edge.depends_on_task_id;
      return h(
        "li",
        null,
        h("button", { type: "button", class: "link-button", "data-open-task": id }, text(taskTitle(id) || id.slice(0, 8))),
        " ",
        shortId(id),
      );
    });
    const events = task.events.items.map((event) =>
      h(
        "li",
        null,
        when(event.created_at),
        h(
          "div",
          { class: "what" },
          code(event.event_type),
          " ",
          muted(text(event.actor)),
          payload(event.payload) === DASH ? null : payload(event.payload),
        ),
      ),
    );
    drawerBody.replaceChildren(
      ...h(
        "div",
        null,
        h(
          "dl",
          { class: "facts" },
          fact("State", badge(task.state)),
          fact("Agent", text(task.assigned_agent)),
          fact("Priority", text(task.priority)),
          fact("Attempts", `${text(task.attempt)}/${text(task.max_attempts)}`),
          fact(
            "Objective",
            d.objective
              ? h("a", { href: `/dashboard/objectives/${encodeURIComponent(d.objective.id)}` }, text(d.objective.title))
              : DASH,
          ),
          fact("Lease", task.lease_owner ? [text(task.lease_owner), " until ", when(task.lease_expires_at)] : DASH),
          fact("Created", when(task.created_at)),
          fact("Updated", when(task.updated_at)),
          fact("Task ID", shortId(task.id)),
          task.last_error ? fact("Last error", h("span", { class: "error-text" }, task.last_error)) : null,
        ),
        task.description ? [h("h3", null, "Description"), h("p", { class: "lead" }, task.description)] : null,
        h("h3", null, `Depends on (${dependencies.length})`),
        dependencies.length ? h("ul", { class: "deps" }, dependencies) : empty("No prerequisites."),
        h("h3", null, `Runs (${task.executions.total})`),
        h(
          "div",
          { class: "panel" },
          table(["Started", "Operation", "Agent", "Status", ["Exit", "num"], ["Took", "num"]], runRows, "No execution runs yet."),
        ),
        h("h3", null, `Verification evidence (${d.tests.length})`),
        h(
          "div",
          { class: "panel" },
          table(
            ["Suite", "Verdict", ["Passed", "num"], ["Failed", "num"], ["Errors", "num"], "Started"],
            testRows,
            "No test runs recorded for this task.",
          ),
        ),
        h("h3", null, `Security decisions (${d.decisions.length})`),
        h(
          "div",
          { class: "panel" },
          table(["When", "Decision", "Operation", "Rationale"], decisionRows, "No security decisions for this task."),
        ),
        h("h3", null, `Events (${task.events.total})`),
        events.length ? h("ol", { class: "timeline" }, events) : empty("No events recorded for this task."),
      ).childNodes,
    );
    enhance(drawerBody);
  }

  async function loadTask(id, quiet) {
    const scroll = drawerBody.scrollTop;
    try {
      const response = await fetch(`/dashboard/data/tasks/${encodeURIComponent(id)}`, { cache: "no-store" });
      if (state.drawerTask !== id) {
        return;
      }
      if (response.status === 404 || response.status === 422) {
        drawerTitle.textContent = "Task not found";
        drawerBody.replaceChildren(empty("This task does not exist.", "It may have been removed after the page loaded."));
        return;
      }
      if (!response.ok) {
        throw new HttpError(response.status);
      }
      const data = await response.json();
      if (state.drawerTask === id) {
        renderDrawer(data);
        drawerBody.scrollTop = scroll;
      }
    } catch (error) {
      if (state.drawerTask === id && !quiet) {
        drawerBody.replaceChildren(
          empty("Task details could not be loaded.", error instanceof HttpError ? error.message : "The API is unreachable."),
          h("button", { type: "button", "data-open-task": id }, icon("retry"), "Try again"),
        );
      }
    }
  }

  function refreshDrawer() {
    return state.drawerTask ? loadTask(state.drawerTask, true) : Promise.resolve();
  }

  function openTask(id, opener) {
    if (!id) {
      return;
    }
    if (!state.drawerTask) {
      state.drawerReturn = opener || document.activeElement;
    }
    state.drawerTask = id;
    drawer.hidden = false;
    window.requestAnimationFrame(() => drawer.classList.add("open"));
    drawerTitle.textContent = taskTitle(id) || "Task";
    drawerBody.replaceChildren(...skeleton());
    window.history.replaceState(null, "", `#task=${encodeURIComponent(id)}`);
    markSelected();
    drawerClose.focus({ preventScroll: true });
    loadTask(id, false);
  }

  function closeDrawer() {
    if (!state.drawerTask) {
      return;
    }
    state.drawerTask = null;
    drawer.classList.remove("open");
    drawer.hidden = true;
    window.history.replaceState(null, "", window.location.href.split("#")[0]);
    markSelected();
    const back = state.drawerReturn;
    state.drawerReturn = null;
    if (back && document.contains(back)) {
      back.focus({ preventScroll: true });
    }
  }

  // ---------------------------------------------------------------- actions

  function toast(message, tone) {
    const node = h("div", { class: `toast t-${tone || "info"}` }, message);
    toasts.appendChild(node);
    while (toasts.children.length > 4) {
      toasts.firstElementChild.remove();
    }
    window.setTimeout(() => node.remove(), tone === "error" ? 7000 : 3500);
  }

  async function copy(value) {
    try {
      await navigator.clipboard.writeText(value);
      toast(`Copied ${value}`, "ok");
    } catch (error) {
      // Plain-HTTP origins have no async clipboard; fall back to a selection copy.
      const area = h("textarea", { class: "visually-hidden", readonly: true });
      area.value = value;
      document.body.appendChild(area);
      area.select();
      const copied = document.execCommand && document.execCommand("copy");
      area.remove();
      toast(copied ? `Copied ${value}` : "Copy failed; select the ID and copy it manually.", copied ? "ok" : "error");
    }
  }

  function setRespondStatus(message) {
    if (respondStatus) {
      respondStatus.textContent = message;
    }
  }

  async function respond(button) {
    const token = sessionStorage.getItem(TOKEN_KEY);
    if (!token) {
      setRespondStatus("Enter the API token above before responding.");
      toast("Enter the API token before responding.", "warn");
      if (tokenInput) {
        tokenInput.focus();
      }
      return;
    }
    const decision = button.dataset.respond;
    const note = (noteInput && noteInput.value.trim()) || `${decision} via dashboard`;
    const body = { decision, text: note };
    if (button.dataset.personId) {
      body.person_id = button.dataset.personId;
    }
    const group = button.closest(".actions") || button;
    group.querySelectorAll("button").forEach((item) => {
      item.disabled = true;
    });
    let message;
    let tone = "error";
    try {
      const response = await fetch(`/authorizations/${encodeURIComponent(button.dataset.requestId)}/respond`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify(body),
      });
      if (response.ok) {
        message = `Response "${decision}" queued.`;
        tone = "ok";
      } else if (response.status === 401) {
        message = "The API token was rejected.";
      } else if (response.status === 409) {
        message = "This request is no longer pending.";
      } else if (response.status === 403) {
        message = "This response is not permitted for the delivered audience.";
      } else {
        message = `Request failed (HTTP ${response.status}).`;
      }
    } catch (error) {
      message = "The API is unreachable.";
    } finally {
      group.querySelectorAll("button").forEach((item) => {
        item.disabled = false;
      });
    }
    setRespondStatus(message);
    toast(message, tone);
    if (tone === "ok") {
      state.snapshot = "";
      refresh();
    }
  }

  function submitFilters(form) {
    const params = new URLSearchParams();
    new FormData(form).forEach((value, key) => {
      const trimmed = String(value).trim();
      if (trimmed) {
        params.set(key, trimmed);
      }
    });
    const query = params.toString();
    const suffix = query ? `?${query}` : "";
    window.history.replaceState(null, "", `${window.location.pathname}${suffix}`);
    state.source = `/dashboard/data/${state.page}${suffix}`;
    state.snapshot = "";
    if (document.activeElement && form.contains(document.activeElement)) {
      document.activeElement.blur();
    }
    refresh();
  }

  function moveCursor(step) {
    const rows = Array.from(
      content.querySelectorAll("table.data tbody tr:not([hidden]):not(.no-match)"),
    );
    if (!rows.length) {
      return;
    }
    const current = rows.indexOf(document.activeElement.closest ? document.activeElement.closest("tr") : null);
    const next = rows[Math.min(Math.max(current + step, 0), rows.length - 1)] || rows[0];
    rows.forEach((row) => row.classList.toggle("cursor", row === next));
    if (next.tabIndex < 0) {
      next.tabIndex = -1;
    }
    next.focus({ preventScroll: true });
    next.scrollIntoView({ block: "nearest", behavior: "auto" });
  }

  function openShortcuts() {
    if (shortcuts && typeof shortcuts.showModal === "function" && !shortcuts.open) {
      shortcuts.showModal();
    }
  }

  // ---------------------------------------------------------------- events

  document.addEventListener("click", (event) => {
    const target = event.target;
    const copyButton = target.closest("button.copy");
    if (copyButton) {
      copy(copyButton.dataset.copyValue);
      return;
    }
    const respondButton = target.closest("button[data-respond]");
    if (respondButton) {
      respond(respondButton);
      return;
    }
    const sortButton = target.closest("button.sort");
    if (sortButton) {
      cycleSort(sortButton);
      return;
    }
    const modeButton = target.closest("[data-graph-mode]");
    if (modeButton) {
      state.graphMode = modeButton.dataset.graphMode;
      enhanceGraph();
      return;
    }
    if (target.closest("[data-action='retry']")) {
      refresh();
      return;
    }
    const opener = target.closest("[data-open-task]");
    if (opener) {
      openTask(opener.dataset.openTask, opener);
      return;
    }
    if (target.closest("[data-close-dialog]")) {
      shortcuts.close();
      return;
    }
    if (target === shortcuts) {
      shortcuts.close();
      return;
    }
    if (!content.contains(target) || target.closest("a, button, input, select, label, code")) {
      return;
    }
    const selection = window.getSelection ? window.getSelection() : null;
    if (selection && !selection.isCollapsed) {
      return;
    }
    const row = target.closest("tr[data-task-id], g.node, .dag li[data-task-id]");
    if (row) {
      openTask(row.dataset.taskId, row);
    }
  });

  content.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") {
      return;
    }
    const row = event.target.closest && event.target.closest("tr[data-task-id], g.node");
    if (row && event.target === row) {
      event.preventDefault();
      openTask(row.dataset.taskId, row);
    }
  });

  content.addEventListener("pointerover", (event) => {
    const node = event.target.closest && event.target.closest("g.node");
    if (node) {
      highlightGraph(node.dataset.taskId, true);
    }
  });
  content.addEventListener("pointerout", (event) => {
    const node = event.target.closest && event.target.closest("g.node");
    if (node && !node.contains(event.relatedTarget)) {
      highlightGraph(node.dataset.taskId, false);
    }
  });

  content.addEventListener("submit", (event) => {
    const form = event.target;
    if (form.matches("#task-filters, #event-filters")) {
      event.preventDefault();
      submitFilters(form);
    }
  });
  content.addEventListener("change", (event) => {
    const form = event.target.closest("#task-filters");
    if (form && event.target.matches("select")) {
      submitFilters(form);
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) {
      return;
    }
    const target = event.target;
    const typing = target.closest && target.closest("input, textarea, select, [contenteditable='true']");
    if (event.key === "Escape") {
      if (target === search) {
        search.value = "";
        state.filter = "";
        applyFilter();
        search.blur();
      } else if (shortcuts && shortcuts.open) {
        return;
      } else if (state.drawerTask) {
        closeDrawer();
      }
      return;
    }
    if (typing) {
      return;
    }
    if (state.pendingKey === "g") {
      state.pendingKey = null;
      const destination = GO[event.key];
      if (destination) {
        event.preventDefault();
        window.location.assign(destination);
      }
      return;
    }
    switch (event.key) {
      case "/":
        event.preventDefault();
        search.focus();
        search.select();
        break;
      case "?":
        event.preventDefault();
        openShortcuts();
        break;
      case "g":
        state.pendingKey = "g";
        clearTimeout(state.pendingTimer);
        state.pendingTimer = window.setTimeout(() => {
          state.pendingKey = null;
        }, 1200);
        break;
      case "j":
      case "k":
        event.preventDefault();
        moveCursor(event.key === "j" ? 1 : -1);
        break;
      case "r":
        refresh();
        toast("Refreshing\u2026");
        break;
      case "t":
        if (window.AetherTheme) {
          window.AetherTheme.toggle();
        }
        break;
      default:
        break;
    }
  });

  search.addEventListener("input", () => {
    state.filter = search.value;
    applyFilter();
  });
  liveToggle.addEventListener("click", () => setPaused(!state.paused));
  drawerClose.addEventListener("click", closeDrawer);
  document.getElementById("shortcuts-open").addEventListener("click", openShortcuts);

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      clearTimeout(state.timer);
    } else if (!state.paused) {
      const stale = !state.updatedAt || Date.now() - state.updatedAt > state.seconds * 1000;
      if (stale) {
        refresh();
      } else {
        schedule();
      }
    }
  });

  if (tokenInput) {
    tokenInput.value = sessionStorage.getItem(TOKEN_KEY) || "";
    tokenInput.addEventListener("input", () => {
      if (tokenInput.value) {
        sessionStorage.setItem(TOKEN_KEY, tokenInput.value);
      } else {
        sessionStorage.removeItem(TOKEN_KEY);
      }
    });
    document.getElementById("forget-token").addEventListener("click", () => {
      sessionStorage.removeItem(TOKEN_KEY);
      tokenInput.value = "";
      setRespondStatus("Token forgotten.");
      toast("Token forgotten for this tab.", "ok");
    });
  }

  // ---------------------------------------------------------------- start

  enhance(content);
  updateLive();
  window.setInterval(() => {
    updateLive();
    updateTimes(content);
    if (state.drawerTask) {
      updateTimes(drawerBody);
    }
  }, 1000);
  const linked = /^#task=([0-9a-f-]{36})$/i.exec(window.location.hash);
  if (linked) {
    openTask(linked[1].toLowerCase(), null);
  }
  refresh();
})();
