import { useEffect, useRef, useState } from 'react';
import { type SpaceNotification, api } from './api';
import { useT } from './i18n';

// W15: notification center — top-bar bell with unread count (30s poll) and
// dropdown list with per-item mark-read. Standalone header only (embedded
// mode: the shell owns the chrome).
export function NotificationBell() {
    const t = useT();
    const [open, setOpen] = useState(false);
    const [items, setItems] = useState<SpaceNotification[]>([]);
    const [unread, setUnread] = useState(0);
    const rootRef = useRef<HTMLDivElement | null>(null);

    useEffect(() => {
        let stopped = false;
        async function tick() {
            try {
                const list = await api.spaceNotifications(false);
                if (stopped) return;
                setItems(list);
                setUnread(list.filter((n) => !n.read).length);
            } catch {
                // bell is best-effort — silent skip (space API may be absent)
            }
        }
        void tick();
        const timer = window.setInterval(() => void tick(), 30000);
        return () => {
            stopped = true;
            window.clearInterval(timer);
        };
    }, []);

    useEffect(() => {
        if (!open) return;
        function onDocClick(e: MouseEvent) {
            if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
        }
        document.addEventListener('mousedown', onDocClick);
        return () => document.removeEventListener('mousedown', onDocClick);
    }, [open]);

    async function markRead(nid: string) {
        try {
            await api.markSpaceNotificationRead(nid);
            setItems((prev) => prev.map((n) => (n.id === nid ? { ...n, read: 1 } : n)));
            setUnread((u) => Math.max(0, u - 1));
        } catch (e) {
            console.error('[cowork-web] notification mark-read failed', e);
        }
    }

    return (
        <div className="relative" ref={rootRef}>
            <button
                type="button"
                className="relative rounded-md px-2 py-1 text-sm text-text-secondary hover:bg-bg-muted"
                aria-label={t('通知', 'Notifications')}
                onClick={() => setOpen((o) => !o)}
            >
                🔔
                {unread > 0 ? (
                    <span className="absolute -right-0.5 -top-0.5 rounded-full bg-error px-1 text-[9px] leading-3 text-white">
                        {unread > 9 ? '9+' : unread}
                    </span>
                ) : null}
            </button>
            {open ? (
                <div className="absolute right-0 z-20 mt-1 max-h-80 w-72 overflow-y-auto rounded-md border border-border bg-bg-elevated p-2 shadow-lg">
                    <div className="pb-1 text-xs font-medium text-text-primary">{t('通知', 'Notifications')}</div>
                    {items.length === 0 ? (
                        <div className="py-2 text-xs text-text-muted">{t('暂无通知', 'No notifications')}</div>
                    ) : (
                        <div className="space-y-1">
                            {items.map((n) => (
                                <div
                                    key={n.id}
                                    className={`rounded-md border px-2 py-1.5 text-xs ${
                                        n.read ? 'border-border bg-bg text-text-muted' : 'border-brand/40 bg-bg'
                                    }`}
                                >
                                    <div className="flex items-center justify-between gap-2">
                                        <span className="min-w-0 flex-1 truncate font-medium text-text-primary">
                                            {n.title}
                                        </span>
                                        {n.read ? null : (
                                            <button
                                                type="button"
                                                className="shrink-0 rounded px-1 text-[10px] text-brand hover:bg-bg-muted"
                                                onClick={() => void markRead(n.id)}
                                            >
                                                {t('已读', 'Mark read')}
                                            </button>
                                        )}
                                    </div>
                                    {n.content ? (
                                        <div className="mt-0.5 line-clamp-2 text-text-secondary">{n.content}</div>
                                    ) : null}
                                </div>
                            ))}
                        </div>
                    )}
                </div>
            ) : null}
        </div>
    );
}
