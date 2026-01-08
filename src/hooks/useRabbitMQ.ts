import { useState, useEffect, useCallback, useRef } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { listen, UnlistenFn } from '@tauri-apps/api/event';
import { BloodbankEvent, RabbitMQConfig, DEFAULT_RABBITMQ_CONFIG } from '@/types/bloodbank';

interface RabbitMQState {
  isConnected: boolean;
  isConnecting: boolean;
  error: string | null;
  config: RabbitMQConfig | null;
}

interface UseRabbitMQReturn {
  state: RabbitMQState;
  events: BloodbankEvent[];
  connect: (config?: Partial<RabbitMQConfig>) => Promise<void>;
  disconnect: () => Promise<void>;
  clearEvents: () => void;
}

export function useRabbitMQ(maxEvents: number = 500): UseRabbitMQReturn {
  const [state, setState] = useState<RabbitMQState>({
    isConnected: false,
    isConnecting: false,
    error: null,
    config: null,
  });
  const [events, setEvents] = useState<BloodbankEvent[]>([]);
  const unlistenRefs = useRef<UnlistenFn[]>([]);

  // Set up event listeners
  useEffect(() => {
    const setupListeners = async () => {
      // Listen for bloodbank events
      const unlistenEvent = await listen<BloodbankEvent>('bloodbank:event', (event) => {
        setEvents((prev) => {
          const newEvents = [event.payload, ...prev];
          return newEvents.slice(0, maxEvents);
        });
      });

      // Listen for connection status
      const unlistenConnected = await listen<RabbitMQConfig>('rabbitmq:connected', (event) => {
        setState((prev) => ({
          ...prev,
          isConnected: true,
          isConnecting: false,
          error: null,
          config: event.payload,
        }));
      });

      // Listen for disconnection
      const unlistenDisconnected = await listen('rabbitmq:disconnected', () => {
        setState((prev) => ({
          ...prev,
          isConnected: false,
          isConnecting: false,
          config: null,
        }));
      });

      unlistenRefs.current = [unlistenEvent, unlistenConnected, unlistenDisconnected];
    };

    setupListeners();

    return () => {
      unlistenRefs.current.forEach((unlisten) => unlisten());
    };
  }, [maxEvents]);

  const connect = useCallback(async (configOverrides?: Partial<RabbitMQConfig>) => {
    setState((prev) => ({ ...prev, isConnecting: true, error: null }));

    try {
      // Get default config from backend and merge with overrides
      const defaultConfig = await invoke<RabbitMQConfig>('rabbitmq_default_config');
      const config = {
        ...defaultConfig,
        ...configOverrides,
        // Map camelCase to snake_case for Rust
        routing_keys: configOverrides?.routingKeys || defaultConfig.routing_keys || ['#'],
      };

      await invoke('rabbitmq_connect', { config });
      // State will be updated via the rabbitmq:connected event
    } catch (err) {
      setState((prev) => ({
        ...prev,
        isConnecting: false,
        error: err instanceof Error ? err.message : String(err),
      }));
    }
  }, []);

  const disconnect = useCallback(async () => {
    try {
      await invoke('rabbitmq_disconnect');
      setState((prev) => ({
        ...prev,
        isConnected: false,
        config: null,
      }));
    } catch (err) {
      setState((prev) => ({
        ...prev,
        error: err instanceof Error ? err.message : String(err),
      }));
    }
  }, []);

  const clearEvents = useCallback(() => {
    setEvents([]);
  }, []);

  return {
    state,
    events,
    connect,
    disconnect,
    clearEvents,
  };
}
