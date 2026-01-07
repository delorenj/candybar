import { useState, useEffect, useCallback, useRef } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { cn } from '@/lib/utils';

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
  children?: EventNode[];
}

interface EventCloudProps {
  events: BloodbankEvent[];
  onNodeClick?: (nodeId: string, children?: EventNode[]) => void;
}

// Parse event type into domain and action
const parseEventType = (eventType: string): { domain: string; entity: string; action: string } => {
  const parts = eventType.split('.');
  return {
    domain: parts[0] || 'unknown',
    entity: parts[1] || '',
    action: parts.slice(2).join('.') || '',
  };
};

// Calculate brightness based on recency (0-1)
const calculateBrightness = (lastFired: number, now: number): number => {
  const elapsed = now - lastFired;
  const decayTime = 3000; // 3 seconds to fully dim
  return Math.max(0, 1 - elapsed / decayTime);
};

export function EventCloud({ events, onNodeClick }: EventCloudProps) {
  const [nodes, setNodes] = useState<Map<string, EventNode>>(new Map());
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const animationRef = useRef<number>();

  // Process incoming events into nodes
  useEffect(() => {
    if (events.length === 0) return;

    const latestEvent = events[0];
    const { domain } = parseEventType(latestEvent.event_type);
    const now = Date.now();

    setNodes(prev => {
      const updated = new Map(prev);
      const existing = updated.get(domain);

      if (existing) {
        updated.set(domain, {
          ...existing,
          count: existing.count + 1,
          lastFired: now,
          brightness: 1,
        });
      } else {
        updated.set(domain, {
          id: domain,
          label: domain,
          count: 1,
          lastFired: now,
          brightness: 1,
        });
      }

      return updated;
    });
  }, [events]);

  // Decay brightness over time
  useEffect(() => {
    const animate = () => {
      const now = Date.now();
      setNodes(prev => {
        const updated = new Map();
        let changed = false;

        prev.forEach((node, key) => {
          const newBrightness = calculateBrightness(node.lastFired, now);
          if (Math.abs(newBrightness - node.brightness) > 0.01) {
            changed = true;
          }
          updated.set(key, { ...node, brightness: newBrightness });
        });

        return changed ? updated : prev;
      });

      animationRef.current = requestAnimationFrame(animate);
    };

    animationRef.current = requestAnimationFrame(animate);
    return () => {
      if (animationRef.current) cancelAnimationFrame(animationRef.current);
    };
  }, []);

  const handleNodeClick = useCallback((nodeId: string) => {
    setSelectedNode(nodeId === selectedNode ? null : nodeId);

    // Build children from actual events
    const childEvents = events
      .filter(e => parseEventType(e.event_type).domain === nodeId)
      .reduce((acc, e) => {
        const { entity, action } = parseEventType(e.event_type);
        const childKey = `${entity}.${action}`;
        if (!acc[childKey]) {
          acc[childKey] = { count: 0, lastFired: 0 };
        }
        acc[childKey].count++;
        const timestamp = new Date(e.timestamp).getTime();
        if (timestamp > acc[childKey].lastFired) {
          acc[childKey].lastFired = timestamp;
        }
        return acc;
      }, {} as Record<string, { count: number; lastFired: number }>);

    const children: EventNode[] = Object.entries(childEvents).map(([key, val]) => ({
      id: `${nodeId}.${key}`,
      label: key,
      count: val.count,
      lastFired: val.lastFired,
      brightness: calculateBrightness(val.lastFired, Date.now()),
    }));

    onNodeClick?.(nodeId, children);
  }, [events, selectedNode, onNodeClick]);

  const nodeArray = Array.from(nodes.values());

  return (
    <div className="relative w-full h-full min-h-[400px] flex items-center justify-center">
      <div className="relative flex flex-wrap items-center justify-center gap-8 p-8">
        <AnimatePresence>
          {nodeArray.map((node, index) => (
            <EventNodeBubble
              key={node.id}
              node={node}
              index={index}
              total={nodeArray.length}
              isSelected={selectedNode === node.id}
              onClick={() => handleNodeClick(node.id)}
            />
          ))}
        </AnimatePresence>

        {nodeArray.length === 0 && (
          <div className="text-muted-foreground text-sm">
            Waiting for events...
          </div>
        )}
      </div>
    </div>
  );
}

interface EventNodeBubbleProps {
  node: EventNode;
  index: number;
  total: number;
  isSelected: boolean;
  onClick: () => void;
}

function EventNodeBubble({ node, index, isSelected, onClick }: EventNodeBubbleProps) {
  // Base size grows with count, capped
  const baseSize = Math.min(120, 60 + Math.log10(node.count + 1) * 30);

  // Glow intensity based on brightness
  const glowIntensity = node.brightness;
  const glowColor = `rgba(255, 220, 50, ${glowIntensity * 0.8})`;
  const glowSize = 20 + glowIntensity * 40;

  return (
    <motion.div
      initial={{ scale: 0, opacity: 0 }}
      animate={{
        scale: 1,
        opacity: 1,
      }}
      exit={{ scale: 0, opacity: 0 }}
      whileHover={{ scale: 1.1 }}
      whileTap={{ scale: 0.95 }}
      onClick={onClick}
      className={cn(
        "relative cursor-pointer select-none",
        "rounded-full flex flex-col items-center justify-center",
        "transition-colors duration-300",
        isSelected ? "ring-2 ring-primary ring-offset-2 ring-offset-background" : ""
      )}
      style={{
        width: baseSize,
        height: baseSize,
        background: `radial-gradient(circle at center,
          hsl(var(--card)) 0%,
          hsl(var(--muted)) 100%)`,
        boxShadow: glowIntensity > 0.1
          ? `0 0 ${glowSize}px ${glowColor},
             0 0 ${glowSize * 2}px ${glowColor.replace(String(glowIntensity * 0.8), String(glowIntensity * 0.4))},
             inset 0 0 ${glowSize / 2}px ${glowColor.replace(String(glowIntensity * 0.8), String(glowIntensity * 0.3))}`
          : `0 4px 20px rgba(0, 0, 0, 0.3)`,
        border: `2px solid ${glowIntensity > 0.1 ? glowColor : 'hsl(var(--border))'}`,
      }}
    >
      {/* Flicker overlay for active events */}
      {glowIntensity > 0.5 && (
        <motion.div
          className="absolute inset-0 rounded-full"
          animate={{
            opacity: [0.3, 0.7, 0.3],
          }}
          transition={{
            duration: 0.15,
            repeat: Infinity,
            repeatType: "reverse",
          }}
          style={{
            background: `radial-gradient(circle at center, ${glowColor} 0%, transparent 70%)`,
          }}
        />
      )}

      {/* Domain label */}
      <span
        className={cn(
          "text-xs font-bold uppercase tracking-wider z-10",
          glowIntensity > 0.5 ? "text-yellow-100" : "text-foreground"
        )}
        style={{
          textShadow: glowIntensity > 0.3
            ? `0 0 10px ${glowColor}`
            : 'none',
        }}
      >
        {node.label}
      </span>

      {/* Event count */}
      <span
        className={cn(
          "text-[10px] font-mono z-10 mt-0.5",
          glowIntensity > 0.5 ? "text-yellow-200" : "text-muted-foreground"
        )}
      >
        {node.count}
      </span>
    </motion.div>
  );
}

export default EventCloud;
