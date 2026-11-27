import { useAutoRetry } from '@fusion/web-ui';
import { useEffect, useState } from 'react';
import { BrowserPanel } from './BrowserPanel';
import { CommandPalette } from './CommandPalette';
import { NotificationBell } from './NotificationBell';
import { api } from './api';
import { useT } from './i18n';
import { onPreferredModelChange, preferredModel, setPreferredModel } from './modelPref';
import { AdminPage } from './pages/AdminPage';
import { ApprovalsPage } from './pages/ApprovalsPage';
import BenchmarkPage from './pages/BenchmarkPage';
import { CanvasPage } from './pages/CanvasPage';
import { CapabilitiesPage } from './pages/CapabilitiesPage';
import { ClusterPage } from './pages/ClusterPage';
import { DelegatePage } from './pages/DelegatePage';
import { DemandPage } from './pages/DemandPage';
import { ExtensionsPage } from './pages/ExtensionsPage';
import { GovernancePage } from './pages/GovernancePage';
import { HomePage } from './pages/HomePage';
import { MobilePage } from './pages/MobilePage';
import { NodesPage } from './pages/NodesPage';
import PermissionsRulesPage from './pages/PermissionsRulesPage';
import { PipelinePage } from './pages/PipelinePage';
import { PluginsPage } from './pages/PluginsPage';
import { RunViewPage } from './pages/RunViewPage';
import { RunsPage } from './pages/RunsPage';
import { SchedulePage } from './pages/SchedulePage';
import { SessionsPage } from './pages/SessionsPage';
import { SkillsPage } from './pages/SkillsPage';
import { SpacePage } from './pages/SpacePage';
import SystemPage from './pages/SystemPage';
import TeamPage from './pages/TeamPage';
import { TemplatesPage } from './pages/TemplatesPage';
import { UsagePage } from './pages/UsagePage';
import VoicePage from './pages/VoicePage';
import WizardPage from './pages/WizardPage';
import { WorkflowsPage } from './pages/WorkflowsPage';
import { embeddedParam, isEmbedded } from './sdk';

// Hash router (no router dep). Routes: home (default landing, issue #223),
// workflows, delegate, canvas (#/canvas/<id>), runs, sessions, nodes,
// templates, schedule. Standalone
// shows its own header tabs; when the shell embeds us (?embedded=1 or
// iframe) the shell owns the chrome — own navigation hides (spec S7).
type Route =
    | 'home'
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
    | 'admin'
    | 'skills'
    | 'extensions'
    | 'capabilities'
    | 'wizard'
    | 'permissions'
    | 'benchmark'
    | 'system'
    | 'team'
    | 'voice'
    | 'governance'
    | 'pipeline'
    | 'demand'
    | 'm';

const ROUTES: Route[] = [
    'home',
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
    'admin',
    'skills',
    'extensions',
    'capabilities',
    'wizard',
    'permissions',
    'benchmark',
    'system',
    'team',
    'voice',
    'governance',
    'pipeline',
];

