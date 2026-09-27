/** One row of a numbered task list, ready to enqueue as its own run. */
export interface SplitTaskItem {
  index: number;
  title: string;
  goal: string;
}

const NUMBERED_LINE = /^[ \t]*(?:(\d{1,3})[.、．)]|（(\d{1,3})）)\s*(\S.*?)\s*$/;

/**
 * Split a prompt on a numbered list (1. / 1、 / （1）).
 * Text before the first item is kept on every row, so each queued run still
 * has the shared rules. Fewer than two items returns an empty list.
 */
export function splitNumberedTask(raw: string): SplitTaskItem[] {
  const lines = raw.split(/\r?\n/);
  const preambleLines: string[] = [];
  const items: { title: string; body: string[] }[] = [];
  let current: { title: string; body: string[] } | null = null;

  for (const line of lines) {
    const match = line.match(NUMBERED_LINE);
    if (match) {
      if (current) {
        items.push(current);
      }
      current = { title: (match[3] || '').trim(), body: [] };
      continue;
    }
    if (current) {
      current.body.push(line);
    } else {
      preambleLines.push(line);
    }
  }
  if (current) {
    items.push(current);
  }
  if (items.length < 2) {
    return [];
  }

  const preamble = preambleLines.join('\n').trim();
  return items.map((item, offset) => {
    const detail = item.body.join('\n').trim();
    const parts = [
      preamble,
      '只执行下面这一条，做完就结束。不要做清单里的其他事项。迷路时只按返回，不要重新打开应用。',
      item.title,
      detail
    ].filter((part) => part.length > 0);
    const title = item.title.length > 72 ? `${item.title.slice(0, 72)}…` : item.title;
    return {
      index: offset + 1,
      title,
      goal: parts.join('\n')
    };
  });
}
