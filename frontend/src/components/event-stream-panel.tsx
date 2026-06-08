import type { StageName, WorkspaceEvent } from "@/components/workspace-types";

type EventStreamPanelProps = {
  events: WorkspaceEvent[];
  selectedStage: StageName | null;
  filterMode: "all" | "stage";
  onChangeFilterMode: (mode: "all" | "stage") => void;
  expandedEventKeys: string[];
  onToggleEvent: (key: string) => void;
};

function formatEventTime(input?: string): string {
  if (!input) return "--:--:--";
  const timeText = input.split("T")[1] || input;
  return timeText.slice(0, 8);
}

function buildEventKey(event: WorkspaceEvent, index: number): string {
  if (typeof event.event_id === "number") return `event:${event.event_id}`;
  return `${event.created_at || "unknown"}:${event.stage || "none"}:${event.event_type || "event"}:${index}`;
}

function buildPreview(payload?: Record<string, unknown>): string {
  if (!payload) return "";
  try {
    const text = JSON.stringify(payload);
    return text.length > 120 ? `${text.slice(0, 120)}...` : text;
  } catch {
    return String(payload);
  }
}

export function EventStreamPanel({
  events,
  selectedStage,
  filterMode,
  onChangeFilterMode,
  expandedEventKeys,
  onToggleEvent,
}: EventStreamPanelProps) {
  const filteredEvents =
    filterMode === "stage" && selectedStage
      ? events.filter((item) => item.stage === selectedStage)
      : events;

  return (
    <section className="workspace-panel event-stream-panel" aria-label="实时事件流">
      <div className="workspace-panel-header">
        <div>
          <p className="workspace-eyebrow">Runtime Feed</p>
          <h2>实时事件流</h2>
        </div>
        <div className="segmented-control" role="tablist" aria-label="事件过滤">
          <button
            type="button"
            className={filterMode === "all" ? "segment active" : "segment"}
            onClick={() => onChangeFilterMode("all")}
          >
            全部
          </button>
          <button
            type="button"
            className={filterMode === "stage" ? "segment active" : "segment"}
            onClick={() => onChangeFilterMode("stage")}
            disabled={!selectedStage}
          >
            当前阶段
          </button>
        </div>
      </div>

      <div className="panel-scroll-body compact-scroll">
        {filteredEvents.length === 0 ? (
          <p className="empty-state">等待事件流入...</p>
        ) : (
          <div className="event-list" role="list">
          {filteredEvents.map((event, index) => {
            const eventKey = buildEventKey(event, index);
            const expanded = expandedEventKeys.includes(eventKey);
            const preview = buildPreview(event.payload);
            return (
              <article key={eventKey} className="event-row" role="listitem">
                <div className="event-row-meta">
                  <span>{formatEventTime(event.created_at)}</span>
                  <span>{event.stage || "system"}</span>
                  <strong>{event.event_type || "event"}</strong>
                </div>
                {preview ? <p className="event-inline-preview">{preview}</p> : null}
                {event.payload ? (
                  <div className="event-expandable">
                    <button
                      type="button"
                      className="inline-toggle"
                      onClick={() => onToggleEvent(eventKey)}
                    >
                      {expanded ? "收起原始负载" : "查看原始负载"}
                    </button>
                    {expanded ? (
                      <pre className="json-block">{JSON.stringify(event.payload, null, 2)}</pre>
                    ) : null}
                  </div>
                ) : null}
              </article>
            );
          })}
          </div>
        )}
      </div>
    </section>
  );
}