function parseHash(): { route: Route; arg: string } {
    const h = window.location.hash.replace(/^#\/?/, '');
    const [head, arg = ''] = h.split('/');
    if (head === 'canvas') return { route: 'canvas', arg };
    if (head === 'home') return { route: 'home', arg };
    if (head === 'workflows') return { route: 'workflows', arg };
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
    if (head === 'admin') return { route: 'admin', arg };
    if (head === 'skills') return { route: 'skills', arg };
    if (head === 'extensions') return { route: 'extensions', arg };
    if (head === 'capabilities') return { route: 'capabilities', arg };
    if (head === 'wizard') return { route: 'wizard', arg };
    if (head === 'permissions') return { route: 'permissions', arg };
    if (head === 'benchmark') return { route: 'benchmark', arg };
    if (head === 'system') return { route: 'system', arg };
    if (head === 'team') return { route: 'team', arg };
    if (head === 'voice') return { route: 'voice', arg };
    if (head === 'governance') return { route: 'governance', arg };
    if (head === 'pipeline') return { route: 'pipeline', arg };
    if (head === 'demand') return { route: 'demand', arg };
    if (head === 'm') return { route: 'm', arg };
    return { route: 'home', arg };
}

function routeLabel(r: Route, t: (zh: string, en: string) => string): string {
    switch (r) {
        case 'home':
            // i18n: user-facing
            return t('首页', 'Home');
        case 'workflows':
            // i18n: user-facing
            return t('工作流', 'Workflows');
        case 'delegate':
            // i18n: user-facing
            return t('委托', 'Delegate');
        case 'runs':
            // i18n: user-facing
            return t('运行', 'Runs');
        case 'sessions':
            // i18n: user-facing
            return t('会话', 'Sessions');
        case 'spaces':
            // i18n: user-facing
            return t('空间', 'Spaces');
        case 'schedule':
            // i18n: user-facing
            return t('调度', 'Schedule');
        case 'templates':
            // i18n: user-facing
            return t('模板', 'Templates');
        case 'nodes':
            // i18n: user-facing
            return t('节点', 'Nodes');
        case 'canvas':
            // i18n: user-facing
            return t('画布', 'Canvas');
        case 'plugins':
            // i18n: user-facing
            return t('插件', 'Plugins');
        case 'approvals':
            // i18n: user-facing
            return t('审批', 'Approvals');
        case 'cluster':
            // i18n: user-facing
            return t('集群', 'Cluster');
        case 'usage':
            // i18n: user-facing
            return t('用量', 'Usage');
        case 'admin':
            // i18n: user-facing
            return t('管理', 'Admin');
        case 'skills':
            // i18n: user-facing
            return t('技能', 'Skills');
        case 'extensions':
            // i18n: user-facing
            return t('扩展', 'Extensions');
        case 'wizard':
            // i18n: user-facing
            return t('Onboarding 向导', 'Onboarding Wizard');
        case 'permissions':
            // i18n: user-facing
            return t('权限规则', 'Permissions');
        case 'benchmark':
            // i18n: user-facing
            return t('基准测试', 'Benchmark');
        case 'system':
            // i18n: user-facing
            return t('系统', 'System');
        case 'team':
            // i18n: user-facing
            return t('团队', 'Team');
        case 'voice':
            // i18n: user-facing
            return t('语音', 'Voice');
        case 'governance':
            // i18n: user-facing
            return t('治理', 'Governance');
        case 'pipeline':
            // i18n: user-facing
            return t('交付管线', 'Pipeline');
        case 'demand':
            // i18n: user-facing
            return t('需求引擎', 'Demand');
        case 'capabilities':
            // i18n: user-facing
            return t('能力市场', 'Capabilities');
        case 'm':
            // i18n: user-facing
            return t('移动端', 'Mobile');
    }
}

export function App() {
    const t = useT();
    const [{ route, arg }, setNav] = useState(parseHash);
    const embedded = isEmbedded() || embeddedParam();
    const [model, setModel] = useState(preferredModel);
    const models = useAutoRetry(() => api.models(), []);

    useEffect(() => {
        const onHash = () => setNav(parseHash());
        window.addEventListener('hashchange', onHash);
        return () => window.removeEventListener('hashchange', onHash);
    }, []);

    useEffect(() => onPreferredModelChange(setModel), []);

    function go(next: Route) {
        window.location.hash = `#/${next}`;
    }

    return (
        <div className="flex h-screen flex-col bg-bg text-text-primary">
            {!embedded ? (
                <header className="flex items-center justify-between border-b border-border px-4 py-2">
                    <div className="text-sm font-medium">Fusion Cowork</div>
                    <div className="flex items-center gap-2">
                        <select
                            // i18n: user-facing
                            aria-label={t('模型', 'Model')}
                            // i18n: user-facing
                            title={models.error ? t('模型列表不可用', 'Models unavailable') : undefined}
                            className="rounded-md border border-border bg-bg px-2 py-1 text-xs text-text-primary"
                            value={model}
                            onChange={(e) => {
                                setModel(e.target.value);
                                setPreferredModel(e.target.value);
                            }}
                        >
                            {/* i18n: user-facing */}
                            <option value="">{t('默认模型', 'Default model')}</option>
                            {model && !(models.data?.models ?? []).some((m) => m.id === model) ? (
                                <option value={model}>{model}</option>
                            ) : null}
                            {(models.data?.models ?? []).map((m) => (
                                <option key={m.id} value={m.id}>
                                    {m.id}
                                </option>
                            ))}
                        </select>
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
                {route === 'home' ? (
                    <HomePage />
                ) : route === 'workflows' ? (
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
                    arg ? (
                        <RunViewPage runId={arg} />
                    ) : (
                        <RunsPage />
                    )
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
                ) : route === 'admin' ? (
                    <AdminPage />
                ) : route === 'skills' ? (
                    <SkillsPage />
                ) : route === 'extensions' ? (
                    <ExtensionsPage />
                ) : route === 'capabilities' ? (
                    <CapabilitiesPage arg={arg} />
                ) : route === 'm' ? (
                    <MobilePage />
                ) : route === 'templates' ? (
                    <TemplatesPage />
                ) : route === 'wizard' ? (
                    <WizardPage />
                ) : route === 'permissions' ? (
                    <PermissionsRulesPage />
                ) : route === 'benchmark' ? (
                    <BenchmarkPage />
                ) : route === 'system' ? (
                    <SystemPage />
                ) : route === 'team' ? (
                    <TeamPage />
                ) : route === 'voice' ? (
                    <VoicePage />
                ) : route === 'governance' ? (
                    <GovernancePage />
                ) : route === 'pipeline' ? (
                    <PipelinePage runId={arg} />
                ) : route === 'demand' ? (
                    <DemandPage />
                ) : (
                    <SchedulePage />
                )}
            </main>
            <BrowserPanel />
            <CommandPalette />
        </div>
    );
}
