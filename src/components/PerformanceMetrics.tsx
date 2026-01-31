import { useState, useEffect } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Activity } from 'lucide-react';

interface PerformanceMetricsProps {
  eventCount: number;
}

export const PerformanceMetrics = ({ eventCount }: PerformanceMetricsProps) => {
  const [metrics, setMetrics] = useState({
    fps: 0,
    memoryUsage: 0,
    eventLatency: 0,
    renderTime: 0,
  });

  useEffect(() => {
    let frameCount = 0;
    let lastTime = performance.now();
    let animationFrameId: number;

    const measurePerformance = () => {
      frameCount++;
      const currentTime = performance.now();
      const elapsed = currentTime - lastTime;

      if (elapsed >= 1000) {
        const fps = Math.round((frameCount * 1000) / elapsed);
        frameCount = 0;
        lastTime = currentTime;

        // Get memory usage if available
        const memory = (performance as any).memory;
        const memoryUsage = memory
          ? Math.round((memory.usedJSHeapSize / memory.jsHeapSizeLimit) * 100)
          : 0;

        setMetrics((prev) => ({
          ...prev,
          fps,
          memoryUsage,
        }));
      }

      animationFrameId = requestAnimationFrame(measurePerformance);
    };

    animationFrameId = requestAnimationFrame(measurePerformance);

    return () => {
      if (animationFrameId) {
        cancelAnimationFrame(animationFrameId);
      }
    };
  }, []);

  const getFpsColor = (fps: number) => {
    if (fps >= 50) return 'text-green-500';
    if (fps >= 30) return 'text-yellow-500';
    return 'text-red-500';
  };

  return (
    <Card className="shadow-md border-border/50 bg-background/50 backdrop-blur-sm">
      <CardHeader className="border-b bg-muted/30 py-2">
        <div className="flex items-center justify-between">
          <CardTitle className="text-sm flex items-center gap-2">
            <Activity className="w-4 h-4" />
            Performance
          </CardTitle>
        </div>
      </CardHeader>
      <CardContent className="p-3">
        <div className="grid grid-cols-3 gap-3">
          <div className="text-center">
            <div className="text-[10px] text-muted-foreground uppercase tracking-wider mb-1">
              FPS
            </div>
            <div className={`text-lg font-bold ${getFpsColor(metrics.fps)}`}>
              {metrics.fps}
            </div>
          </div>
          <div className="text-center">
            <div className="text-[10px] text-muted-foreground uppercase tracking-wider mb-1">
              Memory
            </div>
            <div className="text-lg font-bold">
              {metrics.memoryUsage}
              <span className="text-xs">%</span>
            </div>
          </div>
          <div className="text-center">
            <div className="text-[10px] text-muted-foreground uppercase tracking-wider mb-1">
              Events
            </div>
            <div className="text-lg font-bold">{eventCount}</div>
          </div>
        </div>
        {metrics.fps < 30 && (
          <div className="mt-2 p-2 bg-yellow-500/10 border border-yellow-500/20 rounded text-xs text-yellow-600 dark:text-yellow-400">
            ⚠️ Performance degraded. Consider reducing event buffer size.
          </div>
        )}
      </CardContent>
    </Card>
  );
};
