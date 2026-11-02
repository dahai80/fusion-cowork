import type { SystemDirectory } from './api';

// issue #251: 新建工作目录的候选 = 服务端系统目录 − 已被同名工作区占用; 空名剔除
export function selectableSystemDirs(
    directories: SystemDirectory[] | undefined,
    usedNames: ReadonlySet<string>
): SystemDirectory[] {
    return (directories ?? []).filter((d) => d.name && !usedNames.has(d.name));
}

// issue #251 弹窗面包屑: "a/b/c" → ["a","b","c"] (根为空串 → [])
export function breadcrumbSegments(relative: string): string[] {
    return relative ? relative.split('/').filter(Boolean) : [];
}

// issue #251: 选中目录的空间名 = 相对路径末段 (根目录用 root_name 兜底)
export function dirBasename(relative: string, rootName: string): string {
    const segs = breadcrumbSegments(relative);
    return segs.length ? segs[segs.length - 1] : rootName;
}
