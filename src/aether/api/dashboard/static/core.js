// Pure helpers for the dashboard: DOM building that only ever creates text nodes, formatting,
// sorting, filtering and the task-graph layout. No page state lives here, so it is testable alone.
(function (global) {
  "use strict";
  const SVG_NS = "http://www.w3.org/2000/svg";
  const DASH = "\u2014";

  function isNode(value) {
    return value !== null && typeof value === "object" && typeof value.nodeType === "number";
  }

  function append(parent, children) {
    for (const child of children) {
      if (Array.isArray(child)) {
        append(parent, child);
      } else if (child !== null && child !== undefined && child !== false) {
        // Strings always become text nodes, so data can never be parsed as markup.
        parent.appendChild(isNode(child) ? child : global.document.createTextNode(String(child)));
      }
    }
    return parent;
  }

  function build(node, attrs, children) {
    if (attrs) {
      for (const name of Object.keys(attrs)) {
        const value = attrs[name];
        if (value === null || value === undefined || value === false) {
          continue;
        }
        if (/^on/i.test(name) || (/^(href|src|action)$/i.test(name) && !isSafeUrl(value))) {
          throw new Error(`Refusing unsafe attribute ${name}`);
        }
        node.setAttribute(name, value === true ? "" : String(value));
      }
    }
    return append(node, children);
  }

  function isSafeUrl(value) {
    // Same-origin paths and fragments only; never script, data or other URL schemes.
    return /^(\/(?!\/)|#)/.test(String(value));
  }

  function h(tag, attrs, ...children) {
    return build(global.document.createElement(tag), attrs, children);
  }

  function s(tag, attrs, ...children) {
    return build(global.document.createElementNS(SVG_NS, tag), attrs, children);
  }

  function text(value) {
    return value === null || value === undefined || value === "" ? DASH : String(value);
  }

  function token(value) {
    const source = value === null || value === undefined ? "none" : String(value);
    return source.toLowerCase().replace(/[^a-z0-9_-]/g, "-");
  }

  function parseTime(value) {
    if (!value) {
      return null;
    }
    let source = String(value).trim().replace(" ", "T").replace(/(\.\d{3})\d+/, "$1");
    if (!/(Z|[+-]\d\d:?\d\d)$/i.test(source)) {
      source += "Z";
    }
    const ms = Date.parse(source);
    return Number.isNaN(ms) ? null : ms;
  }

  function relative(seconds) {
    const future = seconds < -0.5;
    const value = Math.abs(seconds);
    let label;
    if (value < 90) {
      label = `${Math.floor(value)}s`;
    } else if (value < 5400) {
      label = `${Math.floor(value / 60)}m`;
    } else if (value < 172800) {
      label = `${Math.floor(value / 3600)}h`;
    } else {
      label = `${Math.floor(value / 86400)}d`;
    }
    return future ? `in ${label}` : `${label} ago`;
  }

  function absolute(ms) {
    return `${new Date(ms).toISOString().replace("T", " ").slice(0, 19)} UTC`;
  }

  function duration(seconds) {
    if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) {
      return DASH;
    }
    if (seconds < 1) {
      return `${Math.round(seconds * 1000)}ms`;
    }
    if (seconds < 90) {
      return `${seconds.toFixed(1)}s`;
    }
    if (seconds < 5400) {
      return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
    }
    return `${Math.floor(seconds / 3600)}h ${Math.round((seconds % 3600) / 60)}m`;
  }

  function number(value) {
    const result = Number(value);
    return Number.isFinite(result) ? result : 0;
  }

  function count(value) {
    const amount = number(value);
    return Number.isInteger(amount)
      ? amount.toLocaleString("en-US")
      : amount.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function humanBytes(value) {
    let size = number(value);
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    for (const unit of units) {
      if (size < 1024 || unit === "TiB") {
        return unit === "B" ? `${size.toFixed(0)} B` : `${size.toFixed(1)} ${unit}`;
      }
      size /= 1024;
    }
    return DASH;
  }

  function percent(ratio) {
    return `${Math.round(ratio * 100)}%`;
  }

  function tone(ratio, thresholds) {
    const limits = thresholds || { warn: 0.7, critical: 0.9 };
    if (ratio >= limits.critical) {
      return "crit";
    }
    return ratio >= limits.warn ? "warn" : "ok";
  }

  // Mirrors Python's json.dumps(sort_keys=True) so server and browser show identical payloads.
  function pyString(value) {
    return JSON.stringify(value).replace(
      /[\u0080-\uffff]/g,
      (char) => `\\u${char.charCodeAt(0).toString(16).padStart(4, "0")}`,
    );
  }

  function pyJson(value) {
    if (Array.isArray(value)) {
      return `[${value.map(pyJson).join(", ")}]`;
    }
    if (value !== null && typeof value === "object") {
      const keys = Object.keys(value).sort();
      return `{${keys.map((key) => `${pyString(key)}: ${pyJson(value[key])}`).join(", ")}}`;
    }
    if (typeof value === "string") {
      return pyString(value);
    }
    return value === undefined ? "null" : JSON.stringify(value);
  }

  function truncate(value, max) {
    const source = String(value);
    return source.length > max ? `${source.slice(0, max - 1)}\u2026` : source;
  }

  function sortKey(cell) {
    if (!cell) {
      return "";
    }
    if (cell.dataset && cell.dataset.sort !== undefined) {
      return cell.dataset.sort;
    }
    const keyed = cell.querySelector ? cell.querySelector("data[value]") : null;
    if (keyed) {
      return keyed.getAttribute("value");
    }
    const time = cell.querySelector ? cell.querySelector("time[datetime]") : null;
    if (time) {
      return time.getAttribute("datetime");
    }
    return (cell.textContent || "").trim();
  }

  function toNumber(value) {
    const cleaned = String(value).replace(/[,$%\s]/g, "").replace(/\/.*$/, "");
    return /^[-+]?(\d+\.?\d*|\.\d+)(e[-+]?\d+)?$/i.test(cleaned) ? Number(cleaned) : null;
  }

  function isBlank(value) {
    return value === "" || value === DASH || value === null || value === undefined;
  }

  function compareKeys(a, b) {
    const x = toNumber(a);
    const y = toNumber(b);
    if (x !== null && y !== null) {
      return x - y;
    }
    return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" });
  }

  function matches(haystack, query) {
    const terms = String(query).toLowerCase().split(/\s+/).filter(Boolean);
    const source = String(haystack).toLowerCase();
    return terms.every((term) => source.includes(term));
  }

  function sparkPoints(values, low, high, width, height) {
    if (!values.length) {
      return [];
    }
    const lo = low === null || low === undefined ? Math.min(...values) : low;
    const hi = high === null || high === undefined ? Math.max(...values) : high;
    const span = hi - lo || 1;
    const points = values.map((value, index) => [
      (width * index) / Math.max(values.length - 1, 1),
      height - 3 - ((height - 6) * (value - lo)) / span,
    ]);
    return points.length === 1 ? [[0, points[0][1]], [width, points[0][1]]] : points;
  }

  // Layered (Sugiyama-style) layout: longest-path layers, barycentre ordering, left-to-right.
  function layoutGraph(tasks, dependencies, options) {
    const o = Object.assign({ nodeWidth: 220, nodeHeight: 48, gapX: 56, gapY: 14, pad: 16 }, options);
    const ids = tasks.map((task) => task.id);
    if (!ids.length) {
      return { nodes: [], edges: [], width: 0, height: 0, layers: 0 };
    }
    const known = new Set(ids);
    const parents = new Map(ids.map((id) => [id, []]));
    const children = new Map(ids.map((id) => [id, []]));
    const edges = [];
    const seen = new Set();
    for (const edge of dependencies) {
      const from = edge.depends_on_task_id;
      const to = edge.task_id;
      const key = `${from}>${to}`;
      if (known.has(from) && known.has(to) && from !== to && !seen.has(key)) {
        seen.add(key);
        parents.get(to).push(from);
        children.get(from).push(to);
        edges.push({ from, to });
      }
    }
    const indegree = new Map(ids.map((id) => [id, parents.get(id).length]));
    const layer = new Map();
    const queue = ids.filter((id) => indegree.get(id) === 0);
    queue.forEach((id) => layer.set(id, 0));
    for (let head = 0; head < queue.length; head += 1) {
      const id = queue[head];
      for (const child of children.get(id)) {
        layer.set(child, Math.max(layer.get(child) || 0, layer.get(id) + 1));
        indegree.set(child, indegree.get(child) - 1);
        if (indegree.get(child) === 0) {
          queue.push(child);
        }
      }
    }
    // Tasks caught in a dependency cycle keep whatever depth their acyclic parents gave them.
    const sparse = [];
    ids.forEach((id) => {
      const depth = layer.get(id) || 0;
      (sparse[depth] = sparse[depth] || []).push(id);
    });
    const layers = sparse.filter(Boolean);
    const position = new Map();
    layers.forEach((list) => list.forEach((id, index) => position.set(id, index)));
    const barycentre = (id, neighbours) => {
      const placed = neighbours.get(id);
      if (!placed.length) {
        return null;
      }
      return placed.reduce((sum, other) => sum + position.get(other), 0) / placed.length;
    };
    for (let pass = 0; pass < 4; pass += 1) {
      const down = pass % 2 === 0;
      const order = down ? layers.slice(1) : layers.slice(0, -1).reverse();
      for (const list of order) {
        const keyed = list.map((id, index) => {
          const centre = barycentre(id, down ? parents : children);
          return { id, index, key: centre === null ? index : centre };
        });
        keyed.sort((a, b) => a.key - b.key || a.index - b.index);
        keyed.forEach((item, index) => {
          list[index] = item.id;
          position.set(item.id, index);
        });
      }
    }
    const tallest = Math.max(...layers.map((list) => list.length));
    const nodes = new Map();
    layers.forEach((list, depth) => {
      const offset = ((tallest - list.length) * (o.nodeHeight + o.gapY)) / 2;
      list.forEach((id, index) => {
        nodes.set(id, {
          id,
          layer: depth,
          x: o.pad + depth * (o.nodeWidth + o.gapX),
          y: o.pad + offset + index * (o.nodeHeight + o.gapY),
        });
      });
    });
    const paths = edges.map(({ from, to }) => {
      const a = nodes.get(from);
      const b = nodes.get(to);
      const x1 = a.x + o.nodeWidth;
      const y1 = a.y + o.nodeHeight / 2;
      const x2 = b.x;
      const y2 = b.y + o.nodeHeight / 2;
      let d;
      if (x2 > x1) {
        const mid = (x1 + x2) / 2;
        d = `M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2 - 3},${y2}`;
      } else {
        const drop = Math.max(a.y, b.y) + o.nodeHeight + o.gapY / 2;
        const bend = o.gapX / 2;
        d =
          `M${x1},${y1} C${x1 + bend},${y1} ${x1 + bend},${drop} ${(x1 + x2) / 2},${drop} ` +
          `S${x2 - bend},${y2} ${x2 - 3},${y2}`;
      }
      return { from, to, d };
    });
    return {
      nodes: Array.from(nodes.values()),
      edges: paths,
      width: o.pad * 2 + layers.length * o.nodeWidth + (layers.length - 1) * o.gapX,
      height: o.pad * 2 + tallest * o.nodeHeight + (tallest - 1) * o.gapY + o.gapY,
      layers: layers.length,
      nodeWidth: o.nodeWidth,
      nodeHeight: o.nodeHeight,
    };
  }

  global.Aether = Object.freeze({
    DASH,
    SVG_NS,
    absolute,
    compareKeys,
    count,
    duration,
    h,
    humanBytes,
    isBlank,
    isNode,
    isSafeUrl,
    layoutGraph,
    matches,
    number,
    parseTime,
    percent,
    pyJson,
    relative,
    s,
    sortKey,
    sparkPoints,
    text,
    token,
    tone,
    truncate,
  });
})(typeof window !== "undefined" ? window : globalThis);
