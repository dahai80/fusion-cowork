# Fusion Cowork Web

DAG workflow canvas UI package for fusion-cowork — the web edition of the workflow builder (union-web PRD N-6, issue #94).

## Scope

- Workflow list with run / edit / delete (confirm dialog, name-typed for delete)
- DAG canvas editing on desktop: node palette (from `/api/nodes`), drag positioning, connect edges, per-node params editor with schema hints
- Read-only canvas on mobile (`devices: ["desktop", "mobile-readonly"]`): palette, editing and write actions hidden, `ReadOnlyBanner` shown
- Runs view: live status polling (5s visible / 30s hidden-tab throttled), per-step results, cancel
- Embedded mode (`?embedded=1` / iframe) hides own navigation — the union-web shell owns the chrome

## Stack (Spec §10.1 baseline)

React 18 + Vite 5 + TypeScript + Tailwind (preset from `@fusion/web-ui`) + Bun. Canvas: `@xyflow/react` (lazy-friendly, desktop editing).

## Commands

```bash
bun install
bun run dev        # vite dev server :11464 (proxies /api /health → :11461)
bun run build      # typecheck + vite build → dist/
bun run typecheck
bun run lint       # biome check src
```

## Backend

The UI consumes the cowork web API (`fusion-cowork web serve`, port 11461):
nodes catalog, workflow definition CRUD, run lifecycle (submit / poll / cancel).
Static hosting of `web/dist` is built into the same server.

Auth: `FUSION_COWORK_WEB_API_KEY` — the gateway `/shell/fusion-cowork/` proxy
injects the module key; the UI itself holds no keys (spec S8). Standalone dev
convenience: `VITE_API_KEY` (build-time env, embedded mode never uses it).

## Manifest

`manifest.json` follows union-web PRD §5.1 (id `fusion-cowork`, category
`platform`, entry `/shell/fusion-cowork/`, devices `["desktop",
"mobile-readonly"]`).

## Vendored packages

`@fusion/web-sdk` and `@fusion/web-ui` are vendored under `vendor/@fusion/`
(byte-identical to the fusion-web-shell repo sources — no private fork).
