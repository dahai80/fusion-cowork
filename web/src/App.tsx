import { useEffect, useState } from 'react';
import { NotificationBell } from './NotificationBell';
import { useT } from './i18n';
import { ApprovalsPage } from './pages/ApprovalsPage';
import { CanvasPage } from './pages/CanvasPage';
import { ClusterPage } from './pages/ClusterPage';
import { DelegatePage } from './pages/DelegatePage';
import { MobilePage } from './pages/MobilePage';
import { NodesPage } from './pages/NodesPage';
import { PluginsPage } from './pages/PluginsPage';
import { RunsPage } from './pages/RunsPage';
import { SchedulePage } from './pages/SchedulePage';
import { SessionsPage } from './pages/SessionsPage';
import { SpacePage } from './pages/SpacePage';
import { TemplatesPage } from './pages/TemplatesPage';
import { UsagePage } from './pages/UsagePage';
import { WorkflowsPage } from './pages/WorkflowsPage';
import { embeddedParam, isEmbedded } from './sdk';

// Hash router (no router dep). Routes: workflows, delegate, canvas
// (#/canvas/<id>), runs, sessions, nodes, templates, schedule. Standalone
// shows its own header tabs; when the shell embeds us (?embedded=1 or
// iframe) the shell owns the chrome — own navigation hides (spec S7).
type Route =
    | 'workflows'
    | 'delegate'
    | 'canvas'
    | 'runs'
    | 'sessions'
    | 'spaces'
    | 'nodes'
    | 'templates'
    | 'schedule'
    | 'plugins'
    | 'approvals'
    | 'cluster'
    | 'usage'
    | 'm';

const ROUTES: Route[] = [
    'workflows',
    'delegate',
    'runs',
    'sessions',
    'spaces',
    'schedule',
    'templates',
    'nodes',
    'canvas',
    'plugins',
    'approvals',
    'cluster',
    'usage',
];

function parseHash(): { route: Route; arg: string } {
    const h = window.location.hash.replace(/^#\/?/, '');
    const [head, arg = ''] = h.split('/');
    if (head === 'canvas') return { route: 'canvas', arg };
    if (head === 'delegate') return { route: 'delegate', arg };
    if (head === 'runs') return { route: 'runs', arg };
    if (head === 'sessions') return { route: 'sessions', arg };
    if (head === 'spaces') return { route: 'spaces', arg };
    if (head === 'nodes') return { route: 'nodes', arg };
    if (head === 'templates') return { route: 'templates', arg };
    if (head === 'schedule') return { route: 'schedule', arg };
    if (head === 'plugins') return { route: 'plugins', arg };
    if (head === 'approvals') return { route: 'approvals', arg };
    if (head === 'cluster') return { route: 'cluster', arg };
    if (head === 'usage') return { route: 'usage', arg };
    if (head === 'm') return { route: 'm', arg };
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
        case 'spaces':
            return t('空间', 'Spaces');
        case 'schedule':
            return t('调度', 'Schedule');
        case 'templates':
            return t('模板', 'Templates');
        case 'nodes':
            return t('节点', 'Nodes');
        case 'canvas':
            return t('画布', 'Canvas');
        case 'plugins':
            return t('插件', 'Plugins');
        case 'approvals':
            return t('审批', 'Approvals');
        case 'cluster':
            return t('集群', 'Cluster');
        case 'usage':
            return t('用量', 'Usage');
        case 'm':
            return t('移动端', 'Mobile');
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
                    <div className="flex items-center gap-2">
                        <NotificationBell />
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
                    </div>
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
                ) : route === 'spaces' ? (
                    <SpacePage />
                ) : route === 'nodes' ? (
                    <NodesPage />
                ) : route === 'plugins' ? (
                    <PluginsPage />
                ) : route === 'approvals' ? (
                    <ApprovalsPage />
                ) : route === 'cluster' ? (
                    <ClusterPage />
                ) : route === 'usage' ? (
                    <UsagePage />
                ) : route === 'm' ? (
                    <MobilePage />
                ) : route === 'templates' ? (
                    <TemplatesPage />
                ) : (
                    <SchedulePage />
                )}
            </main>
        </div>
    );
}
