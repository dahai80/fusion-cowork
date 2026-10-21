import { useEffect, useState } from 'react';
import { type BrowserStatus, type FusionBrowserStatus, api } from './api';
import { useT } from './i18n';
import { streamSse } from './sdk';

// issue #153: 托管浏览器侧栏 — 实时截图轮询 (2s) + 启动/关闭。
// issue #162: 增加 fusion-browser 引擎模式 — 每运行 profile + 登录态导入 + SSE 截图流。

const STATUS_MS = 3000;
const SHOT_MS = 2000;

type Mode = 'chromium' | 'fusion';

export function BrowserPanel() {
    const t = useT();
    const [mode, setMode] = useState<Mode>('chromium');
    const [status, setStatus] = useState<BrowserStatus>({ running: false });
    const [shot, setShot] = useState('');
    const [url, setUrl] = useState('https://');
    const [open, setOpen] = useState(true);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');

    const [fusion, setFusion] = useState<FusionBrowserStatus>({ running: false });
    const [fusionShot, setFusionShot] = useState('');
    const [sessionId, setSessionId] = useState('');
    const [runId, setRunId] = useState('');
    const [cookies, setCookies] = useState('');

    useEffect(() => {
        let alive = true;
        async function tick() {
            try {
                const next = await api.browserStatus();
                if (alive) setStatus(next);
            } catch {
                // 服务端不可达时保持上一状态
            }
        }
        void tick();
        const timer = window.setInterval(() => void tick(), STATUS_MS);
        return () => {
            alive = false;
            window.clearInterval(timer);
        };
    }, []);

    useEffect(() => {
        if (!status.running || !open || mode !== 'chromium') {
            setShot((prev) => {
                if (prev) URL.revokeObjectURL(prev);
                return '';
            });
            return;
        }
        let alive = true;
        async function grab() {
            try {
                const next = await api.browserScreenshot();
                if (!alive) {
                    URL.revokeObjectURL(next);
                    return;
                }
                setShot((prev) => {
                    if (prev) URL.revokeObjectURL(prev);
                    return next;
                });
            } catch {
                // 截图瞬时失败 (页面切换) — 下一轮重试
            }
        }
        void grab();
        const timer = window.setInterval(() => void grab(), SHOT_MS);
        return () => {
            alive = false;
            window.clearInterval(timer);
        };
    }, [status.running, open, mode]);

    useEffect(() => {
        if (!open || mode !== 'fusion') return;
        let alive = true;
        async function tick() {
            try {
                const next = await api.fusionBrowserStatus();
                if (alive) setFusion(next);
            } catch {
                // 引擎不可达
            }
        }
        void tick();
        const timer = window.setInterval(() => void tick(), STATUS_MS);
        return () => {
            alive = false;
            window.clearInterval(timer);
        };
    }, [open, mode]);

    useEffect(() => {
        if (!open || mode !== 'fusion' || !sessionId || !fusion.running) {
            setFusionShot('');
            return;
        }
        const ctrl = new AbortController();
        const path = `/api/fusion-browser/stream?session_id=${encodeURIComponent(sessionId)}`;
        streamSse(
            path,
            (event, data) => {
                if (event !== 'screenshot_frame') return;
                const png = (data as { png?: string }).png;
                if (png) setFusionShot(`data:image/png;base64,${png}`);
            },
            ctrl.signal
        ).catch((e) => console.warn('[cowork-web] fusion browser stream closed', e));
        return () => {
            ctrl.abort();
        };
    }, [open, mode, sessionId, fusion.running]);

    async function launch() {
        setBusy(true);
        setError('');
        try {
            setStatus(await api.browserLaunch({ url: url.trim() || 'about:blank' }));
        } catch (e) {
            console.error('[cowork-web] browser launch failed', e);
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy(false);
        }
    }

    async function close() {
        setBusy(true);
        try {
            await api.browserClose();
            setStatus({ running: false });
        } catch (e) {
            console.error('[cowork-web] browser close failed', e);
        } finally {
            setBusy(false);
        }
    }

    async function fusionLaunch() {
        setBusy(true);
        setError('');
        try {
            setFusion(await api.fusionBrowserLaunch({ url: url.trim() || undefined }));
        } catch (e) {
            console.error('[cowork-web] fusion browser launch failed', e);
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy(false);
        }
    }

    async function fusionSession() {
        setBusy(true);
        setError('');
        try {
            const session = await api.fusionBrowserSession({
                run_id: runId.trim() || undefined,
                initial_url: url.trim() || undefined,
            });
            setSessionId(session.session_id);
        } catch (e) {
            console.error('[cowork-web] fusion browser session failed', e);
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy(false);
        }
    }

    async function fusionImport() {
        if (!sessionId) return;
        setBusy(true);
        setError('');
        try {
            const result = await api.fusionBrowserImportCookies({ session_id: sessionId, browser: 'firefox' });
            setCookies(
                t(
                    `已导入 ${result.imported}, 跳过 app-bound ${result.skipped_app_bound}, 跳过其它 ${result.skipped_other}`,
                    `imported ${result.imported}, skipped ${result.skipped_app_bound} app-bound, ${result.skipped_other} other`
                )
            );
        } catch (e) {
            console.error('[cowork-web] fusion browser cookie import failed', e);
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy(false);
        }
    }

    async function fusionFinish() {
        setBusy(true);
        try {
            await api.fusionBrowserFinish({ run_id: runId.trim() || undefined });
            setSessionId('');
            setFusionShot('');
            setCookies('');
            setFusion(await api.fusionBrowserStatus());
        } catch (e) {
            console.error('[cowork-web] fusion browser finish failed', e);
        } finally {
            setBusy(false);
        }
    }

    if (!open) {
        return (
            <button
                type="button"
                className="fixed bottom-3 right-3 z-40 rounded-md border border-border bg-bg-elevated px-2 py-1 text-xs text-text-secondary"
                onClick={() => setOpen(true)}
            >
                {t('浏览器', 'Browser')}
            </button>
        );
    }

    const running = mode === 'chromium' ? status.running : fusion.running;
    const frame = mode === 'chromium' ? shot : fusionShot;

    return (
        <div className="fixed bottom-3 right-3 z-40 w-72 rounded-lg border border-border bg-bg-elevated p-2 shadow-overlay">
            <div className="flex items-center justify-between pb-1">
                <span className="text-xs font-medium text-text-primary">
                    {t('托管浏览器', 'Managed browser')}{' '}
                    {running ? (mode === 'chromium' ? `:${status.port}` : '· fusion') : ''}
                </span>
                <button
                    type="button"
                    className="text-xs text-text-muted hover:text-text-primary"
                    onClick={() => setOpen(false)}
                >
                    ×
                </button>
            </div>
            <div className="flex gap-1 pb-1">
                <button
                    type="button"
                    className={`rounded px-1.5 py-0.5 text-[10px] ${mode === 'chromium' ? 'bg-bg-muted text-text-primary' : 'text-text-muted'}`}
                    onClick={() => setMode('chromium')}
                >
                    Chromium
                </button>
                <button
                    type="button"
                    className={`rounded px-1.5 py-0.5 text-[10px] ${mode === 'fusion' ? 'bg-bg-muted text-text-primary' : 'text-text-muted'}`}
                    onClick={() => setMode('fusion')}
                >
                    fusion-browser
                </button>
            </div>
            <div className="aspect-video w-full overflow-hidden rounded border border-border bg-bg">
                {running && frame ? (
                    <img
                        src={frame}
                        alt={t('浏览器截图', 'Browser screenshot')}
                        className="h-full w-full object-contain"
                    />
                ) : (
                    <div className="flex h-full items-center justify-center text-[10px] text-text-muted">
                        {running ? t('等待截图…', 'Waiting for screenshot…') : t('未运行', 'Not running')}
                    </div>
                )}
            </div>
            {mode === 'fusion' ? (
                <div className="mt-2 space-y-1">
                    <input
                        className="w-full rounded-md border border-border bg-bg px-2 py-1 font-mono text-[10px] text-text-primary"
                        placeholder={t('运行 ID (可选, profile 归属)', 'Run id (optional)')}
                        value={runId}
                        onChange={(e) => setRunId(e.target.value)}
                    />
                    {running ? (
                        <div className="flex gap-1">
                            <button
                                type="button"
                                className="flex-1 rounded-md border border-border px-2 py-1 text-[10px] text-text-secondary hover:bg-bg-muted"
                                disabled={busy}
                                onClick={() => void fusionSession()}
                            >
                                {t('新会话', 'New session')}
                            </button>
                            <button
                                type="button"
                                className="flex-1 rounded-md border border-border px-2 py-1 text-[10px] text-text-secondary hover:bg-bg-muted"
                                disabled={busy || !sessionId}
                                onClick={() => void fusionImport()}
                            >
                                {t('导入登录态', 'Import login')}
                            </button>
                            <button
                                type="button"
                                className="rounded-md border border-border px-2 py-1 text-[10px] text-text-secondary hover:bg-bg-muted"
                                disabled={busy}
                                onClick={() => void fusionFinish()}
                            >
                                {t('收尾', 'Finish')}
                            </button>
                        </div>
                    ) : (
                        <div className="flex gap-1">
                            <input
                                className="min-w-0 flex-1 rounded-md border border-border bg-bg px-2 py-1 font-mono text-[10px] text-text-primary"
                                value={url}
                                onChange={(e) => setUrl(e.target.value)}
                            />
                            <button
                                type="button"
                                className="rounded-md border border-border px-2 py-1 text-xs text-text-secondary hover:bg-bg-muted"
                                disabled={busy}
                                onClick={() => void fusionLaunch()}
                            >
                                {t('启动', 'Launch')}
                            </button>
                        </div>
                    )}
                    {sessionId ? (
                        <div className="font-mono text-[10px] text-text-muted">
                            {t('会话', 'Session')}: {sessionId}
                        </div>
                    ) : null}
                    {cookies ? <div className="text-[10px] text-text-secondary">{cookies}</div> : null}
                </div>
            ) : running ? (
                <button
                    type="button"
                    className="mt-2 w-full rounded-md border border-border px-2 py-1 text-xs text-text-secondary hover:bg-bg-muted"
                    disabled={busy}
                    onClick={() => void close()}
                >
                    {t('关闭浏览器', 'Close browser')}
                </button>
            ) : (
                <div className="mt-2 flex gap-1">
                    <input
                        className="min-w-0 flex-1 rounded-md border border-border bg-bg px-2 py-1 font-mono text-[10px] text-text-primary"
                        value={url}
                        onChange={(e) => setUrl(e.target.value)}
                    />
                    <button
                        type="button"
                        className="rounded-md border border-border px-2 py-1 text-xs text-text-secondary hover:bg-bg-muted"
                        disabled={busy}
                        onClick={() => void launch()}
                    >
                        {t('启动', 'Launch')}
                    </button>
                </div>
            )}
            {error ? <div className="pt-1 text-[10px] text-error">{error}</div> : null}
        </div>
    );
}
