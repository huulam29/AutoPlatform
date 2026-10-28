// A small line diff (LCS) for the History and assistant "Show changes" views.

export type DiffLine = { op: " " | "+" | "-"; text: string };

const MAX_CELLS = 4_000_000; // beyond this (2000 x 2000 lines), a plain replace is shown

export function lineDiff(before: string, after: string): DiffLine[] {
  const a = before.split("\n");
  const b = after.split("\n");
  // trim the common head and tail first: most edits are small
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) start++;
  let endA = a.length;
  let endB = b.length;
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) {
    endA--;
    endB--;
  }
  const head: DiffLine[] = a.slice(0, start).map((text) => ({ op: " ", text }));
  const tail: DiffLine[] = a.slice(endA).map((text) => ({ op: " ", text }));
  const x = a.slice(start, endA);
  const y = b.slice(start, endB);
  if (x.length * y.length > MAX_CELLS) {
    return [
      ...head,
      ...x.map((text) => ({ op: "-" as const, text })),
      ...y.map((text) => ({ op: "+" as const, text })),
      ...tail,
    ];
  }
  // lcs[i][j] = LCS length of x[i:] and y[j:]
  const lcs: number[][] = Array.from({ length: x.length + 1 }, () => new Array<number>(y.length + 1).fill(0));
  for (let i = x.length - 1; i >= 0; i--) {
    for (let j = y.length - 1; j >= 0; j--) {
      lcs[i][j] = x[i] === y[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }
  const mid: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < x.length && j < y.length) {
    if (x[i] === y[j]) {
      mid.push({ op: " ", text: x[i] });
      i++;
      j++;
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      mid.push({ op: "-", text: x[i++] });
    } else {
      mid.push({ op: "+", text: y[j++] });
    }
  }
  while (i < x.length) mid.push({ op: "-", text: x[i++] });
  while (j < y.length) mid.push({ op: "+", text: y[j++] });
  return [...head, ...mid, ...tail];
}

/** Changed lines with `context` unchanged lines around them; gaps become null. */
export function withContext(lines: DiffLine[], context = 3): (DiffLine | null)[] {
  const keep = new Array<boolean>(lines.length).fill(false);
  lines.forEach((l, k) => {
    if (l.op === " ") return;
    for (let d = -context; d <= context; d++) if (k + d >= 0 && k + d < lines.length) keep[k + d] = true;
  });
  const out: (DiffLine | null)[] = [];
  lines.forEach((l, k) => {
    if (keep[k]) out.push(l);
    else if (out[out.length - 1] !== null) out.push(null); // one marker per hidden run
  });
  return out;
}

export function diffStats(lines: DiffLine[]): { added: number; removed: number } {
  return {
    added: lines.filter((l) => l.op === "+").length,
    removed: lines.filter((l) => l.op === "-").length,
  };
}
