import { useEffect, useState } from 'react';
import { useT } from './i18n';
import { CanvasPage } from './pages/CanvasPage';
import { DelegatePage } from './pages/DelegatePage';
import { NodesPage } from './pages/NodesPage';
import { RunsPage } from './pages/RunsPage';
import { SchedulePage } from './pages/SchedulePage';
import { SessionsPage } from './pages/SessionsPage';
import { TemplatesPage } from './pages/TemplatesPage';
import { WorkflowsPage } from './pages/WorkflowsPage';
import { embeddedParam, isEmbedded } from './sdk';

// Hash router (no router dep). Routes: workflows, delegate, canvas
// (#/canvas/<id>), runs, sessions, nodes, templates, schedule. Standalone
// shows its own header tabs; when the shell embeds us (?embedded=1 or
// iframe) the shell owns the chrome — own navigation hides (spec S7).
type Route = 'workflows' | 'delegate' | 'canvas' | 'runs' | 'sessions' | 'nodes' | 'templates' | 'schedule';

const ROUTES: Route[] = ['workflows', 'delegate', 'runs', 'sessions', 'schedule', 'templates', 'nodes', 'canvas'];

function parseHash(): { route: Route; arg: string } {
    const h = window.location.hash.replace(/^#\/?/, '');
    const [head, arg = ''] = h.split('/');
    if (head === 'canvas') return { route: 'canvas', arg };
    if (head === 'delegate') return { route: 'delegate', arg };
    if (head === 'runs') return { route: 'runs', arg };
    if (head === 'sessions') return { route: 'sessions', arg };
    if (head === 'nodes') return { route: 'nodes', arg };
    if (head === 'templates') return { route: 'templates', arg };
    if (head === 'schedule') return { route: 'schedule', arg };
    return { route: 'workflows', arg };
}

function routeLabel(r: Route, t: (zh: string, en: string) => string): string {
    switch (r) {
        case 'workflows':
            return t('工作流', 'Workflows');
        case 'delegate':
            return t('委托', 'Delegate');
        case 'runs':
            return t('运行', 'Runs');
        case 'sessions':
            return t('会话', 'Sessions');
        case 'schedule':
            return t('调度', 'Schedule');
        case 'templates':
            return t('模板', 'Templates');
        case 'nodes':
            return t('节点', 'Nodes');
        case 'canvas':
            return t('画布', 'Canvas');
    }
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
                        {ROUTES.map((r) => (
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
                                {routeLabel(r, t)}
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
                ) : route === 'delegate' ? (
                    <DelegatePage />
                ) : route === 'runs' ? (
                    <RunsPage />
                ) : route === 'sessions' ? (
                    <SessionsPage />
                ) : route === 'nodes' ? (
                    <NodesPage />
                ) : route === 'templates' ? (
                    <TemplatesPage />
                ) : (
                    <SchedulePage />
                )}
            </main>
        </div>
    );
}
