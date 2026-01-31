import { useState, useEffect, useCallback } from 'react';
import { Activity, Zap, Clock, TrendingUp, AlertCircle, CheckCircle, Wifi, WifiOff, Download, GitBranch } from 'lucide-react';
import { AnimatePresence } from 'framer-motion';
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { Button } from "@/components/ui/button";
import { NumberTicker } from "@/components/magicui/number-ticker";
import { DotPattern } from "@/components/magicui/dot-pattern";
import { BorderBeam } from "@/components/magicui/border-beam";
import { ShineBorder } from "@/components/magicui/shine-border";
import { ModeToggle } from "@/components/ModeToggle";
import { EventCloud } from "@/components/EventCloud";
import { EventGraph } from "@/components/EventGraph";
import { EventFilters, EventFilter } from "@/components/EventFilters";
import { JsonViewer } from "@/components/JsonViewer";
import { EventFlowDiagram } from "@/components/EventFlowDiagram";
import { useRabbitMQ } from "@/hooks/useRabbitMQ";
import { useEventFiltering } from "@/hooks/useEventFiltering";
import { BloodbankEvent, getDomainColor, getDomainFromEventType } from "@/types/bloodbank";
import { cn } from "@/lib/utils";

interface EventNode {
  id: string;
  label: string;
  count: number;
  lastFired: number;
  brightness: number;
  color: string;
}

interface EventStats {
  totalEvents: number;
  eventsPerMinute: number;
  errorRate: number;
  uniqueDomains: number;
}

interface GraphState {
  parentId: string;
  parentLabel: string;
  children: EventNode[];
}

