import { useMemo } from 'react';
import { BloodbankEvent, getDomainFromEventType } from '@/types/bloodbank';
import { EventFilter } from '@/components/EventFilters';

const TIME_RANGES = {
  '5min': 5 * 60 * 1000,
  '15min': 15 * 60 * 1000,
  '1hr': 60 * 60 * 1000,
  '6hr': 6 * 60 * 60 * 1000,
  '24hr': 24 * 60 * 60 * 1000,
};

export function useEventFiltering(events: BloodbankEvent[], filters: EventFilter) {
  const filteredEvents = useMemo(() => {
    const now = Date.now();

    return events.filter((event) => {
      // Domain filter
      if (filters.domain !== 'all') {
        const eventDomain = getDomainFromEventType(event.event_type);
        if (eventDomain !== filters.domain) return false;
      }

      // Event type filter
      if (filters.eventType !== 'all') {
        if (event.event_type !== filters.eventType) return false;
      }

      // Source app filter
      if (filters.sourceApp !== 'all') {
        if (event.source.app !== filters.sourceApp) return false;
      }

      // Time range filter
      if (filters.timeRange !== 'all') {
        const eventTime = new Date(event.timestamp).getTime();
        const cutoff = now - TIME_RANGES[filters.timeRange];
        if (eventTime < cutoff) return false;
      }

      // Session ID filter
      if (filters.sessionId) {
        const sessionId = event.agent_context?.session_id;
        if (sessionId !== filters.sessionId) return false;
      }

      // Errors only filter
      if (filters.errorsOnly) {
        const isError = event.event_type.includes('error') ||
                       event.event_type.includes('failed');
        if (!isError) return false;
      }

      // Search text filter (search in event type and payload)
      if (filters.searchText) {
        const searchLower = filters.searchText.toLowerCase();
        const payloadStr = JSON.stringify(event.payload).toLowerCase();
        const eventTypeLower = event.event_type.toLowerCase();

        if (!eventTypeLower.includes(searchLower) &&
            !payloadStr.includes(searchLower)) {
          return false;
        }
      }

      return true;
    });
  }, [events, filters]);

  // Extract unique values for filter dropdowns
  const availableSources = useMemo(() => {
    const sources = new Set(events.map(e => e.source.app));
    return Array.from(sources).sort();
  }, [events]);

  const availableSessions = useMemo(() => {
    const sessions = new Set(
      events
        .map(e => e.agent_context?.session_id)
        .filter((id): id is string => !!id)
    );
    return Array.from(sessions).sort();
  }, [events]);

  return {
    filteredEvents,
    availableSources,
    availableSessions,
    totalCount: events.length,
    filteredCount: filteredEvents.length,
  };
}
