import { useState, useMemo } from 'react';
import { Copy, Check, ChevronRight, ChevronDown, Download } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { ScrollArea } from '@/components/ui/scroll-area';
import { cn } from '@/lib/utils';

interface JsonViewerProps {
  data: any;
  maxHeight?: string;
  onExport?: () => void;
}

interface JsonNodeProps {
  data: any;
  name?: string;
  level?: number;
  isLast?: boolean;
}

const JsonNode = ({ data, name, level = 0, isLast = true }: JsonNodeProps) => {
  const [isExpanded, setIsExpanded] = useState(level < 2);

  const indent = level * 16;

  if (data === null) {
    return (
      <div style={{ paddingLeft: `${indent}px` }} className="font-mono text-xs">
        {name && <span className="text-blue-400">{name}: </span>}
        <span className="text-purple-400">null</span>
        {!isLast && <span className="text-muted-foreground">,</span>}
      </div>
    );
  }

  if (typeof data === 'boolean') {
    return (
      <div style={{ paddingLeft: `${indent}px` }} className="font-mono text-xs">
        {name && <span className="text-blue-400">{name}: </span>}
        <span className="text-orange-400">{data.toString()}</span>
        {!isLast && <span className="text-muted-foreground">,</span>}
      </div>
    );
  }

  if (typeof data === 'number') {
    return (
      <div style={{ paddingLeft: `${indent}px` }} className="font-mono text-xs">
        {name && <span className="text-blue-400">{name}: </span>}
        <span className="text-green-400">{data}</span>
        {!isLast && <span className="text-muted-foreground">,</span>}
      </div>
    );
  }

  if (typeof data === 'string') {
    return (
      <div style={{ paddingLeft: `${indent}px` }} className="font-mono text-xs">
        {name && <span className="text-blue-400">{name}: </span>}
        <span className="text-yellow-400">"{data}"</span>
        {!isLast && <span className="text-muted-foreground">,</span>}
      </div>
    );
  }

  if (Array.isArray(data)) {
    if (data.length === 0) {
      return (
        <div style={{ paddingLeft: `${indent}px` }} className="font-mono text-xs">
          {name && <span className="text-blue-400">{name}: </span>}
          <span className="text-muted-foreground">[]</span>
          {!isLast && <span className="text-muted-foreground">,</span>}
        </div>
      );
    }

    return (
      <div style={{ paddingLeft: `${indent}px` }} className="font-mono text-xs">
        <div
          className="flex items-center gap-1 cursor-pointer hover:bg-accent/50 -ml-5 pl-5 pr-2 rounded"
          onClick={() => setIsExpanded(!isExpanded)}
        >
          {isExpanded ? (
            <ChevronDown className="w-3 h-3 text-muted-foreground" />
          ) : (
            <ChevronRight className="w-3 h-3 text-muted-foreground" />
          )}
          {name && <span className="text-blue-400">{name}: </span>}
          <span className="text-muted-foreground">
            [{!isExpanded && `${data.length} items`}
          </span>
        </div>
        {isExpanded && (
          <div>
            {data.map((item, i) => (
              <JsonNode
                key={i}
                data={item}
                level={level + 1}
                isLast={i === data.length - 1}
              />
            ))}
            <div style={{ paddingLeft: `${indent}px` }} className="text-muted-foreground">
              ]{!isLast && ','}
            </div>
          </div>
        )}
        {!isExpanded && (
          <span className="text-muted-foreground">]{!isLast && ','}</span>
        )}
      </div>
    );
  }

  if (typeof data === 'object') {
    const entries = Object.entries(data);

    if (entries.length === 0) {
      return (
        <div style={{ paddingLeft: `${indent}px` }} className="font-mono text-xs">
          {name && <span className="text-blue-400">{name}: </span>}
          <span className="text-muted-foreground">{'{}'}</span>
          {!isLast && <span className="text-muted-foreground">,</span>}
        </div>
      );
    }

    return (
      <div style={{ paddingLeft: `${indent}px` }} className="font-mono text-xs">
        <div
          className="flex items-center gap-1 cursor-pointer hover:bg-accent/50 -ml-5 pl-5 pr-2 rounded"
          onClick={() => setIsExpanded(!isExpanded)}
        >
          {isExpanded ? (
            <ChevronDown className="w-3 h-3 text-muted-foreground" />
          ) : (
            <ChevronRight className="w-3 h-3 text-muted-foreground" />
          )}
          {name && <span className="text-blue-400">{name}: </span>}
          <span className="text-muted-foreground">
            {'{'}{!isExpanded && `${entries.length} keys`}
          </span>
        </div>
        {isExpanded && (
          <div>
            {entries.map(([key, value], i) => (
              <JsonNode
                key={key}
                name={key}
                data={value}
                level={level + 1}
                isLast={i === entries.length - 1}
              />
            ))}
            <div style={{ paddingLeft: `${indent}px` }} className="text-muted-foreground">
              {'}'}{!isLast && ','}
            </div>
          </div>
        )}
        {!isExpanded && (
          <span className="text-muted-foreground">{'}'}{!isLast && ','}</span>
        )}
      </div>
    );
  }

  return null;
};

export const JsonViewer = ({ data, maxHeight = '400px', onExport }: JsonViewerProps) => {
  const [copied, setCopied] = useState(false);

  const jsonString = useMemo(() => JSON.stringify(data, null, 2), [data]);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(jsonString);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error('Failed to copy:', err);
    }
  };

  const stats = useMemo(() => {
    const size = new Blob([jsonString]).size;
    const lines = jsonString.split('\n').length;

    const getDepth = (obj: any, currentDepth = 0): number => {
      if (typeof obj !== 'object' || obj === null) return currentDepth;
      const depths = Object.values(obj).map((val) => getDepth(val, currentDepth + 1));
      return Math.max(currentDepth, ...depths);
    };

    return {
      size: size < 1024 ? `${size} B` : `${(size / 1024).toFixed(1)} KB`,
      lines,
      depth: getDepth(data),
    };
  }, [jsonString, data]);

  return (
    <div className="space-y-2">
      {/* Toolbar */}
      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <div className="flex items-center gap-3">
          <span>{stats.size}</span>
          <span>·</span>
          <span>{stats.lines} lines</span>
          <span>·</span>
          <span>depth {stats.depth}</span>
        </div>
        <div className="flex items-center gap-1">
          {onExport && (
            <Button
              variant="ghost"
              size="sm"
              onClick={onExport}
              className="h-7 px-2"
            >
              <Download className="w-3 h-3 mr-1" />
              Export
            </Button>
          )}
          <Button
            variant="ghost"
            size="sm"
            onClick={handleCopy}
            className="h-7 px-2"
          >
            {copied ? (
              <>
                <Check className="w-3 h-3 mr-1 text-green-500" />
                Copied
              </>
            ) : (
              <>
                <Copy className="w-3 h-3 mr-1" />
                Copy
              </>
            )}
          </Button>
        </div>
      </div>

      {/* JSON Tree */}
      <ScrollArea
        className={cn(
          'w-full rounded-md border bg-muted/30 p-4',
          'dark:bg-slate-950/50'
        )}
        style={{ height: maxHeight }}
      >
        <JsonNode data={data} />
      </ScrollArea>
    </div>
  );
};
