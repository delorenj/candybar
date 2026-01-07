import { useState, useEffect, useCallback, useRef } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { cn } from '@/lib/utils';
import {
  BloodbankEvent,
  BLOODBANK_DOMAINS,
  DomainKey,
  getDomainFromEventType,
  getDomainColor,
} from '@/types/bloodbank';

interface EventNode {
  id: string;
  label: string;
  count: number;
  lastFired: number;
  brightness: number;
  color: string;
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

// Initialize nodes with all known Bloodbank domains
const initializeNodes = (): Map<string, EventNode> => {
  const nodes = new Map<string, EventNode>();

  Object.entries(BLOODBANK_DOMAINS).forEach(([domain, config]) => {
    nodes.set(domain, {
      id: domain,
      label: config.label,
      count: 0,
      lastFired: 0,
      brightness: 0,
      color: config.color,
    });
  });

  return nodes;
};

export function EventCloud({ events, onNodeClick }: EventCloudProps) {
  const [nodes, setNodes] = useState<Map<string, EventNode>>(() => initializeNodes());
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const animationRef = useRef<number>();

  // Process incoming events into nodes
  useEffect(() => {
    if (events.length === 0) return;

    const latestEvent = events[0];
    const domain = getDomainFromEventType(latestEvent.event_type);
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
        // Unknown domain - add it dynamically
        updated.set(domain, {
          id: domain,
          label: domain,
          count: 1,
          lastFired: now,
          brightness: 1,
          color: getDomainColor(domain),
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
      .filter(e => getDomainFromEventType(e.event_type) === nodeId)
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

    const parentColor = getDomainColor(nodeId as DomainKey | 'unknown');
    const children: EventNode[] = Object.entries(childEvents).map(([key, val]) => ({
      id: `${nodeId}.${key}`,
      label: key,
      count: val.count,
      lastFired: val.lastFired,
      brightness: calculateBrightness(val.lastFired, Date.now()),
      color: parentColor,
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
  // Base size grows with count, capped - minimum size for visibility
  const baseSize = node.count > 0
    ? Math.min(140, 70 + Math.log10(node.count + 1) * 35)
    : 60;

  // Glow intensity based on brightness
  const glowIntensity = node.brightness;

  // Use domain color for glow when active, otherwise use the accent yellow
  const activeGlow = node.brightness > 0.3;
  const glowColor = activeGlow
    ? `${node.color}${Math.round(glowIntensity * 255).toString(16).padStart(2, '0')}`
    : 'transparent';
  const glowSize = 20 + glowIntensity * 50;

  // Dim appearance for inactive domains
  const isDormant = node.count === 0 && node.brightness === 0;

  return (
    <motion.div
      initial={{ scale: 0, opacity: 0 }}
      animate={{
        scale: 1,
        opacity: isDormant ? 0.5 : 1,
      }}
      exit={{ scale: 0, opacity: 0 }}
      whileHover={{ scale: 1.1 }}
      whileTap={{ scale: 0.95 }}
      onClick={onClick}
      className={cn(
        "relative cursor-pointer select-none",
        "rounded-full flex flex-col items-center justify-center",
        "transition-all duration-300",
        isSelected ? "ring-2 ring-offset-2 ring-offset-background" : "",
        isDormant && "opacity-40"
      )}
      style={{
        width: baseSize,
        height: baseSize,
        background: `radial-gradient(circle at 30% 30%,
          ${node.color}22 0%,
          ${node.color}11 50%,
          hsl(var(--muted)) 100%)`,
        boxShadow: glowIntensity > 0.1
          ? `0 0 ${glowSize}px ${glowColor},
             0 0 ${glowSize * 2}px ${glowColor.slice(0, -2)}66,
             inset 0 0 ${glowSize / 2}px ${glowColor.slice(0, -2)}44`
          : `0 4px 20px rgba(0, 0, 0, 0.2)`,
        border: `2px solid ${glowIntensity > 0.1 ? node.color : 'hsl(var(--border))'}`,
        ringColor: node.color,
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
            background: `radial-gradient(circle at center, ${node.color}88 0%, transparent 70%)`,
          }}
        />
      )}

      {/* Domain label */}
      <span
        className={cn(
          "text-xs font-bold uppercase tracking-wider z-10",
          glowIntensity > 0.5 ? "text-white" : "text-foreground",
          isDormant && "text-muted-foreground"
        )}
        style={{
          textShadow: glowIntensity > 0.3
            ? `0 0 10px ${node.color}`
            : 'none',
        }}
      >
        {node.label}
      </span>

      {/* Event count */}
      <span
        className={cn(
          "text-[10px] font-mono z-10 mt-0.5",
          glowIntensity > 0.5 ? "text-white/80" : "text-muted-foreground"
        )}
      >
        {node.count > 0 ? node.count : '-'}
      </span>
    </motion.div>
  );
}

export default EventCloud;
