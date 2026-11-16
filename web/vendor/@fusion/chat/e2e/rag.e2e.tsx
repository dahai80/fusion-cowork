import { renderToStaticMarkup } from 'react-dom/server';
import { ChatView } from '../src/ChatView';
/** E2E evidence script (issue #157): rag adapter against the LIVE fusion-rag
 *  daemon (:11436). Q&A send → subscribed answer + reply-self quote. */
import { RagSessionAdapter } from '../src/adapters/rag';

const BASE = 'http://127.0.0.1:11436';
const KB = 'kb_t';

async function main(): Promise<void> {
    const adapter = new RagSessionAdapter(BASE, `/kb/bases/${KB}/ask`);
    console.log('[e2e:rag] capabilities', adapter.capabilities);

    const got: Array<{ id: string; body: string; replyTo?: string }> = [];
    const unsub = adapter.subscribe((m) =>
        got.push({ id: m.id, body: m.body, replyTo: m.replyTo })
    );

    const questionId = `msg_local_${Date.now()}`;
    const out = await adapter.send({ body: 'hi, reply in one short sentence', replyTo: undefined });
    console.log('[e2e:rag] send#1', out);

    const q1 = questionId;
    const out2 = await adapter.send({ body: 'and now summarize that in five words', replyTo: q1 });
    console.log('[e2e:rag] send#2 (reply-self)', out2);
    unsub();

    const answers = got.filter((m) => m.body);
    console.log('[e2e:rag] received answers', answers);
    if (!out.delivered || !out2.delivered) throw new Error('send not delivered');
    if (answers.length < 2) throw new Error('missing subscribed answers');
    if (!out2.msgId) throw new Error('missing msgId');

    const html = renderToStaticMarkup(<ChatView adapter={adapter} selfId="self" />);
    if (
        html.includes('voice-button') ||
        html.includes('presence-bar') ||
        html.includes('attach file')
    ) {
        throw new Error('capability leak: rag adapter must hide voice/presence/attachments');
    }
    console.log('[e2e:rag] ChatView static render clean — no gated affordances rendered');
    console.log('[e2e:rag] PASS');
    process.exit(0);
}

main().catch((err) => {
    console.error('[e2e:rag] FAIL', err);
    process.exit(1);
});
