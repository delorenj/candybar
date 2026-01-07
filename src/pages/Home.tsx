import { useState, useEffect } from 'react';
import { Activity, Zap, Clock, TrendingUp, AlertCircle, CheckCircle } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { NumberTicker } from "@/components/magicui/number-ticker";
import { DotPattern } from "@/components/magicui/dot-pattern";
import { BorderBeam } from "@/components/magicui/border-beam";
import { ShineBorder } from "@/components/magicui/shine-border";
import { ModeToggle } from "@/components/ModeToggle";
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

interface EventStats {
  totalEvents: number;
  eventsPerMinute: number;
  errorRate: number;
  activeConnections: number;
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

  useEffect(() => {
    setIsConnected(true);

    const mockEvents: BloodbankEvent[] = [
      {
        event_id: "550e8400-e29b-41d4-a716-446655440000",
        event_type: "fireflies.transcript.ready",
        timestamp: new Date().toISOString(),
        source: { host: "big-chungus", app: "fireflies-webhook", type: "webhook" },
        correlation_ids: ["123e4567-e89b-12d3-a456-426614174000"],
        payload: {
          id: "transcript_123",
          title: "Meeting Notes",
          duration: 1800,
          participants: ["alice@example.com", "bob@example.com"]
        }
      },
      {
        event_id: "660e8400-e29b-41d4-a716-446655440001",
        event_type: "agent.thread.prompt",
        timestamp: new Date().toISOString(),
        source: { host: "agent-01", app: "thread-manager", type: "api" },
        correlation_ids: [],
        payload: {
          thread_id: "thread_456",
          agent_name: "oracle",
          prompt: "Analyze the latest metrics",
          tokens: 150
        }
      },
      {
        event_id: "770e8400-e29b-41d4-a716-446655440002",
        event_type: "llm.response",
        timestamp: new Date().toISOString(),
        source: { host: "llm-gateway", app: "openai-proxy", type: "api" },
        correlation_ids: ["660e8400-e29b-41d4-a716-446655440001"],
        payload: {
          model: "gpt-4",
          usage: { prompt_tokens: 150, completion_tokens: 300, total_tokens: 450 },
          response_time_ms: 1250
        }
      }
    ];

    const interval = setInterval(() => {
      const newEvent = { ...mockEvents[Math.floor(Math.random() * mockEvents.length)] };
      newEvent.event_id = Math.random().toString(36).substring(7);
      newEvent.timestamp = new Date().toISOString();

      setEvents(prev => [newEvent, ...prev.slice(0, 99)]);
      setStats(prev => ({
        totalEvents: prev.totalEvents + 1,
        eventsPerMinute: Math.floor(Math.random() * 20) + 5,
        errorRate: Math.random() * 5,
        activeConnections: Math.floor(Math.random() * 10) + 3
      }));
    }, 2000);

    return () => clearInterval(interval);
  }, []);

  const getEventTypeIcon = (eventType: string) => {
    if (eventType.includes('error')) return <AlertCircle className="w-4 h-4 text-destructive" />;
    if (eventType.includes('ready') || eventType.includes('response')) return <CheckCircle className="w-4 h-4 text-green-500" />;
    if (eventType.includes('prompt')) return <Zap className="w-4 h-4 text-blue-500" />;
    return <Activity className="w-4 h-4 text-muted-foreground" />;
  };

  const formatTimestamp = (timestamp: string) => {
    return new Date(timestamp).toLocaleTimeString();
  };

