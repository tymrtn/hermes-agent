/**
 * Regression: the Kanban dashboard plugin must not blank the board when the
 * host SDK it is paired with predates the ``buildWsUrl`` WebSocket helper.
 *
 * Live incident: a stale host bundle (built before ``buildWsUrl`` was added to
 * ``window.__HERMES_PLUGIN_SDK__``) was served alongside the current plugin
 * bundle. The plugin's WebSocket effect called ``SDK.buildWsUrl(...)``
 * unguarded; against the stale SDK that is ``undefined``, so the effect threw
 * ``TypeError: SDK.buildWsUrl is not a function`` synchronously — React
 * unmounted the tree and the board went blank.
 *
 * This test *executes the real plugin bundle* (no source-shape assertions).
 * It supplies the plugin the two host-SDK shapes it must survive — one with
 * ``buildWsUrl`` (current host) and one without (legacy/stale host) — and
 * drives it through mount → board load → WebSocket effect. The plugin is
 * decoupled from React by design (it consumes ``SDK.React`` / ``SDK.hooks``),
 * so the host here provides a minimal synchronous hooks runtime that runs the
 * component's real effects, exactly as the browser host runs them.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it, vi } from "vitest";

const PLUGIN_PATH = fileURLToPath(
  new URL("../../../plugins/kanban/dashboard/dist/index.js", import.meta.url),
);
const PLUGIN_SRC = readFileSync(PLUGIN_PATH, "utf8");

// --- minimal React the plugin consumes via SDK.React -----------------------
// createElement records a plain tree; child components are never recursed
// (the plugin renders KanbanPage; children appear only as `type` refs). This
// is enough to inspect what KanbanPage itself committed.
type VNode = { type: unknown; props: Record<string, unknown>; children: unknown[] };

function createElement(type: unknown, props: Record<string, unknown> | null, ...children: unknown[]): VNode {
  return { type, props: props || {}, children };
}

const mockReact = { createElement, Component: class Component {} };

// --- minimal, synchronous hooks runtime the plugin consumes via SDK.hooks --
// A single-fiber slot model: renders run top-to-bottom, effects fire after
// each render when their deps change, and setState schedules another render.
// This faithfully drives the component's real useEffect bodies (where the
// crash lives) without needing react-dom / a DOM.
function createHooksHost() {
  const slots: Record<number, { value?: unknown; deps?: unknown[]; cleanup?: unknown; current?: unknown }> = {};
  let cursor = 0;
  let pendingEffects: Array<{ i: number; fn: () => unknown; deps?: unknown[] }> = [];
  let dirty = false;
  let lastTree: unknown = null;

  function sameDeps(a?: unknown[], b?: unknown[]): boolean {
    if (!a || !b || a.length !== b.length) return false;
    return a.every((v, i) => Object.is(v, b[i]));
  }

  const hooks = {
    useState(init: unknown) {
      const i = cursor++;
      if (!slots[i]) slots[i] = { value: typeof init === "function" ? (init as () => unknown)() : init };
      const slot = slots[i];
      const setState = (next: unknown) => {
        const v = typeof next === "function" ? (next as (p: unknown) => unknown)(slot.value) : next;
        if (!Object.is(v, slot.value)) {
          slot.value = v;
          dirty = true;
        }
      };
      return [slot.value, setState];
    },
    useRef(init: unknown) {
      const i = cursor++;
      if (!slots[i]) slots[i] = { current: init };
      return slots[i];
    },
    useMemo(fn: () => unknown, deps?: unknown[]) {
      const i = cursor++;
      const prev = slots[i];
      if (!prev || !sameDeps(prev.deps, deps)) slots[i] = { value: fn(), deps };
      return slots[i].value;
    },
    useCallback(fn: unknown, deps?: unknown[]) {
      const i = cursor++;
      const prev = slots[i];
      if (!prev || !sameDeps(prev.deps, deps)) slots[i] = { value: fn, deps };
      return slots[i].value;
    },
    useEffect(fn: () => unknown, deps?: unknown[]) {
      const i = cursor++;
      pendingEffects.push({ i, fn, deps });
    },
    useContext() {
      return undefined;
    },
    createContext() {
      return {};
    },
  };

  function renderOnce(Component: (props: Record<string, unknown>) => unknown) {
    cursor = 0;
    pendingEffects = [];
    lastTree = Component({});
    // commit passive effects — this is where openWs() runs, and where the
    // unguarded buildWsUrl() call throws against a legacy SDK.
    for (const e of pendingEffects) {
      const prev = slots[e.i];
      if (prev && sameDeps(prev.deps, e.deps)) continue;
      if (prev && typeof prev.cleanup === "function") (prev.cleanup as () => void)();
      const cleanup = e.fn();
      slots[e.i] = { deps: e.deps, cleanup: typeof cleanup === "function" ? cleanup : undefined };
    }
  }

  async function mount(Component: (props: Record<string, unknown>) => unknown) {
    let guard = 0;
    do {
      dirty = false;
      renderOnce(Component); // may throw out of here (the blank-board crash)
      // Let mocked fetchJSON promise chains resolve → setState → dirty.
      await new Promise((res) => setTimeout(res, 0));
    } while (dirty && ++guard < 50);
    return lastTree;
  }

  return { hooks, mount };
}

// --- tree helpers ----------------------------------------------------------
function findByClass(tree: unknown, sub: string): VNode | null {
  let found: VNode | null = null;
  const walk = (node: unknown) => {
    if (found || node == null || typeof node !== "object") return;
    if (Array.isArray(node)) {
      node.forEach(walk);
      return;
    }
    const n = node as VNode;
    const cls = n.props?.className;
    if (typeof cls === "string" && cls.includes(sub)) {
      found = n;
      return;
    }
    if (n.props && n.props.children != null) walk(n.props.children);
    if (n.children) walk(n.children);
  };
  walk(tree);
  return found;
}

function findByProp(tree: unknown, name: string, value: unknown): VNode | null {
  let found: VNode | null = null;
  const walk = (node: unknown) => {
    if (found || node == null || typeof node !== "object") return;
    if (Array.isArray(node)) {
      node.forEach(walk);
      return;
    }
    const n = node as VNode;
    if (n.props?.[name] === value) {
      found = n;
      return;
    }
    if (n.props?.children != null) walk(n.props.children);
    if (n.children) walk(n.children);
  };
  walk(tree);
  return found;
}

function collectText(node: unknown): string {
  if (node == null) return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(collectText).join("");
  if (typeof node === "object") {
    const n = node as VNode;
    let s = "";
    if (n.props && n.props.children != null) s += collectText(n.props.children);
    if (n.children) s += collectText(n.children);
    return s;
  }
  return "";
}

// --- host / plugin harness -------------------------------------------------
function fetchJSONMock(currentBoard = "skippy") {
  return vi.fn(async (url: string) => {
    if (url.includes("/config")) return { render_markdown: true };
    if (url.includes("/boards")) {
      return {
        boards: [
          { slug: "skippy", name: "Skippy" },
          { slug: "klasificados", name: "Klasificados" },
        ],
        current: currentBoard,
      };
    }
    if (url.includes("/board")) return { columns: [], assignees: [], tenants: [], latest_event_id: 0 };
    return {};
  });
}

interface HostOpts {
  withBuildWsUrl: boolean;
  buildWsUrl?: ReturnType<typeof vi.fn>;
  sockets?: string[];
  search?: string;
  storedBoard?: string | null;
  currentBoard?: string;
}

function loadPluginWithHost(hooks: unknown, opts: HostOpts) {
  const registered: Record<string, (props: Record<string, unknown>) => unknown> = {};
  const sockets = opts.sockets ?? [];

  const sdk: Record<string, unknown> = {
    React: mockReact,
    hooks,
    components: {}, // primitives are only used as element `type` refs here
    utils: { cn: (...c: unknown[]) => c.filter(Boolean).join(" "), timeAgo: () => "", isoTimeAgo: () => "" },
    fetchJSON: fetchJSONMock(opts.currentBoard),
    authedFetch: vi.fn(async () => ({ ok: true, json: async () => ({}) })),
  };
  if (opts.withBuildWsUrl) {
    sdk.sdkVersion = "1.1.0";
    sdk.buildWsUrl = opts.buildWsUrl ?? vi.fn(async () => "ws://host.test/events");
    sdk.buildWsAuthParam = vi.fn(async () => ["token", ""]);
  }

  const fakeWindow: Record<string, unknown> = {
    __HERMES_PLUGIN_SDK__: sdk,
    __HERMES_PLUGINS__: {
      register(name: string, comp: (props: Record<string, unknown>) => unknown) {
        registered[name] = comp;
      },
      registerSlot() {},
    },
    __HERMES_SESSION_TOKEN__: "",
    __HERMES_AUTH_REQUIRED__: false,
    location: { search: opts.search ?? "" },
    localStorage: {
      getItem: () => opts.storedBoard === undefined ? "skippy" : opts.storedBoard,
      setItem: () => {},
      removeItem: () => {},
    },
    addEventListener() {},
    removeEventListener() {},
    prompt: () => null,
    confirm: () => true,
    alert: () => {},
  };

  const fakeDocument = {
    addEventListener() {},
    removeEventListener() {},
    body: { appendChild() {}, removeChild() {} },
    querySelector: () => null,
    elementFromPoint: () => null,
  };

  class FakeWebSocket {
    url: string;
    onopen: unknown;
    onmessage: unknown;
    onclose: unknown;
    constructor(url: string) {
      this.url = url;
      sockets.push(url);
    }
    close() {}
  }

  const run = new Function(
    "window",
    "document",
    "navigator",
    "WebSocket",
    "setTimeout",
    "clearTimeout",
    "console",
    PLUGIN_SRC,
  );
  run(fakeWindow, fakeDocument, { clipboard: null }, FakeWebSocket, setTimeout, clearTimeout, console);

  if (!registered.kanban) throw new Error("plugin did not register 'kanban' component");
  return { component: registered.kanban, sdk, sockets };
}

describe("kanban plugin — WebSocket helper compatibility", () => {
  it("uses SDK.buildWsUrl with {since, board} when the host provides it", async () => {
    const host = createHooksHost();
    const sockets: string[] = [];
    const buildWsUrl = vi.fn(async () => "ws://host.test/events");
    const { component } = loadPluginWithHost(host.hooks, { withBuildWsUrl: true, buildWsUrl, sockets });

    const tree = await host.mount(component);

    // Board rendered (not blanked) and the current helper path was used with
    // the pinned board + cursor params preserved.
    expect(findByClass(tree, "hermes-kanban")).toBeTruthy();
    expect(buildWsUrl).toHaveBeenCalledWith("/api/plugins/kanban/events", { since: "0", board: "skippy" });
    expect(sockets).toEqual(["ws://host.test/events"]);
  });

  it("lets board/task deep links override the remembered board and opens the drawer", async () => {
    const host = createHooksHost();
    const buildWsUrl = vi.fn(async () => "ws://host.test/events");
    const { component } = loadPluginWithHost(host.hooks, {
      withBuildWsUrl: true,
      buildWsUrl,
      search: "?board=skippy&task=t_980e1afd",
      storedBoard: "klasificados",
    });

    const tree = await host.mount(component);

    expect(findByClass(tree, "hermes-kanban")).toBeTruthy();
    expect(buildWsUrl).toHaveBeenCalledWith("/api/plugins/kanban/events", { since: "0", board: "skippy" });
    expect(findByProp(tree, "taskId", "t_980e1afd")).toBeTruthy();
  });

  it("uses the backend current board when neither a deep link nor remembered board exists", async () => {
    const host = createHooksHost();
    const buildWsUrl = vi.fn(async () => "ws://host.test/events");
    const { component } = loadPluginWithHost(host.hooks, {
      withBuildWsUrl: true,
      buildWsUrl,
      storedBoard: null,
      currentBoard: "klasificados",
    });

    const tree = await host.mount(component);

    expect(findByClass(tree, "hermes-kanban")).toBeTruthy();
    expect(buildWsUrl).toHaveBeenLastCalledWith(
      "/api/plugins/kanban/events",
      { since: "0", board: "klasificados" },
    );
  });

  it("degrades live updates without blanking the board when the host lacks buildWsUrl", async () => {
    const host = createHooksHost();
    const sockets: string[] = [];
    const { component } = loadPluginWithHost(host.hooks, { withBuildWsUrl: false, sockets });

    // Must NOT throw (a synchronous throw here is what unmounts the board).
    const tree = await host.mount(component);

    // Board tree still present — the board, drawer, and REST actions stay usable.
    expect(findByClass(tree, "hermes-kanban")).toBeTruthy();

    // No hand-rolled WebSocket attempt (no insecure fallback auth).
    expect(sockets.length).toBe(0);

    // A visible, non-fatal degraded-live-updates status is surfaced (the board
    // is NOT replaced by the fatal load-error card).
    const banner = findByClass(tree, "text-destructive");
    expect(banner).toBeTruthy();
    expect(collectText(banner)).toMatch(/live updates/i);
  });
});
