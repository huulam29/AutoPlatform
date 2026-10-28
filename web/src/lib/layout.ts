// Automatic left-to-right layered layout. No stored positions: the YAML is the only source of
// truth, so the graph is laid out from scratch on every load.
//
// 1. Back edges: edges with `max_loops` (bounded loops) first, then any edge that closes a
//    cycle in a DFS over the rest (an invalid pipeline still gets drawn).
// 2. Layers: longest path over the forward edges (a node sits right of all its predecessors).
// 3. Order inside a layer: start from file order, then a few barycenter sweeps to cut crossings.
// 4. Coordinates: fixed column width, rows centered per layer.

export interface LayoutEdge {
  src: string;
  dst: string;
  max_loops?: number | null;
}

export interface LayoutOptions {
  nodeWidth?: number;
  nodeHeight?: number;
  gapX?: number;
  gapY?: number;
  sweeps?: number;
}

export interface Layout {
  positions: Record<string, { x: number; y: number }>;
  layers: Record<string, number>;
  order: string[][]; // node ids per layer, top to bottom
  backEdges: Set<number>; // indexes into the edges array
}

export function findBackEdges(nodes: string[], edges: LayoutEdge[]): Set<number> {
  const known = new Set(nodes);
  const back = new Set<number>();
  edges.forEach((e, i) => {
    if (e.max_loops != null || e.src === e.dst) back.add(i);
  });
  const out = new Map<string, number[]>();
  nodes.forEach((n) => out.set(n, []));
  edges.forEach((e, i) => {
    if (!back.has(i) && known.has(e.src) && known.has(e.dst)) out.get(e.src)!.push(i);
  });
  const indeg = new Map<string, number>(nodes.map((n) => [n, 0]));
  edges.forEach((e, i) => {
    if (!back.has(i) && known.has(e.src) && known.has(e.dst))
      indeg.set(e.dst, (indeg.get(e.dst) ?? 0) + 1);
  });
  const state = new Map<string, 0 | 1 | 2>(); // 0 new, 1 on stack, 2 done
  const visit = (start: string) => {
    // iterative DFS so huge graphs can't blow the stack
    const stack: { node: string; next: number }[] = [{ node: start, next: 0 }];
    state.set(start, 1);
    while (stack.length) {
      const top = stack[stack.length - 1];
      const outs = out.get(top.node)!;
      if (top.next >= outs.length) {
        state.set(top.node, 2);
        stack.pop();
        continue;
      }
      const i = outs[top.next++];
      const dst = edges[i].dst;
      const s = state.get(dst) ?? 0;
      if (s === 1) back.add(i);
      else if (s === 0) {
        state.set(dst, 1);
        stack.push({ node: dst, next: 0 });
      }
    }
  };
  for (const n of nodes) if ((indeg.get(n) ?? 0) === 0 && !state.get(n)) visit(n);
  for (const n of nodes) if (!state.get(n)) visit(n);
  return back;
}

export function assignLayers(
  nodes: string[],
  edges: LayoutEdge[],
  back: Set<number>,
): Record<string, number> {
  const known = new Set(nodes);
  const fwd = edges.filter(
    (e, i) => !back.has(i) && known.has(e.src) && known.has(e.dst) && e.src !== e.dst,
  );
  const indeg = new Map<string, number>(nodes.map((n) => [n, 0]));
  fwd.forEach((e) => indeg.set(e.dst, indeg.get(e.dst)! + 1));
  const layer: Record<string, number> = {};
  nodes.forEach((n) => (layer[n] = 0));
  const queue = nodes.filter((n) => indeg.get(n) === 0);
  while (queue.length) {
    const n = queue.shift()!;
    for (const e of fwd) {
      if (e.src !== n) continue;
      layer[e.dst] = Math.max(layer[e.dst], layer[n] + 1);
      indeg.set(e.dst, indeg.get(e.dst)! - 1);
      if (indeg.get(e.dst) === 0) queue.push(e.dst);
    }
  }
  return layer;
}

function barycenter(
  order: string[][],
  edges: LayoutEdge[],
  back: Set<number>,
  sweeps: number,
): string[][] {
  const pos = new Map<string, number>();
  const index = () => order.forEach((l) => l.forEach((n, i) => pos.set(n, i)));
  index();
  const fwd = edges.filter((_, i) => !back.has(i));
  for (let s = 0; s < sweeps; s++) {
    const down = s % 2 === 0;
    const range = down
      ? order.map((_, i) => i).slice(1)
      : order.map((_, i) => i).slice(0, -1).reverse();
    for (const li of range) {
      const score = new Map<string, number>();
      order[li].forEach((n, i) => {
        const nbrs = fwd
          .filter((e) => (down ? e.dst === n : e.src === n))
          .map((e) => pos.get(down ? e.src : e.dst))
          .filter((x): x is number => x !== undefined);
        score.set(n, nbrs.length ? nbrs.reduce((a, b) => a + b, 0) / nbrs.length : i);
      });
      order[li] = [...order[li]].sort((a, b) => score.get(a)! - score.get(b)!);
      index();
    }
  }
  return order;
}

export function layout(nodes: string[], edges: LayoutEdge[], opts: LayoutOptions = {}): Layout {
  // Top to bottom: layers are rows (y), nodes within a layer spread across (x).
  const { nodeWidth = 200, nodeHeight = 96, gapX = 40, gapY = 48, sweeps = 4 } = opts;
  const backEdges = findBackEdges(nodes, edges);
  const layers = assignLayers(nodes, edges, backEdges);
  const depth = nodes.length ? Math.max(...nodes.map((n) => layers[n])) + 1 : 0;
  let order: string[][] = Array.from({ length: depth }, () => []);
  nodes.forEach((n) => order[layers[n]].push(n));
  order = barycenter(order, edges, backEdges, sweeps);
  const widest = Math.max(0, ...order.map((l) => l.length));
  const positions: Layout["positions"] = {};
  order.forEach((l, li) => {
    const offset = ((widest - l.length) * (nodeWidth + gapX)) / 2;
    l.forEach((n, i) => {
      positions[n] = { x: offset + i * (nodeWidth + gapX), y: li * (nodeHeight + gapY) };
    });
  });
  return { positions, layers, order, backEdges };
}
