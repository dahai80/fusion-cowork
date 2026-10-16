import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// Dev server 11464 (architecture/port-registry.yaml). Proxies API paths to
// the web API server on 11461 so standalone mode mirrors the embedded
// gateway /shell/fusion-cowork/ layout (same-origin fetch, no CORS).
// FUSION_DEV_BACKEND / FUSION_COWORK_WEB_PORT override for CI parity.
const backend = process.env.FUSION_DEV_BACKEND ?? 'http://127.0.0.1:11461';
const port = Number(process.env.FUSION_COWORK_WEB_PORT ?? 11464);

const apiPaths = ['/api', '/health'];

export default defineConfig({
    plugins: [react()],
    // issue #94: embedded deployment serves dist under the gateway sub-path
    // /shell/fusion-cowork/ — absolute /assets would 404 there. Relative
    // base + hash router (#/...) resolves assets against the module root in
    // BOTH embedded and standalone-served modes.
    base: './',
    server: {
        port,
        strictPort: true,
        proxy: Object.fromEntries(apiPaths.map((p) => [p, { target: backend, changeOrigin: true }])),
    },
    preview: {
        port,
        strictPort: true,
        proxy: Object.fromEntries(apiPaths.map((p) => [p, { target: backend, changeOrigin: true }])),
    },
    envPrefix: ['VITE_', 'FUSION_'],
});
