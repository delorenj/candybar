import { useState, useEffect, useCallback } from 'react';
import { Activity, Zap, Clock, TrendingUp, AlertCircle, CheckCircle } from 'lucide-react';
import { AnimatePresence } from 'framer-motion';
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { NumberTicker } from "@/components/magicui/number-ticker";
import { DotPattern } from "@/components/magicui/dot-pattern";
import { BorderBeam } from "@/components/magicui/border-beam";
import { ShineBorder } from "@/components/magicui/shine-border";
import { ModeToggle } from "@/components/ModeToggle";
import { EventCloud } from "@/components/EventCloud";
import { EventGraph } from "@/components/EventGraph";
import { cn } from "@/lib/utils";

interface BloodbankEvent {
  event_id: string;
  event_type: string;
  timestamp: string;
  source: {
    host: string;
    app: string;
    type: string;
  };
  correlation_ids: string[];
  payload: any;
}

interface EventNode {
  id: string;
  label: string;
  count: number;
  lastFired: number;
  brightness: number;
}

interface EventStats {
  totalEvents: number;
  eventsPerMinute: number;
  errorRate: number;
  activeConnections: number;
}

interface GraphState {
  parentId: string;
  parentLabel: string;
  children: EventNode[];
}

const BloodbankObservability = () => {
  const [events, setEvents] = useState<BloodbankEvent[]>([]);
  const [stats, setStats] = useState<EventStats>({
    totalEvents: 0,
    eventsPerMinute: 0,
    errorRate: 0,
    activeConnections: 0
  });
  const [isConnected, setIsConnected] = useState(false);
  const [selectedEvent, setSelectedEvent] = useState<BloodbankEvent | null>(null);
  const [graphState, setGraphState] = useState<GraphState | null>(null);
  const [showEventList, setShowEventList] = useState(false);

  useEffect(() => {
    setIsConnected(true);

    // Mock events representing real Bloodbank event types
    const mockEvents: BloodbankEvent[] = [
      {
        event_id: "550e8400-e29b-41d4-a716-446655440000",
        event_type: "fireflies.transcript.ready",
        timestamp: new Date().toISOString(),
        source: { host: "big-chungus", app: "fireflies-webhook", type: "webhook" },
        correlation_ids: ["123e4567-e89b-12d3-a456-426614174000"],
        payload: { id: "transcript_123", title: "Meeting Notes", duration: 1800 }
      },
      {
        event_id: "660e8400-e29b-41d4-a716-446655440001",
        event_type: "fireflies.meeting.started",
        timestamp: new Date().toISOString(),
        source: { host: "big-chungus", app: "fireflies-webhook", type: "webhook" },
        correlation_ids: [],
        payload: { meeting_id: "meet_789", participants: 3 }
      },
      {
        event_id: "770e8400-e29b-41d4-a716-446655440002",
        event_type: "agent.thread.prompt",
        timestamp: new Date().toISOString(),
        source: { host: "agent-01", app: "thread-manager", type: "api" },
        correlation_ids: [],
        payload: { thread_id: "thread_456", agent_name: "oracle", tokens: 150 }
      },
      {
        event_id: "880e8400-e29b-41d4-a716-446655440003",
        event_type: "agent.task.completed",
        timestamp: new Date().toISOString(),
        source: { host: "agent-01", app: "thread-manager", type: "api" },
        correlation_ids: [],
        payload: { task_id: "task_123", status: "success" }
      },
      {
        event_id: "990e8400-e29b-41d4-a716-446655440004",
        event_type: "llm.response.received",
        timestamp: new Date().toISOString(),
        source: { host: "llm-gateway", app: "openai-proxy", type: "api" },
        correlation_ids: [],
        payload: { model: "gpt-4", tokens: 450, latency_ms: 1250 }
      },
      {
        event_id: "aa0e8400-e29b-41d4-a716-446655440005",
        event_type: "llm.request.sent",
        timestamp: new Date().toISOString(),
        source: { host: "llm-gateway", app: "openai-proxy", type: "api" },
        correlation_ids: [],
        payload: { model: "gpt-4", prompt_tokens: 150 }
      },
      {
        event_id: "bb0e8400-e29b-41d4-a716-446655440006",
        event_type: "artifact.file.created",
        timestamp: new Date().toISOString(),
        source: { host: "artifact-store", app: "file-manager", type: "api" },
        correlation_ids: [],
        payload: { filename: "report.pdf", size: 2048576 }
      },
      {
        event_id: "cc0e8400-e29b-41d4-a716-446655440007",
        event_type: "github.pr.opened",
        timestamp: new Date().toISOString(),
        source: { host: "github-webhook", app: "github-events", type: "webhook" },
        correlation_ids: [],
        payload: { pr_number: 123, repo: "delorenj/project" }
      },
      {
        event_id: "dd0e8400-e29b-41d4-a716-446655440008",
        event_type: "github.commit.pushed",
        timestamp: new Date().toISOString(),
        source: { host: "github-webhook", app: "github-events", type: "webhook" },
        correlation_ids: [],
        payload: { sha: "abc123", message: "feat: add feature" }
      },
      {
        event_id: "ee0e8400-e29b-41d4-a716-446655440009",
        event_type: "workflow.step.completed",
        timestamp: new Date().toISOString(),
        source: { host: "n8n-runner", app: "workflow-engine", type: "internal" },
        correlation_ids: [],
        payload: { workflow_id: "wf_001", step: "transform" }
      },
    ];

    // Fire events at varying rates to simulate real activity
    const interval = setInterval(() => {
      // Random burst of 1-3 events
      const burstSize = Math.random() < 0.3 ? Math.floor(Math.random() * 3) + 1 : 1;

      for (let i = 0; i < burstSize; i++) {
        setTimeout(() => {
          const newEvent = { ...mockEvents[Math.floor(Math.random() * mockEvents.length)] };
          newEvent.event_id = Math.random().toString(36).substring(7);
          newEvent.timestamp = new Date().toISOString();

          setEvents(prev => [newEvent, ...prev.slice(0, 199)]);
          setStats(prev => ({
            totalEvents: prev.totalEvents + 1,
            eventsPerMinute: Math.floor(Math.random() * 30) + 10,
            errorRate: Math.random() * 3,
            activeConnections: Math.floor(Math.random() * 10) + 5
          }));
        }, i * 100); // Stagger within burst
      }
    }, 500 + Math.random() * 1000); // Vary interval

    return () => clearInterval(interval);
  }, []);

  const handleNodeClick = useCallback((nodeId: string, children?: EventNode[]) => {
    if (children && children.length > 0) {
      setGraphState({
        parentId: nodeId,
        parentLabel: nodeId,
        children,
      });
    }
  }, []);

  const handleCloseGraph = useCallback(() => {
    setGraphState(null);
  }, []);

  const getEventTypeIcon = (eventType: string) => {
    if (eventType.includes('error')) return <AlertCircle className="w-4 h-4 text-destructive" />;
    if (eventType.includes('ready') || eventType.includes('completed')) return <CheckCircle className="w-4 h-4 text-green-500" />;
    if (eventType.includes('prompt') || eventType.includes('request')) return <Zap className="w-4 h-4 text-blue-500" />;
    return <Activity className="w-4 h-4 text-muted-foreground" />;
  };

  const formatTimestamp = (timestamp: string) => {
    return new Date(timestamp).toLocaleTimeString();
  };

  return (
    <div className="relative min-h-screen w-full bg-background overflow-hidden">
      <DotPattern
        className={cn(
          "[mask-image:radial-gradient(600px_circle_at_center,white,transparent)]",
        )}
      />

      {/* Header */}
      <header className="relative z-10 border-b bg-background/60 backdrop-blur-md">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex justify-between items-center py-4">
            <div className="flex items-center space-x-3">
              <div className="relative p-2 bg-primary/10 rounded-xl">
                <Activity className="w-6 h-6 text-primary" />
                <BorderBeam size={40} duration={4} />
              </div>
              <div>
                <h1 className="text-xl font-bold tracking-tight">Candybar</h1>
                <p className="text-xs text-muted-foreground font-medium">Bloodbank event observability for 33GOD</p>
              </div>
            </div>
            <div className="flex items-center space-x-4">
              <button
                onClick={() => setShowEventList(!showEventList)}
                className={cn(
                  "px-3 py-1 text-xs font-medium rounded-md transition-colors",
                  showEventList
                    ? "bg-primary text-primary-foreground"
                    : "bg-muted text-muted-foreground hover:bg-muted/80"
                )}
              >
                {showEventList ? 'Cloud View' : 'List View'}
              </button>
              <Badge variant={isConnected ? "outline" : "destructive"} className={cn(
                "gap-1.5 px-3 py-1 font-semibold",
                isConnected && "border-green-500/50 text-green-600 dark:text-green-400 bg-green-500/5"
              )}>
                <div className={cn(
                  "w-1.5 h-1.5 rounded-full animate-pulse",
                  isConnected ? "bg-green-500" : "bg-red-500"
                )} />
                {isConnected ? 'Connected' : 'Disconnected'}
              </Badge>
              <ModeToggle />
            </div>
          </div>
        </div>
      </header>

      <main className="relative z-10 max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {/* Stats Cards */}
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-6">
          {[
            { label: "Total Events", value: stats.totalEvents, icon: Activity, color: "text-blue-500" },
            { label: "Events/Min", value: stats.eventsPerMinute, icon: TrendingUp, color: "text-green-500" },
            { label: "Error Rate", value: stats.errorRate, suffix: "%", icon: AlertCircle, color: "text-red-500", decimals: 1 },
            { label: "Connections", value: stats.activeConnections, icon: Zap, color: "text-purple-500" }
          ].map((stat, i) => (
            <Card key={i} className="overflow-hidden relative group border-none shadow-md">
              <ShineBorder borderWidth={1} duration={10} shineColor={["#A97CF8", "#F38CB8", "#FF2975"]} />
              <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-1 pt-3 px-4">
                <CardTitle className="text-xs font-medium text-muted-foreground">{stat.label}</CardTitle>
                <stat.icon className={cn("w-3 h-3 transition-transform group-hover:scale-110", stat.color)} />
              </CardHeader>
              <CardContent className="pb-3 px-4">
                <div className="text-2xl font-bold tracking-tight">
                  <NumberTicker value={stat.value} decimalPlaces={stat.decimals || 0} />
                  {stat.suffix}
                </div>
              </CardContent>
            </Card>
          ))}
        </div>

        {/* Main Content */}
        {showEventList ? (
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 items-start">
            {/* Event Stream */}
            <Card className="lg:col-span-2 shadow-xl border-border/50 bg-background/50 backdrop-blur-sm">
              <CardHeader className="border-b bg-muted/30 py-3">
                <div className="flex items-center justify-between">
                  <CardTitle className="text-base">Event Stream</CardTitle>
                  <Badge variant="secondary" className="font-mono text-[10px]">LIVE</Badge>
                </div>
              </CardHeader>
              <CardContent className="p-0">
                <ScrollArea className="h-[500px]">
                  <div className="divide-y divide-border/50">
                    {events.length === 0 ? (
                      <div className="p-8 text-center text-muted-foreground flex flex-col items-center gap-2">
                        <Zap className="w-8 h-8 opacity-20" />
                        <p>No events received yet</p>
                      </div>
                    ) : (
                      events.slice(0, 50).map((event) => (
                        <div
                          key={event.event_id}
                          className={cn(
                            "px-4 py-3 hover:bg-accent/50 cursor-pointer transition-colors group",
                            selectedEvent?.event_id === event.event_id && "bg-accent"
                          )}
                          onClick={() => setSelectedEvent(event)}
                        >
                          <div className="flex items-center justify-between">
                            <div className="flex items-center space-x-3">
                              <div className="p-1.5 rounded-full bg-background border shadow-sm group-hover:border-primary/50 transition-colors">
                                {getEventTypeIcon(event.event_type)}
                              </div>
                              <div>
                                <p className="text-xs font-semibold tracking-tight">
                                  {event.event_type}
                                </p>
                                <p className="text-[10px] text-muted-foreground font-medium flex items-center gap-2">
                                  <span className="text-primary/70">{event.source.app}</span>
                                  <Separator orientation="vertical" className="h-2" />
                                  <span>{formatTimestamp(event.timestamp)}</span>
                                </p>
                              </div>
                            </div>
                            <Clock className="w-3 h-3 text-muted-foreground/50" />
                          </div>
                        </div>
                      ))
                    )}
                  </div>
                </ScrollArea>
              </CardContent>
            </Card>

            {/* Event Details */}
            <Card className="shadow-xl border-border/50 bg-background/50 backdrop-blur-sm sticky top-24">
              <CardHeader className="border-b bg-muted/30 py-3">
                <CardTitle className="text-base">Event Details</CardTitle>
              </CardHeader>
              <CardContent className="p-4">
                {selectedEvent ? (
                  <div className="space-y-4">
                    <div className="grid grid-cols-2 gap-3">
                      <div className="space-y-1">
                        <h3 className="text-[9px] uppercase tracking-widest font-bold text-muted-foreground/70">Type</h3>
                        <p className="text-xs font-semibold">{selectedEvent.event_type}</p>
                      </div>
                      <div className="space-y-1">
                        <h3 className="text-[9px] uppercase tracking-widest font-bold text-muted-foreground/70">Source</h3>
                        <p className="text-xs font-semibold">{selectedEvent.source.app}</p>
                      </div>
                    </div>

                    <div className="space-y-1">
                      <h3 className="text-[9px] uppercase tracking-widest font-bold text-muted-foreground/70">Event ID</h3>
                      <p className="text-[9px] font-mono bg-muted p-1 rounded border border-border/50">{selectedEvent.event_id}</p>
                    </div>

                    <Separator />

                    <div className="space-y-1">
                      <h3 className="text-[9px] uppercase tracking-widest font-bold text-muted-foreground/70">Payload</h3>
                      <ScrollArea className="h-[180px] w-full rounded-md border bg-muted/50 p-3">
                        <pre className="text-[9px] leading-relaxed font-mono">
                          {JSON.stringify(selectedEvent.payload, null, 2)}
                        </pre>
                      </ScrollArea>
                    </div>
                  </div>
                ) : (
                  <div className="py-16 text-center space-y-2">
                    <div className="w-10 h-10 bg-muted rounded-full flex items-center justify-center mx-auto opacity-50">
                      <Activity className="w-5 h-5 text-muted-foreground" />
                    </div>
                    <p className="text-xs text-muted-foreground font-medium">Click an event to view details</p>
                  </div>
                )}
              </CardContent>
            </Card>
          </div>
        ) : (
          /* Event Cloud View */
          <Card className="shadow-xl border-border/50 bg-background/30 backdrop-blur-sm min-h-[500px]">
            <CardHeader className="border-b bg-muted/30 py-3">
              <div className="flex items-center justify-between">
                <div>
                  <CardTitle className="text-base">Event Domains</CardTitle>
                  <p className="text-xs text-muted-foreground mt-0.5">Click a domain to explore sub-events</p>
                </div>
                <Badge variant="secondary" className="font-mono text-[10px]">LIVE</Badge>
              </div>
            </CardHeader>
            <CardContent className="p-0">
              <EventCloud events={events} onNodeClick={handleNodeClick} />
            </CardContent>
          </Card>
        )}
      </main>

      {/* Graph Overlay */}
      <AnimatePresence>
        {graphState && (
          <EventGraph
            parentId={graphState.parentId}
            parentLabel={graphState.parentLabel}
            children={graphState.children}
            onClose={handleCloseGraph}
          />
        )}
      </AnimatePresence>
    </div>
  );
};

export default BloodbankObservability;
