import type { SystemDirectory } from './api';

// issue #251: 新建工作目录的候选 = 服务端系统目录 − 已被同名工作区占用; 空名剔除
export function selectableSystemDirs(
    directories: SystemDirectory[] | undefined,
    usedNames: ReadonlySet<string>
): SystemDirectory[] {
    return (directories ?? []).filter((d) => d.name && !usedNames.has(d.name));
}
