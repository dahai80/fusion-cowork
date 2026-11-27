import type { SystemDirectory } from './api';

// issue #251: candidates for a new working directory = server system directories − names already taken by workspaces; empty names dropped
export function selectableSystemDirs(
    directories: SystemDirectory[] | undefined,
    usedNames: ReadonlySet<string>
): SystemDirectory[] {
    return (directories ?? []).filter((d) => d.name && !usedNames.has(d.name));
}

// issue #251 dialog breadcrumbs: "a/b/c" → ["a","b","c"] (root is the empty string → [])
export function breadcrumbSegments(relative: string): string[] {
    return relative ? relative.split('/').filter(Boolean) : [];
}

// issue #251: workspace name for the selected directory = last segment of the relative path (root falls back to root_name)
export function dirBasename(relative: string, rootName: string): string {
    const segs = breadcrumbSegments(relative);
    return segs.length ? segs[segs.length - 1] : rootName;
}
