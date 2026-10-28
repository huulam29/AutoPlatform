import { Handle, Position, type NodeProps } from "@xyflow/react";
import type { FlowNode } from "../lib/graph";
import { colorGroup } from "../lib/runs";
import { Icon, Status, type IconName } from "./Icon";

const KIND_ICON: Record<string, IconName> = {
  agent: "agent",
  action: "action",
  router: "router",
  gate: "gate",
  wait: "wait",
  chat: "chat",
};

export function PipelineNode({ data, selected }: NodeProps<FlowNode>) {
  const status = data.status;
  const cls = [
    "pnode",
    `pnode-${data.nodeType}`,
    status ? `status-${status} run-${colorGroup(status, data.nodeType)}` : "",
    data.issues.length ? "has-issues" : "",
    selected ? "selected" : "",
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <div className={cls} title={data.issues.map((i) => i.message).join("\n") || undefined}>
      <Handle type="target" position={Position.Top} id="in" />
      <Handle type="target" position={Position.Right} id="back-in" className="handle-back" />
      <div className="pnode-head">
        <span className="pnode-kind">
          <Icon name={KIND_ICON[data.nodeType] ?? "circleDashed"} />
          {data.nodeType}
        </span>
        {data.start && (
          <span className="badge badge-start" title="the start point: takes the run's input">
            start
          </span>
        )}
        {data.issues.length > 0 && (
          <span className="badge badge-issue" aria-label={`${data.issues.length} issues`}>
            {data.issues.length}
          </span>
        )}
        {data.iterations !== undefined && data.iterations > 1 && (
          <span className="badge badge-iter" title="iterations">
            ×{data.iterations}
          </span>
        )}
      </div>
      <div className="pnode-id">{data.id}</div>
      {data.summary && <div className="pnode-summary">{data.summary}</div>}
      {data.ai && (data.ai.harness || data.ai.model) && (
        <div
          className="pnode-ai"
          title={
            [
              data.ai.harness && `harness ${data.ai.harness}${data.ai.harnessInherited ? " (inherited)" : ""}`,
              data.ai.model && `model ${data.ai.model}${data.ai.modelInherited ? " (inherited)" : ""}`,
            ]
              .filter(Boolean)
              .join(", ") || undefined
          }
        >
          {data.ai.harness && (
            <span className={data.ai.harnessInherited ? "inherited" : undefined}>{data.ai.harness}</span>
          )}
          {data.ai.harness && data.ai.model && <span className="sep"> · </span>}
          {data.ai.model && (
            <span className={data.ai.modelInherited ? "inherited" : undefined}>{data.ai.model}</span>
          )}
        </div>
      )}
      {status && (
        <div className="pnode-status">
          <Status s={status} nodeType={data.nodeType} />
          {data.tasks && data.tasks.total > 1 && (
            <span
              className="pnode-tasks"
              title={`tasks: ${data.tasks.running} running, ${data.tasks.done} done, ${data.tasks.failed} failed, ${data.tasks.pending} pending`}
            >
              {data.tasks.done}/{data.tasks.total}
            </span>
          )}
        </div>
      )}
      {status && data.tasks && data.tasks.total > 1 && (
        <div className="pnode-taskbar" aria-hidden>
          {Array.from({ length: Math.min(data.tasks.total, 24) }, (_, i) => {
            const t = data.tasks!;
            const cls = i < t.done ? "d" : i < t.done + t.failed ? "f" : i < t.done + t.failed + t.running ? "r" : "";
            return <i key={i} className={cls} />;
          })}
        </div>
      )}
      <Handle type="source" position={Position.Bottom} id="out" />
      <Handle type="source" position={Position.Right} id="back-out" className="handle-back" />
    </div>
  );
}

export const nodeTypes = { pipeline: PipelineNode };
