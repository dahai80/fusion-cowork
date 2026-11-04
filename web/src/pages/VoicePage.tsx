// Voice panel (issue #278): fusion-speech WS ingress client — mic STT + TTS playback
import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api';

type AsrEvent = { event?: string; state?: string; text?: string; session?: string };

const TARGET_RATE = 16000;

export default function VoicePage() {
    const [status, setStatus] = useState<string>('');
    const [connState, setConnState] = useState<'idle' | 'connecting' | 'live'>('idle');
    const [finals, setFinals] = useState<string[]>([]);
    const [interim, setInterim] = useState('');
    const [speakText, setSpeakText] = useState('');
    const [error, setError] = useState('');
    const wsRef = useRef<WebSocket | null>(null);
    const audioCtxRef = useRef<AudioContext | null>(null);
    const streamRef = useRef<MediaStream | null>(null);
    const nodeRef = useRef<ScriptProcessorNode | null>(null);
    const playCtxRef = useRef<AudioContext | null>(null);

    useEffect(() => {
        void (async () => {
            try {
                const h = await api.speechHealth();
                setStatus(
                    h.status === 'ok'
                        ? `fusion-speech ${h.version ?? ''} · stt=${h.stt_model ?? '?'} · tts=${h.tts_model ?? '?'}`
                        : 'daemon down — start fusion-speech first'
                );
            } catch {
                setStatus('daemon unreachable');
            }
        })();
        document.title = 'Voice — Cowork';
        return () => {
            stopSession();
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    const handleEvent = useCallback((frame: AsrEvent) => {
        const ev = frame.event ?? '';
        if (ev === 'speech.asr') {
            if (frame.state === 'final' && frame.text) {
                setFinals((prev) => [...prev.slice(-49), frame.text as string]);
                setInterim('');
            } else if (frame.text) {
                setInterim(frame.text);
            }
        } else if (ev === 'speech.session.error') {
            setError(String((frame as Record<string, unknown>).error ?? 'session error'));
        }
    }, []);

    const startSession = useCallback(async () => {
        setError('');
        try {
            const cfg = await api.speechConfig();
            const wsUrl = cfg.token ? `${cfg.url}?token=${encodeURIComponent(cfg.token)}` : cfg.url;
            const ws = new WebSocket(wsUrl);
            wsRef.current = ws;
            setConnState('connecting');
            ws.onopen = () => {
                ws.send(JSON.stringify({ type: 'start', mode: 'full_duplex', encoding: 'int16', tts_sink: 'ws' }));
                setConnState('live');
            };
            ws.onmessage = (msg) => {
                if (typeof msg.data === 'string') {
                    try {
                        handleEvent(JSON.parse(msg.data) as AsrEvent);
                    } catch {
                        /* ignore malformed control frames */
                    }
                    return;
                }
                // binary int16 TTS PCM @ 24k — play through speakers
                void playPcm(msg.data as ArrayBuffer);
            };
            ws.onerror = () => setError('WebSocket error — check fusion-speech daemon');
            ws.onclose = () => {
                setConnState('idle');
                stopMic();
            };

            // mic capture → 16k mono int16 frames
            const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            streamRef.current = stream;
            const ctx = new AudioContext();
            audioCtxRef.current = ctx;
            const src = ctx.createMediaStreamSource(stream);
            const node = ctx.createScriptProcessor(4096, 1, 1);
            nodeRef.current = node;
            node.onaudioprocess = (e) => {
                if (ws.readyState !== WebSocket.OPEN) return;
                const input = e.inputBuffer.getChannelData(0);
                ws.send(resampleToInt16(input, ctx.sampleRate, TARGET_RATE));
            };
            src.connect(node);
            node.connect(ctx.destination);
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
            setConnState('idle');
        }
    }, [handleEvent]);

    const playPcm = useCallback(async (buf: ArrayBuffer) => {
        if (!playCtxRef.current) playCtxRef.current = new AudioContext({ sampleRate: 24000 });
        const ctx = playCtxRef.current;
        await ctx.resume();
        const i16 = new Int16Array(buf);
        if (i16.length === 0) return;
        const f32 = new Float32Array(i16.length);
        for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768;
        const audioBuf = ctx.createBuffer(1, f32.length, ctx.sampleRate);
        audioBuf.copyToChannel(f32, 0);
        const source = ctx.createBufferSource();
        source.buffer = audioBuf;
        source.connect(ctx.destination);
        source.start();
    }, []);

    const stopMic = useCallback(() => {
        nodeRef.current?.disconnect();
        nodeRef.current = null;
        streamRef.current?.getTracks().forEach((t) => t.stop());
        streamRef.current = null;
        void audioCtxRef.current?.close();
        audioCtxRef.current = null;
    }, []);

    const stopSession = useCallback(() => {
        const ws = wsRef.current;
        if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'stop' }));
        ws?.close();
        wsRef.current = null;
        stopMic();
        setConnState('idle');
    }, [stopMic]);

    const speak = () => {
        const ws = wsRef.current;
        if (!ws || ws.readyState !== WebSocket.OPEN || !speakText.trim()) return;
        ws.send(JSON.stringify({ type: 'speak', text: speakText.trim() }));
    };

    return (
        <div className="mx-auto max-w-4xl space-y-4 p-4">
            <h1 className="text-lg font-semibold text-zinc-900 dark:text-zinc-100">语音面板</h1>
            <div className="text-xs text-zinc-500 dark:text-zinc-400">{status}</div>
            {error && (
                <div className="rounded-xl border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300">
                    {error}
                </div>
            )}

            <div className="rounded-2xl border border-zinc-200/80 bg-white p-4 shadow-sm dark:border-zinc-700 dark:bg-zinc-900">
                <div className="flex items-center justify-between">
                    <span className="text-sm font-medium text-zinc-700 dark:text-zinc-300">
                        麦克风会话{' '}
                        <span
                            className={
                                connState === 'live' ? 'text-emerald-600 dark:text-emerald-400' : 'text-zinc-400'
                            }
                        >
                            {connState === 'live' ? '● live' : connState === 'connecting' ? '连接中…' : '未连接'}
                        </span>
                    </span>
                    {connState === 'live' ? (
                        <button
                            type="button"
                            onClick={stopSession}
                            className="rounded-xl border border-zinc-200/80 px-4 py-2 text-sm shadow-sm dark:border-zinc-700"
                        >
                            停止
                        </button>
                    ) : (
                        <button
                            type="button"
                            onClick={() => void startSession()}
                            className="rounded-xl bg-indigo-600 px-4 py-2 text-sm font-medium text-white shadow-sm"
                        >
                            开始收音
                        </button>
                    )}
                </div>
                <p className="mt-2 text-xs text-zinc-400">
                    浏览器采集 16k PCM 推送到 fusion-speech WS ingress; TTS 回包自动播放。
                </p>
            </div>

            <div className="rounded-2xl border border-zinc-200/80 bg-white p-4 shadow-sm dark:border-zinc-700 dark:bg-zinc-900">
                <div className="mb-2 text-sm font-medium text-zinc-700 dark:text-zinc-300">识别结果</div>
                {interim && <div className="text-xs italic text-zinc-400">{interim}</div>}
                <ul className="space-y-1 text-sm dark:text-zinc-200">
                    {finals.map((f, i) => (
                        <li key={i} className="border-b border-zinc-100 py-1 dark:border-zinc-800">
                            {f}
                        </li>
                    ))}
                    {finals.length === 0 && !interim && (
                        <li className="text-xs text-zinc-400">暂无 — 开始收音后说话</li>
                    )}
                </ul>
            </div>

            <div className="rounded-2xl border border-zinc-200/80 bg-white p-4 shadow-sm dark:border-zinc-700 dark:bg-zinc-900">
                <div className="mb-2 text-sm font-medium text-zinc-700 dark:text-zinc-300">TTS 合成</div>
                <div className="flex gap-2">
                    <input
                        value={speakText}
                        onChange={(e) => setSpeakText(e.target.value)}
                        placeholder="要朗读的文本"
                        className="flex-1 rounded-xl border border-zinc-200/80 bg-white px-3 py-2 text-sm dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-100"
                    />
                    <button
                        type="button"
                        disabled={connState !== 'live' || !speakText.trim()}
                        onClick={speak}
                        className="rounded-xl bg-indigo-600 px-4 py-2 text-sm font-medium text-white shadow-sm disabled:opacity-50"
                    >
                        朗读
                    </button>
                </div>
            </div>
        </div>
    );
}

function resampleToInt16(input: Float32Array, fromRate: number, toRate: number): ArrayBuffer {
    if (fromRate === toRate) return floatToInt16(input);
    const ratio = fromRate / toRate;
    const outLen = Math.floor(input.length / ratio);
    const out = new Float32Array(outLen);
    for (let i = 0; i < outLen; i++) {
        const pos = i * ratio;
        const idx = Math.floor(pos);
        const frac = pos - idx;
        const a = input[idx] ?? 0;
        const b = input[idx + 1] ?? a;
        out[i] = a + (b - a) * frac;
    }
    return floatToInt16(out);
}

function floatToInt16(samples: Float32Array): ArrayBuffer {
    const buf = new ArrayBuffer(samples.length * 2);
    const view = new DataView(buf);
    for (let i = 0; i < samples.length; i++) {
        const s = Math.max(-1, Math.min(1, samples[i]));
        view.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return buf;
}
