import { useEffect, useState } from 'react';
import { type BrowserStatus, api } from './api';
import { useT } from './i18n';

// issue #153: 托管浏览器侧栏 — 实时截图轮询 (2s) + 启动/关闭。运行页可见,
// 由工作流里的 managed_browser 节点或本侧栏启动的会话共用同一 CDP 端口。

const STATUS_MS = 3000;
const SHOT_MS = 2000;

export function BrowserPanel() {
    const t = useT();
    const [status, setStatus] = useState<BrowserStatus>({ running: false });
    const [shot, setShot] = useState('');
    const [url, setUrl] = useState('https://');
    const [open, setOpen] = useState(true);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');

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
        if (!status.running || !open) {
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
    }, [status.running, open]);

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

    return (
        <div className="fixed bottom-3 right-3 z-40 w-72 rounded-lg border border-border bg-bg-elevated p-2 shadow-overlay">
            <div className="flex items-center justify-between pb-1">
                <span className="text-xs font-medium text-text-primary">
                    {t('托管浏览器', 'Managed browser')} {status.running ? `:${status.port}` : ''}
                </span>
                <button
                    type="button"
                    className="text-xs text-text-muted hover:text-text-primary"
                    onClick={() => setOpen(false)}
                >
                    ×
                </button>
            </div>
            <div className="aspect-video w-full overflow-hidden rounded border border-border bg-bg">
                {status.running && shot ? (
                    <img
                        src={shot}
                        alt={t('浏览器截图', 'Browser screenshot')}
                        className="h-full w-full object-contain"
                    />
                ) : (
                    <div className="flex h-full items-center justify-center text-[10px] text-text-muted">
                        {status.running ? t('等待截图…', 'Waiting for screenshot…') : t('未运行', 'Not running')}
                    </div>
                )}
            </div>
            {status.running ? (
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
