import { useState, useEffect, useRef, useCallback } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { X } from 'lucide-react';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';

interface EventNode {
  id: string;
  label: string;
  count: number;
  lastFired: number;
  brightness: number;
}

interface GraphNode extends EventNode {
  x: number;
  y: number;
  vx: number;
  vy: number;
}

interface EventGraphProps {
  parentId: string;
  parentLabel: string;
  children: EventNode[];
  onClose: () => void;
}

// Calculate brightness based on recency (0-1)
const calculateBrightness = (lastFired: number, now: number): number => {
  const elapsed = now - lastFired;
  const decayTime = 3000;
  return Math.max(0, 1 - elapsed / decayTime);
};

export function EventGraph({ parentId, parentLabel, children, onClose }: EventGraphProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [nodes, setNodes] = useState<GraphNode[]>([]);
  const [parentPos, setParentPos] = useState({ x: 0, y: 0 });
  const animationRef = useRef<number>();
  const [dimensions, setDimensions] = useState({ width: 0, height: 0 });

  // Initialize node positions in a circle around parent
  useEffect(() => {
    if (!containerRef.current) return;

    const { width, height } = containerRef.current.getBoundingClientRect();
    setDimensions({ width, height });

    const centerX = width / 2;
    const centerY = height / 2;
    setParentPos({ x: centerX, y: centerY });

    const radius = Math.min(width, height) * 0.35;
    const angleStep = (2 * Math.PI) / children.length;

    const initialNodes: GraphNode[] = children.map((child, i) => {
      const angle = angleStep * i - Math.PI / 2;
      return {
        ...child,
        x: centerX + Math.cos(angle) * radius,
        y: centerY + Math.sin(angle) * radius,
        vx: 0,
        vy: 0,
      };
    });

    setNodes(initialNodes);
  }, [children]);

  // Simple physics simulation for organic movement
  useEffect(() => {
    if (nodes.length === 0 || dimensions.width === 0) return;

    const simulate = () => {
      setNodes(prev => {
        const updated = prev.map(node => {
          // Spring force toward original position
          const targetAngle = Math.atan2(node.y - parentPos.y, node.x - parentPos.x);
          const targetRadius = Math.min(dimensions.width, dimensions.height) * 0.35;
          const targetX = parentPos.x + Math.cos(targetAngle) * targetRadius;
          const targetY = parentPos.y + Math.sin(targetAngle) * targetRadius;

          // Apply spring force
          const springK = 0.02;
          const ax = (targetX - node.x) * springK;
          const ay = (targetY - node.y) * springK;

          // Add slight random jitter for liveliness
          const jitter = 0.1;
          const jitterX = (Math.random() - 0.5) * jitter;
          const jitterY = (Math.random() - 0.5) * jitter;

          // Apply damping
          const damping = 0.9;
          const nvx = (node.vx + ax + jitterX) * damping;
          const nvy = (node.vy + ay + jitterY) * damping;

          return {
            ...node,
            x: node.x + nvx,
            y: node.y + nvy,
            vx: nvx,
            vy: nvy,
            brightness: calculateBrightness(node.lastFired, Date.now()),
          };
        });

        return updated;
      });

      animationRef.current = requestAnimationFrame(simulate);
    };

    animationRef.current = requestAnimationFrame(simulate);
    return () => {
      if (animationRef.current) cancelAnimationFrame(animationRef.current);
    };
  }, [nodes.length, dimensions, parentPos]);

  return (
    <motion.div
      ref={containerRef}
      initial={{ opacity: 0, scale: 0.9 }}
      animate={{ opacity: 1, scale: 1 }}
      exit={{ opacity: 0, scale: 0.9 }}
      className="fixed inset-0 z-50 bg-background/95 backdrop-blur-md"
    >
      {/* Close button */}
      <Button
        variant="ghost"
        size="icon"
        className="absolute top-4 right-4 z-10"
        onClick={onClose}
      >
        <X className="w-5 h-5" />
      </Button>

      {/* Graph title */}
      <div className="absolute top-4 left-4 z-10">
        <h2 className="text-lg font-bold tracking-tight">{parentLabel}</h2>
        <p className="text-xs text-muted-foreground">
          {children.length} sub-events
        </p>
      </div>

      {/* SVG for connection lines */}
      <svg className="absolute inset-0 w-full h-full pointer-events-none">
        <defs>
          <linearGradient id="lineGradient" x1="0%" y1="0%" x2="100%" y2="0%">
            <stop offset="0%" stopColor="hsl(var(--primary))" stopOpacity="0.3" />
            <stop offset="50%" stopColor="hsl(var(--primary))" stopOpacity="0.6" />
            <stop offset="100%" stopColor="hsl(var(--primary))" stopOpacity="0.3" />
          </linearGradient>
        </defs>
        {nodes.map(node => (
          <motion.line
            key={`line-${node.id}`}
            x1={parentPos.x}
            y1={parentPos.y}
            x2={node.x}
            y2={node.y}
            stroke="url(#lineGradient)"
            strokeWidth={1 + node.brightness * 2}
            initial={{ pathLength: 0 }}
            animate={{ pathLength: 1 }}
            transition={{ duration: 0.5, delay: 0.2 }}
          />
        ))}
      </svg>

      {/* Parent node */}
      <motion.div
        className="absolute -translate-x-1/2 -translate-y-1/2"
        style={{ left: parentPos.x, top: parentPos.y }}
        initial={{ scale: 0 }}
        animate={{ scale: 1 }}
        transition={{ type: 'spring', stiffness: 200, damping: 20 }}
      >
        <div
          className={cn(
            "w-20 h-20 rounded-full",
            "flex items-center justify-center",
            "bg-primary text-primary-foreground",
            "font-bold text-xs uppercase tracking-wider",
            "shadow-lg shadow-primary/30"
          )}
        >
          {parentLabel}
        </div>
      </motion.div>

      {/* Child nodes */}
      <AnimatePresence>
        {nodes.map((node, index) => {
          const glowIntensity = node.brightness;
          const glowColor = `rgba(255, 220, 50, ${glowIntensity * 0.8})`;
          const glowSize = 15 + glowIntensity * 30;
          const nodeSize = 50 + Math.log10(node.count + 1) * 20;

          return (
            <motion.div
              key={node.id}
              className="absolute -translate-x-1/2 -translate-y-1/2 cursor-pointer"
              style={{ left: node.x, top: node.y }}
              initial={{ scale: 0, opacity: 0 }}
              animate={{ scale: 1, opacity: 1 }}
              exit={{ scale: 0, opacity: 0 }}
              transition={{ delay: index * 0.05 }}
              whileHover={{ scale: 1.15 }}
            >
              <div
                className={cn(
                  "rounded-full flex flex-col items-center justify-center",
                  "transition-all duration-150"
                )}
                style={{
                  width: nodeSize,
                  height: nodeSize,
                  background: `radial-gradient(circle at center,
                    hsl(var(--card)) 0%,
                    hsl(var(--muted)) 100%)`,
                  boxShadow: glowIntensity > 0.1
                    ? `0 0 ${glowSize}px ${glowColor},
                       0 0 ${glowSize * 2}px ${glowColor.replace(String(glowIntensity * 0.8), String(glowIntensity * 0.4))}`
                    : `0 4px 15px rgba(0, 0, 0, 0.3)`,
                  border: `1.5px solid ${glowIntensity > 0.1 ? glowColor : 'hsl(var(--border))'}`,
                }}
              >
                {/* Flicker effect */}
                {glowIntensity > 0.5 && (
                  <motion.div
                    className="absolute inset-0 rounded-full"
                    animate={{ opacity: [0.2, 0.6, 0.2] }}
                    transition={{ duration: 0.15, repeat: Infinity }}
                    style={{
                      background: `radial-gradient(circle at center, ${glowColor} 0%, transparent 70%)`,
                    }}
                  />
                )}

                <span
                  className={cn(
                    "text-[9px] font-semibold z-10 text-center leading-tight px-1",
                    glowIntensity > 0.5 ? "text-yellow-100" : "text-foreground"
                  )}
                  style={{
                    textShadow: glowIntensity > 0.3 ? `0 0 8px ${glowColor}` : 'none',
                  }}
                >
                  {node.label}
                </span>
                <span
                  className={cn(
                    "text-[8px] font-mono z-10",
                    glowIntensity > 0.5 ? "text-yellow-200" : "text-muted-foreground"
                  )}
                >
                  {node.count}
                </span>
              </div>
            </motion.div>
          );
        })}
      </AnimatePresence>
    </motion.div>
  );
}

export default EventGraph;