  return (
    <div className="relative min-h-screen w-full bg-background overflow-hidden">
      <DotPattern
        className={cn(
          "[mask-image:radial-gradient(500px_circle_at_center,white,transparent)]",
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
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
          {[
            { label: "Total Events", value: stats.totalEvents, icon: Activity, color: "text-blue-500" },
            { label: "Events/Min", value: stats.eventsPerMinute, icon: TrendingUp, color: "text-green-500" },
            { label: "Error Rate", value: stats.errorRate, suffix: "%", icon: AlertCircle, color: "text-red-500", decimals: 1 },
            { label: "Connections", value: stats.activeConnections, icon: Zap, color: "text-purple-500" }
          ].map((stat, i) => (
            <Card key={i} className="overflow-hidden relative group border-none shadow-md">
              <ShineBorder borderWidth={1} duration={10} shineColor={["#A97CF8", "#F38CB8", "#FF2975"]} />
              <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                <CardTitle className="text-sm font-medium text-muted-foreground">{stat.label}</CardTitle>
                <stat.icon className={cn("w-4 h-4 transition-transform group-hover:scale-110", stat.color)} />
              </CardHeader>
              <CardContent>
                <div className="text-3xl font-bold tracking-tight">
                  <NumberTicker value={stat.value} decimalPlaces={stat.decimals || 0} />
                  {stat.suffix}
                </div>
              </CardContent>
            </Card>
          ))}
        </div>

        {/* Main Content */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 items-start">
          {/* Event Stream */}
          <Card className="lg:col-span-2 shadow-xl border-border/50 bg-background/50 backdrop-blur-sm">
            <CardHeader className="border-b bg-muted/30 py-4">
              <div className="flex items-center justify-between">
                <CardTitle className="text-lg">Event Stream</CardTitle>
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
                    events.map((event) => (
                      <div
                        key={event.event_id}
                        className={cn(
                          "px-6 py-4 hover:bg-accent/50 cursor-pointer transition-colors group",
                          selectedEvent?.event_id === event.event_id && "bg-accent"
                        )}
                        onClick={() => setSelectedEvent(event)}
                      >
                        <div className="flex items-center justify-between">
                          <div className="flex items-center space-x-4">
                            <div className="p-2 rounded-full bg-background border shadow-sm group-hover:border-primary/50 transition-colors">
                              {getEventTypeIcon(event.event_type)}
                            </div>
                            <div>
                              <p className="text-sm font-semibold tracking-tight">
                                {event.event_type}
                              </p>
                              <p className="text-[11px] text-muted-foreground font-medium flex items-center gap-2">
                                <span className="text-primary/70">{event.source.app}</span>
                                <Separator orientation="vertical" className="h-2" />
                                <span>{formatTimestamp(event.timestamp)}</span>
                              </p>
                            </div>
                          </div>
                          <div className="flex items-center gap-3">
                            {event.correlation_ids.length > 0 && (
                              <Badge variant="secondary" className="text-[10px] h-5">
                                {event.correlation_ids.length}
                              </Badge>
                            )}
                            <Clock className="w-3.5 h-3.5 text-muted-foreground/50" />
                          </div>
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
            <CardHeader className="border-b bg-muted/30 py-4">
              <CardTitle className="text-lg">Event Details</CardTitle>
            </CardHeader>
            <CardContent className="p-6">
              {selectedEvent ? (
                <div className="space-y-6">
                  <div className="grid grid-cols-2 gap-4">
                    <div className="space-y-1">
                      <h3 className="text-[10px] uppercase tracking-widest font-bold text-muted-foreground/70">Type</h3>
                      <p className="text-xs font-semibold">{selectedEvent.event_type}</p>
                    </div>
                    <div className="space-y-1">
                      <h3 className="text-[10px] uppercase tracking-widest font-bold text-muted-foreground/70">Source</h3>
                      <p className="text-xs font-semibold">{selectedEvent.source.app}</p>
                    </div>
                  </div>

                  <div className="space-y-1">
                    <h3 className="text-[10px] uppercase tracking-widest font-bold text-muted-foreground/70">Event ID</h3>
                    <p className="text-[10px] font-mono bg-muted p-1.5 rounded border border-border/50">{selectedEvent.event_id}</p>
                  </div>

                  <div className="space-y-1">
                    <h3 className="text-[10px] uppercase tracking-widest font-bold text-muted-foreground/70">Timestamp</h3>
                    <p className="text-xs font-medium text-muted-foreground">{new Date(selectedEvent.timestamp).toLocaleString()}</p>
                  </div>

                  <Separator />

                  <div className="space-y-2">
                    <h3 className="text-[10px] uppercase tracking-widest font-bold text-muted-foreground/70">Payload</h3>
                    <ScrollArea className="h-[200px] w-full rounded-md border bg-muted/50 p-4">
                      <pre className="text-[10px] leading-relaxed font-mono">
                        {JSON.stringify(selectedEvent.payload, null, 2)}
                      </pre>
                    </ScrollArea>
                  </div>
                </div>
              ) : (
                <div className="py-20 text-center space-y-3">
                  <div className="w-12 h-12 bg-muted rounded-full flex items-center justify-center mx-auto opacity-50">
                    <Activity className="w-6 h-6 text-muted-foreground" />
                  </div>
                  <p className="text-sm text-muted-foreground font-medium">Click an event to view details</p>
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      </main>
    </div>
  );
};

export default BloodbankObservability;
