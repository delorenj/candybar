import { useMemo } from 'react';
import { BloodbankEvent, getDomainFromEventType, getDomainColor } from '@/types/bloodbank';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { ScrollArea } from '@/components/ui/scroll-area';
import { GitBranch, ArrowRight } from 'lucide-react';

interface FlowNode {
  id: string;
  label: string;
  count: number;
  color: string;
  type: 'source' | 'event' | 'target';
}

interface FlowEdge {
  from: string;
  to: string;
  count: number;
  label: string;
}

interface EventFlowDiagramProps {
  events: BloodbankEvent[];
  maxHeight?: string;
}

export const EventFlowDiagram = ({ events, maxHeight = '600px' }: EventFlowDiagramProps) => {
  const { nodes, flows } = useMemo(() => {
    const flowMap = new Map<string, { count: number; events: BloodbankEvent[] }>();

    // Build flows: source → event_type → target
    events.forEach((event) => {
      const source = event.source.app;
      const eventType = event.event_type;
      const target = event.agent_context?.agent_name || 'system';

      const flowKey = `${source}::${eventType}::${target}`;

      if (!flowMap.has(flowKey)) {
        flowMap.set(flowKey, { count: 0, events: [] });
      }

      const flow = flowMap.get(flowKey)!;
      flow.count++;
      flow.events.push(event);
    });

    // Extract unique nodes
    const sourceNodes = new Map<string, FlowNode>();
    const eventNodes = new Map<string, FlowNode>();
    const targetNodes = new Map<string, FlowNode>();

    flowMap.forEach((flow, flowKey) => {
      const [source, eventType, target] = flowKey.split('::');
      const domain = getDomainFromEventType(eventType);
      const color = getDomainColor(domain);

      // Source node
      if (!sourceNodes.has(source)) {
        sourceNodes.set(source, {
          id: `source-${source}`,
          label: source,
          count: 0,
          color: '#6B7280',
          type: 'source',
        });
      }
      sourceNodes.get(source)!.count += flow.count;

      // Event node
      if (!eventNodes.has(eventType)) {
        eventNodes.set(eventType, {
          id: `event-${eventType}`,
          label: eventType,
          count: 0,
          color,
          type: 'event',
        });
      }
      eventNodes.get(eventType)!.count += flow.count;

      // Target node
      if (!targetNodes.has(target)) {
        targetNodes.set(target, {
          id: `target-${target}`,
          label: target,
          count: 0,
          color: '#6B7280',
          type: 'target',
        });
      }
      targetNodes.get(target)!.count += flow.count;
    });

    // Build edges
    const edgesList: FlowEdge[] = [];

    flowMap.forEach((flow, flowKey) => {
      const [source, eventType, target] = flowKey.split('::');

      // Source to Event
      edgesList.push({
        from: `source-${source}`,
        to: `event-${eventType}`,
        count: flow.count,
        label: '',
      });

      // Event to Target
      edgesList.push({
        from: `event-${eventType}`,
        to: `target-${target}`,
        count: flow.count,
        label: '',
      });
    });

    return {
      nodes: {
        sources: Array.from(sourceNodes.values()).sort((a, b) => b.count - a.count),
        events: Array.from(eventNodes.values()).sort((a, b) => b.count - a.count),
        targets: Array.from(targetNodes.values()).sort((a, b) => b.count - a.count),
      },
      flows: Array.from(flowMap.entries()).sort((a, b) => b[1].count - a[1].count),
    };
  }, [events]);

  const maxCount = Math.max(
    ...nodes.sources.map((n) => n.count),
    ...nodes.events.map((n) => n.count),
    ...nodes.targets.map((n) => n.count),
    1
  );

  return (
    <Card className="shadow-xl border-border/50 bg-background/50 backdrop-blur-sm">
      <CardHeader className="border-b bg-muted/30 py-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <GitBranch className="w-4 h-4" />
            <CardTitle className="text-base">Event Flow Diagram</CardTitle>
          </div>
          <Badge variant="secondary" className="font-mono text-xs">
            {flows.length} flows
          </Badge>
        </div>
      </CardHeader>

      <CardContent className="p-0">
        <ScrollArea style={{ height: maxHeight }}>
          <div className="p-6">
            {/* Three-column layout: Sources → Events → Targets */}
            <div className="grid grid-cols-3 gap-8">
              {/* Sources Column */}
              <div className="space-y-2">
                <h3 className="text-xs font-bold text-muted-foreground uppercase tracking-wider mb-3">
                  Sources ({nodes.sources.length})
                </h3>
                {nodes.sources.map((node) => {
                  const widthPercent = (node.count / maxCount) * 100;
                  return (
                    <div
                      key={node.id}
                      className="group relative p-2 rounded-md bg-muted/30 hover:bg-accent/50 transition-colors cursor-pointer"
                    >
                      <div className="flex items-center justify-between mb-1">
                        <span className="text-xs font-semibold truncate">{node.label}</span>
                        <Badge variant="secondary" className="text-[10px] font-mono">
                          {node.count}
                        </Badge>
                      </div>
                      <div className="h-1 bg-muted rounded-full overflow-hidden">
                        <div
                          className="h-full bg-blue-500 transition-all"
                          style={{ width: `${widthPercent}%` }}
                        />
                      </div>
                    </div>
                  );
                })}
              </div>

              {/* Events Column */}
              <div className="space-y-2">
                <h3 className="text-xs font-bold text-muted-foreground uppercase tracking-wider mb-3">
                  Events ({nodes.events.length})
                </h3>
                {nodes.events.map((node) => {
                  const widthPercent = (node.count / maxCount) * 100;
                  return (
                    <div
                      key={node.id}
                      className="group relative p-2 rounded-md hover:bg-accent/50 transition-colors cursor-pointer"
                      style={{ backgroundColor: node.color + '15' }}
                    >
                      <div className="flex items-center justify-between mb-1">
                        <div className="flex items-center gap-1.5 min-w-0 flex-1">
                          <div
                            className="w-2 h-2 rounded-full flex-shrink-0"
                            style={{ backgroundColor: node.color }}
                          />
                          <span className="text-xs font-semibold truncate">{node.label}</span>
                        </div>
                        <Badge variant="secondary" className="text-[10px] font-mono">
                          {node.count}
                        </Badge>
                      </div>
                      <div className="h-1 bg-muted rounded-full overflow-hidden">
                        <div
                          className="h-full transition-all"
                          style={{ width: `${widthPercent}%`, backgroundColor: node.color }}
                        />
                      </div>
                    </div>
                  );
                })}
              </div>

              {/* Targets Column */}
              <div className="space-y-2">
                <h3 className="text-xs font-bold text-muted-foreground uppercase tracking-wider mb-3">
                  Targets ({nodes.targets.length})
                </h3>
                {nodes.targets.map((node) => {
                  const widthPercent = (node.count / maxCount) * 100;
                  return (
                    <div
                      key={node.id}
                      className="group relative p-2 rounded-md bg-muted/30 hover:bg-accent/50 transition-colors cursor-pointer"
                    >
                      <div className="flex items-center justify-between mb-1">
                        <span className="text-xs font-semibold truncate">{node.label}</span>
                        <Badge variant="secondary" className="text-[10px] font-mono">
                          {node.count}
                        </Badge>
                      </div>
                      <div className="h-1 bg-muted rounded-full overflow-hidden">
                        <div
                          className="h-full bg-purple-500 transition-all"
                          style={{ width: `${widthPercent}%` }}
                        />
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>

            {/* Flow Paths */}
            <div className="mt-8 space-y-2">
              <h3 className="text-xs font-bold text-muted-foreground uppercase tracking-wider mb-3">
                Top Flow Paths
              </h3>
              {flows.slice(0, 10).map(([flowKey, flow]) => {
                const [source, eventType, target] = flowKey.split('::');
                const domain = getDomainFromEventType(eventType);
                const color = getDomainColor(domain);

                return (
                  <div
                    key={flowKey}
                    className="flex items-center gap-2 p-2 rounded-md hover:bg-accent/50 transition-colors text-xs"
                  >
                    <Badge variant="outline" className="font-mono">
                      {flow.count}
                    </Badge>
                    <span className="font-medium text-muted-foreground">{source}</span>
                    <ArrowRight className="w-3 h-3 text-muted-foreground/50" />
                    <div className="flex items-center gap-1.5">
                      <div className="w-2 h-2 rounded-full" style={{ backgroundColor: color }} />
                      <span className="font-semibold" style={{ color }}>{eventType}</span>
                    </div>
                    <ArrowRight className="w-3 h-3 text-muted-foreground/50" />
                    <span className="font-medium text-muted-foreground">{target}</span>
                  </div>
                );
              })}
            </div>
          </div>
        </ScrollArea>
      </CardContent>
    </Card>
  );
};
