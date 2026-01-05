'use client';

import React, { useState, useEffect } from 'react';
import { Activity, Zap, Clock, TrendingUp, AlertCircle, CheckCircle } from 'lucide-react';
// import { invoke } from '@tauri-apps/api/tauri';

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
    // Connect to RabbitMQ and start receiving events
    // TODO: Implement Tauri backend commands
    setIsConnected(true);

    // Mock event stream for now
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

    // Simulate real-time events
    const interval = setInterval(() => {
      const newEvent = mockEvents[Math.floor(Math.random() * mockEvents.length)];
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
    if (eventType.includes('error')) return <AlertCircle className="w-4 h-4 text-red-500" />;
    if (eventType.includes('ready') || eventType.includes('response')) return <CheckCircle className="w-4 h-4 text-green-500" />;
    if (eventType.includes('prompt')) return <Zap className="w-4 h-4 text-blue-500" />;
    return <Activity className="w-4 h-4 text-gray-500" />;
  };

  const formatTimestamp = (timestamp: string) => {
    return new Date(timestamp).toLocaleTimeString();
  };

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Header */}
      <div className="bg-white shadow-sm border-b">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex justify-between items-center py-4">
            <div className="flex items-center space-x-3">
              <Activity className="w-8 h-8 text-blue-600" />
              <div>
            <h1 className="text-2xl font-bold text-gray-900">Candybar</h1>
            <p className="text-sm text-gray-500">Bloodbank event observability for 33GOD system</p>
              </div>
            </div>
            <div className="flex items-center space-x-2">
              <div className={`w-3 h-3 rounded-full ${isConnected ? 'bg-green-500' : 'bg-red-500'}`}></div>
              <span className="text-sm text-gray-600">
                {isConnected ? 'Connected' : 'Disconnected'}
              </span>
            </div>
          </div>
        </div>
      </div>

      {/* Stats Cards */}
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6">
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-6">
          <div className="bg-white rounded-lg shadow p-6">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium text-gray-600">Total Events</p>
                <p className="text-2xl font-bold text-gray-900">{stats.totalEvents}</p>
              </div>
              <Activity className="w-8 h-8 text-blue-600" />
            </div>
          </div>
          
          <div className="bg-white rounded-lg shadow p-6">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium text-gray-600">Events/Min</p>
                <p className="text-2xl font-bold text-gray-900">{stats.eventsPerMinute}</p>
              </div>
              <TrendingUp className="w-8 h-8 text-green-600" />
            </div>
          </div>
          
          <div className="bg-white rounded-lg shadow p-6">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium text-gray-600">Error Rate</p>
                <p className="text-2xl font-bold text-gray-900">{stats.errorRate.toFixed(1)}%</p>
              </div>
              <AlertCircle className="w-8 h-8 text-red-600" />
            </div>
          </div>
          
          <div className="bg-white rounded-lg shadow p-6">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium text-gray-600">Connections</p>
                <p className="text-2xl font-bold text-gray-900">{stats.activeConnections}</p>
              </div>
              <Zap className="w-8 h-8 text-purple-600" />
            </div>
          </div>
        </div>

        {/* Main Content */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Event Stream */}
          <div className="lg:col-span-2 bg-white rounded-lg shadow">
            <div className="px-6 py-4 border-b border-gray-200">
              <h2 className="text-lg font-medium text-gray-900">Event Stream</h2>
            </div>
            <div className="divide-y divide-gray-200 max-h-96 overflow-y-auto">
              {events.length === 0 ? (
                <div className="px-6 py-4 text-center text-gray-500">
                  No events received yet
                </div>
              ) : (
                events.map((event) => (
                  <div
                    key={event.event_id}
                    className="px-6 py-4 hover:bg-gray-50 cursor-pointer"
                    onClick={() => setSelectedEvent(event)}
                  >
                    <div className="flex items-center justify-between">
                      <div className="flex items-center space-x-3">
                        {getEventTypeIcon(event.event_type)}
                        <div>
                          <p className="text-sm font-medium text-gray-900">
                            {event.event_type}
                          </p>
                          <p className="text-xs text-gray-500">
                            {event.source.app} • {formatTimestamp(event.timestamp)}
                          </p>
                        </div>
                      </div>
                      <div className="text-xs text-gray-400">
                        {event.correlation_ids.length > 0 && (
                          <span className="mr-2">🔗 {event.correlation_ids.length}</span>
                        )}
                        <Clock className="inline w-3 h-3" />
                      </div>
                    </div>
                  </div>
                ))
              )}
            </div>
          </div>

          {/* Event Details */}
          <div className="bg-white rounded-lg shadow">
            <div className="px-6 py-4 border-b border-gray-200">
              <h2 className="text-lg font-medium text-gray-900">Event Details</h2>
            </div>
            <div className="p-6">
              {selectedEvent ? (
                <div className="space-y-4">
                  <div>
                    <h3 className="text-sm font-medium text-gray-900">Event Type</h3>
                    <p className="text-sm text-gray-600">{selectedEvent.event_type}</p>
                  </div>
                  <div>
                    <h3 className="text-sm font-medium text-gray-900">Event ID</h3>
                    <p className="text-xs text-gray-600 font-mono">{selectedEvent.event_id}</p>
                  </div>
                  <div>
                    <h3 className="text-sm font-medium text-gray-900">Source</h3>
                    <p className="text-sm text-gray-600">
                      {selectedEvent.source.app} ({selectedEvent.source.host})
                    </p>
                  </div>
                  <div>
                    <h3 className="text-sm font-medium text-gray-900">Timestamp</h3>
                    <p className="text-sm text-gray-600">
                      {new Date(selectedEvent.timestamp).toLocaleString()}
                    </p>
                  </div>
                  <div>
                    <h3 className="text-sm font-medium text-gray-900">Payload</h3>
                    <pre className="text-xs text-gray-600 bg-gray-50 p-3 rounded overflow-x-auto">
                      {JSON.stringify(selectedEvent.payload, null, 2)}
                    </pre>
                  </div>
                </div>
              ) : (
                <div className="text-center text-gray-500">
                  Click an event to view details
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

export default BloodbankObservability;