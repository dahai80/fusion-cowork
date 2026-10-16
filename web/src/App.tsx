import { useEffect, useState } from 'react';
import { useT } from './i18n';
import { CanvasPage } from './pages/CanvasPage';
import { RunsPage } from './pages/RunsPage';
import { WorkflowsPage } from './pages/WorkflowsPage';
import { embeddedParam, isEmbedded } from './sdk';

// Hash router (no router dep). Three routes: workflow list, DAG canvas
// (optionally #/canvas/<id> to edit a saved workflow), runs. Standalone
// shows its own header tabs; when the shell embeds us (?embedded=1 or
// iframe) the shell owns the chrome — own navigation hides (spec S7).
type Route = 'workflows' | 'canvas' | 'runs';

function parseHash(): { route: Route; arg: string } {
    const h = window.location.hash.replace(/^#\/?/, '');
    const [head, arg = ''] = h.split('/');
    if (head === 'canvas') return { route: 'canvas', arg };
    if (head === 'runs') return { route: 'runs', arg };
    return { route: 'workflows', arg };
}

export function App() {
    const t = useT();
    const [{ route, arg }, setNav] = useState(parseHash);
    const embedded = isEmbedded() || embeddedParam();

    useEffect(() => {
        const onHash = () => setNav(parseHash());
        window.addEventListener('hashchange', onHash);
        return () => window.removeEventListener('hashchange', onHash);
    }, []);

    function go(next: Route) {
        window.location.hash = `#/${next}`;
    }

    return (
        <div className="flex h-screen flex-col bg-bg text-text-primary">
            {!embedded ? (
                <header className="flex items-center justify-between border-b border-border px-4 py-2">
                    <div className="text-sm font-medium">Fusion Cowork</div>
                    <nav className="flex gap-1">
                        {(['workflows', 'canvas', 'runs'] as Route[]).map((r) => (
                            <button
                                type="button"
                                key={r}
                                className={`rounded-md px-3 py-1 text-sm ${
                                    route === r
                                        ? 'bg-bg-muted text-text-primary'
                                        : 'text-text-secondary hover:bg-bg-muted'
                                }`}
                                onClick={() => go(r)}
                            >
                                {r === 'workflows'
                                    ? t('工作流', 'Workflows')
                                    : r === 'canvas'
                                      ? t('画布', 'Canvas')
                                      : t('运行', 'Runs')}
                            </button>
                        ))}
                    </nav>
                </header>
            ) : null}
            <main className={`min-h-0 flex-1 ${route === 'canvas' ? 'overflow-hidden' : 'overflow-y-auto p-4'}`}>
                {route === 'workflows' ? (
                    <WorkflowsPage
                        onEdit={(id) => {
                            window.location.hash = `#/canvas/${id}`;
                        }}
                    />
                ) : route === 'canvas' ? (
                    <CanvasPage workflowId={arg} />
                ) : (
                    <RunsPage />
                )}
            </main>
        </div>
    );
}