const BloodbankObservability = () => {
  const { state, events, connect, disconnect } = useRabbitMQ(500);
  const [selectedEvent, setSelectedEvent] = useState<BloodbankEvent | null>(null);
  const [graphState, setGraphState] = useState<GraphState | null>(null);
  const [viewMode, setViewMode] = useState<'cloud' | 'list' | 'flow'>('list');
  const [filters, setFilters] = useState<EventFilter>({
    domain: 'all',
    eventType: 'all',
    sourceApp: 'all',
    searchText: '',
    timeRange: 'all',
    sessionId: '',
    errorsOnly: false,
  });
  const [stats, setStats] = useState<EventStats>({
    totalEvents: 0,
    eventsPerMinute: 0,
    errorRate: 0,
    uniqueDomains: 0,
  });

  // Apply filters
  const { filteredEvents, availableSources, availableSessions, filteredCount } = useEventFiltering(events, filters);

  // Calculate stats from real events
  useEffect(() => {
    const now = Date.now();
    const oneMinuteAgo = now - 60000;

    const recentEvents = events.filter(e => new Date(e.timestamp).getTime() > oneMinuteAgo);
    const errorEvents = events.filter(e => e.event_type.includes('error') || e.event_type.includes('failed'));
    const domains = new Set(events.map(e => getDomainFromEventType(e.event_type)));

    setStats({
      totalEvents: events.length,
      eventsPerMinute: recentEvents.length,
      errorRate: events.length > 0 ? (errorEvents.length / events.length) * 100 : 0,
      uniqueDomains: domains.size,
    });
  }, [events]);

  // Auto-connect on mount
  useEffect(() => {
    connect();
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
    if (eventType.includes('error') || eventType.includes('failed')) return <AlertCircle className="w-4 h-4 text-destructive" />;
    if (eventType.includes('ready') || eventType.includes('completed')) return <CheckCircle className="w-4 h-4 text-green-500" />;
    if (eventType.includes('prompt') || eventType.includes('request') || eventType.includes('started')) return <Zap className="w-4 h-4 text-blue-500" />;
    return <Activity className="w-4 h-4 text-muted-foreground" />;
  };

  const formatTimestamp = (timestamp: string) => {
    return new Date(timestamp).toLocaleTimeString();
  };

  const handleConnectionToggle = () => {
    if (state.isConnected) {
      disconnect();
    } else {
      connect();
    }
  };

  const handleExportEvents = () => {
    const dataStr = JSON.stringify(filteredEvents, null, 2);
    const dataBlob = new Blob([dataStr], { type: 'application/json' });
    const url = URL.createObjectURL(dataBlob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `candybar-events-${new Date().toISOString()}.json`;
    link.click();
    URL.revokeObjectURL(url);
  };

  const handleExportEventCSV = () => {
    const headers = ['Event ID', 'Event Type', 'Timestamp', 'Source App', 'Domain', 'Session ID'];
    const rows = filteredEvents.map((event) => [
      event.event_id,
      event.event_type,
      event.timestamp,
      event.source.app,
      getDomainFromEventType(event.event_type),
      event.agent_context?.session_id || '',
    ]);

    const csv = [headers, ...rows].map((row) => row.map((cell) => `"${cell}"`).join(',')).join('\n');
    const dataBlob = new Blob([csv], { type: 'text/csv' });
    const url = URL.createObjectURL(dataBlob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `candybar-events-${new Date().toISOString()}.csv`;
    link.click();
    URL.revokeObjectURL(url);
  };

  const handleClearFilters = () => {
    setFilters({
      domain: 'all',
      eventType: 'all',
      sourceApp: 'all',
      searchText: '',
      timeRange: 'all',
      sessionId: '',
      errorsOnly: false,
    });
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
              {/* View Mode Selector */}
              <div className="flex items-center gap-1 bg-muted rounded-md p-1">
                <button
                  onClick={() => setViewMode('list')}
                  className={cn(
                    "px-3 py-1 text-xs font-medium rounded transition-colors",
                    viewMode === 'list'
                      ? "bg-primary text-primary-foreground"
                      : "text-muted-foreground hover:text-foreground"
                  )}
                >
                  List
                </button>
                <button
                  onClick={() => setViewMode('cloud')}
                  className={cn(
                    "px-3 py-1 text-xs font-medium rounded transition-colors",
                    viewMode === 'cloud'
                      ? "bg-primary text-primary-foreground"
                      : "text-muted-foreground hover:text-foreground"
                  )}
                >
                  Cloud
                </button>
                <button
                  onClick={() => setViewMode('flow')}
                  className={cn(
                    "px-3 py-1 text-xs font-medium rounded transition-colors flex items-center gap-1",
                    viewMode === 'flow'
                      ? "bg-primary text-primary-foreground"
                      : "text-muted-foreground hover:text-foreground"
                  )}
                >
                  <GitBranch className="w-3 h-3" />
                  Flow
                </button>
              </div>

              {/* Export Button */}
              <Button
                variant="outline"
                size="sm"
                onClick={handleExportEvents}
                disabled={filteredEvents.length === 0}
                className="gap-1.5"
              >
                <Download className="w-3 h-3" />
                Export JSON
              </Button>

              {/* Connection Button */}
              <Button
                variant={state.isConnected ? "outline" : "default"}
                size="sm"
                onClick={handleConnectionToggle}
                disabled={state.isConnecting}
                className="gap-2"
              >
                {state.isConnecting ? (
                  <>
                    <div className="w-3 h-3 border-2 border-current border-t-transparent rounded-full animate-spin" />
                    Connecting...
                  </>
                ) : state.isConnected ? (
                  <>
                    <WifiOff className="w-3 h-3" />
                    Disconnect
                  </>
                ) : (
                  <>
                    <Wifi className="w-3 h-3" />
                    Connect
                  </>
                )}
              </Button>

              <Badge variant={state.isConnected ? "outline" : "destructive"} className={cn(
                "gap-1.5 px-3 py-1 font-semibold",
                state.isConnected && "border-green-500/50 text-green-600 dark:text-green-400 bg-green-500/5"
              )}>
                <div className={cn(
                  "w-1.5 h-1.5 rounded-full",
                  state.isConnected ? "bg-green-500 animate-pulse" : "bg-red-500"
                )} />
                {state.isConnected ? 'Connected' : state.error ? 'Error' : 'Disconnected'}
              </Badge>
              <ModeToggle />
            </div>
          </div>
        </div>
      </header>

      {/* Error Banner */}
      {state.error && (
        <div className="relative z-10 bg-destructive/10 border-b border-destructive/20 px-4 py-2">
          <div className="max-w-7xl mx-auto flex items-center gap-2 text-sm text-destructive">
            <AlertCircle className="w-4 h-4" />
            <span>{state.error}</span>
            <Button variant="ghost" size="sm" onClick={() => connect()} className="ml-auto">
              Retry
            </Button>
          </div>
        </div>
      )}

      <main className="relative z-10 max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {/* Stats Cards */}
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-6">
          {[
            { label: "Total Events", value: stats.totalEvents, icon: Activity, color: "text-blue-500" },
            { label: "Events/Min", value: stats.eventsPerMinute, icon: TrendingUp, color: "text-green-500" },
            { label: "Error Rate", value: stats.errorRate, suffix: "%", icon: AlertCircle, color: "text-red-500", decimals: 1 },
            { label: "Domains", value: stats.uniqueDomains, icon: Zap, color: "text-purple-500" }
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

        {/* Filters */}
        <div className="mb-6">
          <EventFilters
            filters={filters}
            onFilterChange={setFilters}
            availableSources={availableSources}
            availableSessions={availableSessions}
            onClearFilters={handleClearFilters}
          />
        </div>

        {/* Filter Results Badge */}
        {filteredCount !== events.length && (
          <div className="mb-4 flex items-center gap-2 text-sm text-muted-foreground">
            <Badge variant="secondary">
              Showing {filteredCount} of {events.length} events
            </Badge>
          </div>
        )}

        {/* Main Content */}
        {viewMode === 'flow' ? (
          <EventFlowDiagram events={filteredEvents} />
        ) : viewMode === 'list' ? (
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 items-start">
            {/* Event Stream */}
            <Card className="lg:col-span-2 shadow-xl border-border/50 bg-background/50 backdrop-blur-sm">
              <CardHeader className="border-b bg-muted/30 py-3">
                <div className="flex items-center justify-between">
                  <CardTitle className="text-base">Event Stream</CardTitle>
                  <Badge variant="secondary" className="font-mono text-[10px]">
                    {state.isConnected ? 'LIVE' : 'OFFLINE'}
                  </Badge>
                </div>
              </CardHeader>
              <CardContent className="p-0">
                <ScrollArea className="h-[500px]">
                  <div className="divide-y divide-border/50">
                    {filteredEvents.length === 0 ? (
                      <div className="p-8 text-center text-muted-foreground flex flex-col items-center gap-2">
                        <Zap className="w-8 h-8 opacity-20" />
                        <p>
                          {state.isConnected
                            ? filteredCount === 0 && events.length > 0
                              ? 'No events match your filters'
                              : 'Waiting for events...'
                            : 'Connect to RabbitMQ to see events'}
                        </p>
                      </div>
                    ) : (
                      filteredEvents.slice(0, 50).map((event) => {
                        const domain = getDomainFromEventType(event.event_type);
                        const domainColor = getDomainColor(domain);

                        return (
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
                                <div
                                  className="p-1.5 rounded-full bg-background border shadow-sm group-hover:border-primary/50 transition-colors"
                                  style={{ borderColor: domainColor + '40' }}
                                >
                                  {getEventTypeIcon(event.event_type)}
                                </div>
                                <div>
                                  <p className="text-xs font-semibold tracking-tight">
                                    {event.event_type}
                                  </p>
                                  <p className="text-[10px] text-muted-foreground font-medium flex items-center gap-2">
                                    <span style={{ color: domainColor }}>{event.source.app}</span>
                                    <Separator orientation="vertical" className="h-2" />
                                    <span>{formatTimestamp(event.timestamp)}</span>
                                  </p>
                                </div>
                              </div>
                              <Clock className="w-3 h-3 text-muted-foreground/50" />
                            </div>
                          </div>
                        );
                      })
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

                    {selectedEvent.correlation_ids.length > 0 && (
                      <div className="space-y-1">
                        <h3 className="text-[9px] uppercase tracking-widest font-bold text-muted-foreground/70">Correlation IDs</h3>
                        <div className="flex flex-wrap gap-1">
                          {selectedEvent.correlation_ids.map((id, i) => (
                            <Badge key={i} variant="outline" className="text-[8px] font-mono">
                              {id.slice(0, 8)}...
                            </Badge>
                          ))}
                        </div>
                      </div>
                    )}

                    <Separator />

                    <div className="space-y-1">
                      <h3 className="text-[9px] uppercase tracking-widest font-bold text-muted-foreground/70">Payload</h3>
                      <JsonViewer
                        data={selectedEvent.payload}
                        maxHeight="200px"
                        onExport={() => {
                          const dataStr = JSON.stringify(selectedEvent, null, 2);
                          const dataBlob = new Blob([dataStr], { type: 'application/json' });
                          const url = URL.createObjectURL(dataBlob);
                          const link = document.createElement('a');
                          link.href = url;
                          link.download = `event-${selectedEvent.event_id}.json`;
                          link.click();
                          URL.revokeObjectURL(url);
                        }}
                      />
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
                <Badge variant="secondary" className="font-mono text-[10px]">
                  {state.isConnected ? 'LIVE' : 'OFFLINE'}
                </Badge>
              </div>
            </CardHeader>
            <CardContent className="p-0">
              <EventCloud events={filteredEvents} onNodeClick={handleNodeClick} />
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
