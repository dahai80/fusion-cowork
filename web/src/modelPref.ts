// W17: global preferred model — top bar picker and inline pickers share it.
const MODEL_KEY = 'fc-preferred-model';
const MODEL_EVENT = 'fc-preferred-model-change';

export function preferredModel(): string {
    try {
        return window.localStorage.getItem(MODEL_KEY) ?? '';
    } catch {
        return '';
    }
}

export function setPreferredModel(model: string): void {
    try {
        window.localStorage.setItem(MODEL_KEY, model);
    } catch {
        // storage disabled (private mode) — in-memory event still updates the UI
    }
    window.dispatchEvent(new CustomEvent(MODEL_EVENT, { detail: model }));
}

export function onPreferredModelChange(callback: (model: string) => void): () => void {
    const onCustom = (event: Event) => {
        callback((event as CustomEvent<string>).detail ?? '');
    };
    const onStorage = (event: StorageEvent) => {
        if (event.key === MODEL_KEY) callback(event.newValue ?? '');
    };
    window.addEventListener(MODEL_EVENT, onCustom);
    window.addEventListener('storage', onStorage);
    return () => {
        window.removeEventListener(MODEL_EVENT, onCustom);
        window.removeEventListener('storage', onStorage);
    };
}
