import { useEffect, useRef, useState } from 'react';
import { type SearchItem, api } from './api';
import { useT } from './i18n';

export function CommandPalette() {
    const t = useT();
    const [open, setOpen] = useState(false);
    const [query, setQuery] = useState('');
    const [items, setItems] = useState<SearchItem[]>([]);
    const [active, setActive] = useState(0);
    const [error, setError] = useState('');
    const inputRef = useRef<HTMLInputElement | null>(null);

    useEffect(() => {
        function onKey(e: KeyboardEvent) {
            if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
                e.preventDefault();
                setOpen((v) => !v);
            } else if (e.key === 'Escape') {
                setOpen(false);
            }
        }
        window.addEventListener('keydown', onKey);
        return () => window.removeEventListener('keydown', onKey);
    }, []);

    useEffect(() => {
        if (open) {
            setActive(0);
            setTimeout(() => inputRef.current?.focus(), 0);
        } else {
            setQuery('');
            setItems([]);
            setError('');
        }
    }, [open]);

    useEffect(() => {
        const q = query.trim();
        if (!q) {
            setItems([]);
            return;
        }
        let stale = false;
        const timer = setTimeout(() => {
            api.search(q)
                .then((res) => {
                    if (stale) return;
                    setItems(res.items ?? []);
                    setActive(0);
                    setError('');
                })
                .catch((e) => {
                    if (!stale) setError(e instanceof Error ? e.message : String(e));
                });
        }, 180);
        return () => {
            stale = true;
            clearTimeout(timer);
        };
    }, [query]);

    function go(item: SearchItem) {
        setOpen(false);
        window.location.hash = `#${item.route}`;
    }

    if (!open) return null;

    return (
        <div
            role="presentation"
            className="fixed inset-0 z-50 flex items-start justify-center bg-black/30 p-4 pt-24"
            onClick={(e) => {
                if (e.target === e.currentTarget) setOpen(false);
            }}
            onKeyDown={(e) => {
                if (e.key === 'Escape') setOpen(false);
            }}
        >
            <div
                role="dialog"
                aria-modal="true"
                aria-label={t('全局搜索', 'Global search')}
                className="w-full max-w-xl overflow-hidden rounded-2xl border border-border bg-bg-elevated shadow-md shadow-zinc-900/10"
            >
                <input
                    ref={inputRef}
                    className="w-full border-b border-border bg-transparent px-4 py-3 text-sm text-text-primary outline-none"
                    placeholder={t('搜索工作流 / 运行 / 空间 / 能力…', 'Search workflows, runs, spaces, capabilities…')}
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    onKeyDown={(e) => {
                        if (e.key === 'ArrowDown') {
                            e.preventDefault();
                            setActive((i) => Math.min(i + 1, items.length - 1));
                        } else if (e.key === 'ArrowUp') {
                            e.preventDefault();
                            setActive((i) => Math.max(i - 1, 0));
                        } else if (e.key === 'Enter' && items[active]) {
                            go(items[active]);
                        }
                    }}
                />
                {error ? <div className="px-4 py-3 text-xs text-error">{error}</div> : null}
                {!error && query.trim() !== '' && items.length === 0 ? (
                    <div className="px-4 py-3 text-xs text-text-muted">{t('无匹配结果', 'No matches')}</div>
                ) : null}
                <ul className="max-h-80 overflow-auto">
                    {items.map((item, i) => (
                        <li key={`${item.kind}:${item.route}:${item.title}:${i}`}>
                            <button
                                type="button"
                                className={`flex w-full items-center gap-2 px-4 py-2 text-left text-sm ${
                                    i === active ? 'bg-bg-muted text-text-primary' : 'text-text-primary'
                                }`}
                                onMouseEnter={() => setActive(i)}
                                onClick={() => go(item)}
                            >
                                <span className="rounded-md border border-border px-1.5 py-0.5 text-[10px] text-text-muted">
                                    {item.kind}
                                </span>
                                <span className="truncate">{item.title}</span>
                            </button>
                        </li>
                    ))}
                </ul>
            </div>
        </div>
    );
}
