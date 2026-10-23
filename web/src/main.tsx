import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { initTheme } from './theme';
import '@fusion/web-ui/tokens.css';
import './index.css';

// issue #176: apply the shell/shared theme before first paint so the iframe
// never flashes the wrong palette, then keep following shell toggles.
initTheme();

const rootEl = document.getElementById('root');
if (rootEl) {
    createRoot(rootEl).render(
        <StrictMode>
            <App />
        </StrictMode>
    );
} else {
    console.error('[web] #root element missing');
}
