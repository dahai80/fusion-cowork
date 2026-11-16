import type { ChatCapabilities, ChatMember, ChatMessage, SendPayload } from './types';

export interface MentionQuery {
    active: boolean;
    start: number;
    query: string;
}

/** Caret-aware @-trigger detection: active when the token between the last
 *  '@' and the caret has no whitespace. */
export function detectMention(text: string, caret: number): MentionQuery {
    const upto = text.slice(0, caret);
    const at = upto.lastIndexOf('@');
    if (at === -1) return { active: false, start: 0, query: '' };
    const query = upto.slice(at + 1);
    if (/\s/.test(query)) return { active: false, start: 0, query: '' };
    return { active: true, start: at, query };
}

export function filterMembers(members: ChatMember[], query: string): ChatMember[] {
    const q = query.toLowerCase();
    return members.filter((m) => m.name.toLowerCase().includes(q)).slice(0, 5);
}

/** Pure send-payload assembly so capability gating is testable without DOM
 *  interaction: mentions only when declared and resolvable; replyTo only when
 *  the replies capability allows it. */
export function buildSendPayload(
    text: string,
    capabilities: ChatCapabilities,
    members: ChatMember[],
    replyTo: ChatMessage | null
): SendPayload {
    const body = text.trim();
    if (!body) return { body: '' };
    const mentions = capabilities.mentions
        ? [...body.matchAll(/@([\w-]+)/g)]
              .map((m) => m[1])
              .filter(
                  (n, i, arr): n is string =>
                      n !== undefined && members.some((m) => m.name === n) && arr.indexOf(n) === i
              )
        : undefined;
    const repliesAllowed = capabilities.replies === true || capabilities.replies === 'self';
    return {
        body,
        replyTo: repliesAllowed && replyTo ? replyTo.id : undefined,
        mentions: mentions?.length ? mentions : undefined,
    };
}
