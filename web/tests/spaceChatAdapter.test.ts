// issue #496: space chat adapter mapping tests (kernel TeamMessage plane
// read model -> @fusion/chat ChatMessage; capability gates).
import { describe, expect, it } from 'bun:test';
import { SpaceChatAdapter, spaceMessageToChat } from '../src/spaceChatAdapter';

describe('spaceMessageToChat', () => {
    it('maps user message fields', () => {
        const chat = spaceMessageToChat({
            id: 'msg_abc123',
            user_id: 'u1',
            content: 'hello @agent_a',
            created_at: '2026-11-16T08:00:00',
        });
        expect(chat.id).toBe('msg_abc123');
        expect(chat.sender).toBe('u1');
        expect(chat.body).toBe('hello @agent_a');
        expect(chat.ts).toBe(Date.parse('2026-11-16T08:00:00'));
    });

    it('maps agent sender + reply threading + mention + artifact refs', () => {
        const chat = spaceMessageToChat({
            id: 'msg_def456',
            user_id: '',
            agent_id: 'agent_a',
            content: 'answer',
            parent_msg_id: 'msg_abc123',
            attachments: ['art_ab12cd34'],
            metadata: { mentions: ['agent_a'] },
        });
        expect(chat.sender).toBe('agent_a');
        expect(chat.replyTo).toBe('msg_abc123');
        expect(chat.mentions).toEqual(['agent_a']);
        expect(chat.attachments).toEqual([{ ref: 'art_ab12cd34' }]);
    });

    it('declares mentions+replies, no attachments/voice/presence', () => {
        const adapter = new SpaceChatAdapter('sp_x');
        expect(adapter.capabilities.mentions).toBe(true);
        expect(adapter.capabilities.replies).toBe(true);
        expect(adapter.capabilities.attachments).toBe(false);
        expect(adapter.capabilities.voice).toBe(false);
        expect(adapter.capabilities.presence).toBe(false);
    });
});
