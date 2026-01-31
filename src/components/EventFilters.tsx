import { useState, useMemo } from 'react';
import { Search, Filter, X, Calendar, AlertCircle } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { BLOODBANK_DOMAINS, EVENT_TYPES, DomainKey } from "@/types/bloodbank";

export interface EventFilter {
  domain: string;
  eventType: string;
  sourceApp: string;
  searchText: string;
  timeRange: 'all' | '5min' | '15min' | '1hr' | '6hr' | '24hr';
  sessionId: string;
  errorsOnly: boolean;
}

interface EventFiltersProps {
  filters: EventFilter;
  onFilterChange: (filters: EventFilter) => void;
  availableSources: string[];
  availableSessions: string[];
  onClearFilters: () => void;
}

export const EventFilters = ({
  filters,
  onFilterChange,
  availableSources,
  availableSessions,
  onClearFilters,
}: EventFiltersProps) => {
  const [isExpanded, setIsExpanded] = useState(false);

  const activeFilterCount = useMemo(() => {
    let count = 0;
    if (filters.domain !== 'all') count++;
    if (filters.eventType !== 'all') count++;
    if (filters.sourceApp !== 'all') count++;
    if (filters.searchText) count++;
    if (filters.timeRange !== 'all') count++;
    if (filters.sessionId) count++;
    if (filters.errorsOnly) count++;
    return count;
  }, [filters]);

  const availableEventTypes = useMemo(() => {
    if (filters.domain === 'all') {
      return Object.values(EVENT_TYPES).flat();
    }
    return EVENT_TYPES[filters.domain as DomainKey] || [];
  }, [filters.domain]);

  const handleFilterChange = (key: keyof EventFilter, value: any) => {
    const newFilters = { ...filters, [key]: value };

    // Reset event type if domain changes
    if (key === 'domain') {
      newFilters.eventType = 'all';
    }

    onFilterChange(newFilters);
  };

  return (
    <Card className="shadow-md border-border/50 bg-background/50 backdrop-blur-sm">
      <CardHeader className="border-b bg-muted/30 py-3 cursor-pointer" onClick={() => setIsExpanded(!isExpanded)}>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Filter className="w-4 h-4" />
            <CardTitle className="text-base">Filters</CardTitle>
            {activeFilterCount > 0 && (
              <Badge variant="secondary" className="font-mono text-xs">
                {activeFilterCount} active
              </Badge>
            )}
          </div>
          <div className="flex items-center gap-2">
            {activeFilterCount > 0 && (
              <Button
                variant="ghost"
                size="sm"
                onClick={(e) => {
                  e.stopPropagation();
                  onClearFilters();
                }}
                className="h-7 px-2 text-xs"
              >
                <X className="w-3 h-3 mr-1" />
                Clear
              </Button>
            )}
            <Button
              variant="ghost"
              size="sm"
              className="h-7 w-7 p-0"
            >
              {isExpanded ? '−' : '+'}
            </Button>
          </div>
        </div>
      </CardHeader>

      {isExpanded && (
        <CardContent className="p-4 space-y-4">
          {/* Search Text */}
          <div className="space-y-2">
            <label className="text-xs font-medium text-muted-foreground">Search in Payload</label>
            <div className="relative">
              <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
              <Input
                placeholder="Search events..."
                value={filters.searchText}
                onChange={(e) => handleFilterChange('searchText', e.target.value)}
                className="pl-9"
              />
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4">
            {/* Domain Filter */}
            <div className="space-y-2">
              <label className="text-xs font-medium text-muted-foreground">Domain</label>
              <Select value={filters.domain} onValueChange={(value) => handleFilterChange('domain', value)}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All Domains</SelectItem>
                  {Object.entries(BLOODBANK_DOMAINS).map(([key, domain]) => (
                    <SelectItem key={key} value={key}>
                      <div className="flex items-center gap-2">
                        <div className="w-2 h-2 rounded-full" style={{ backgroundColor: domain.color }} />
                        {domain.label}
                      </div>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {/* Event Type Filter */}
            <div className="space-y-2">
              <label className="text-xs font-medium text-muted-foreground">Event Type</label>
              <Select
                value={filters.eventType}
                onValueChange={(value) => handleFilterChange('eventType', value)}
                disabled={filters.domain === 'all'}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All Events</SelectItem>
                  {availableEventTypes.map((eventType) => (
                    <SelectItem key={eventType} value={eventType}>
                      {eventType}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {/* Source App Filter */}
            <div className="space-y-2">
              <label className="text-xs font-medium text-muted-foreground">Source Application</label>
              <Select value={filters.sourceApp} onValueChange={(value) => handleFilterChange('sourceApp', value)}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All Sources</SelectItem>
                  {availableSources.map((source) => (
                    <SelectItem key={source} value={source}>
                      {source}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {/* Time Range Filter */}
            <div className="space-y-2">
              <label className="text-xs font-medium text-muted-foreground flex items-center gap-1">
                <Calendar className="w-3 h-3" />
                Time Range
              </label>
              <Select value={filters.timeRange} onValueChange={(value: any) => handleFilterChange('timeRange', value)}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All Time</SelectItem>
                  <SelectItem value="5min">Last 5 minutes</SelectItem>
                  <SelectItem value="15min">Last 15 minutes</SelectItem>
                  <SelectItem value="1hr">Last hour</SelectItem>
                  <SelectItem value="6hr">Last 6 hours</SelectItem>
                  <SelectItem value="24hr">Last 24 hours</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>

          {/* Session ID Filter */}
          {availableSessions.length > 0 && (
            <div className="space-y-2">
              <label className="text-xs font-medium text-muted-foreground">Session ID</label>
              <Select value={filters.sessionId} onValueChange={(value) => handleFilterChange('sessionId', value)}>
                <SelectTrigger>
                  <SelectValue placeholder="All sessions" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="">All Sessions</SelectItem>
                  {availableSessions.map((session) => (
                    <SelectItem key={session} value={session}>
                      <span className="font-mono text-xs">{session}</span>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}

          {/* Errors Only Toggle */}
          <div className="flex items-center justify-between p-3 bg-muted/30 rounded-md">
            <div className="flex items-center gap-2">
              <AlertCircle className="w-4 h-4 text-destructive" />
              <label className="text-sm font-medium">Errors Only</label>
            </div>
            <Switch
              checked={filters.errorsOnly}
              onCheckedChange={(checked) => handleFilterChange('errorsOnly', checked)}
            />
          </div>
        </CardContent>
      )}
    </Card>
  );
};
