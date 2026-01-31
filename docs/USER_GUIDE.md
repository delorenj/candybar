# Candybar User Guide

## Overview

Candybar is a real-time event monitoring desktop application for the 33GOD ecosystem. It visualizes and analyzes events from Bloodbank's RabbitMQ event bus, providing comprehensive filtering, search, and visualization capabilities.

## Getting Started

### Launching Candybar

**Development Mode:**
```bash
cd /home/delorenj/code/33GOD/candybar/trunk-main
npm run dev
```

**Production Build:**
```bash
npm run tauri build
# Binary will be in src-tauri/target/release/
```

### Connecting to Bloodbank

1. Click the **Connect** button in the top-right corner
2. Default configuration:
   - Host: `localhost`
   - Port: `5672`
   - Exchange: `bloodbank.events.v1`
   - Routing Key: `#` (all events)

3. Connection status indicators:
   - 🟢 **Green dot** - Connected
   - 🔴 **Red dot** - Disconnected
   - ⏳ **Spinner** - Connecting

## Interface Overview

### Header

- **Logo & Title** - Candybar branding
- **View Mode Selector** - Switch between List, Cloud, and Flow views
- **Export Button** - Export filtered events to JSON
- **Connection Button** - Connect/disconnect from RabbitMQ
- **Status Badge** - Shows connection state
- **Theme Toggle** - Switch between light/dark mode

### Statistics Cards

Four real-time metrics displayed at the top:
1. **Total Events** - Count of all received events
2. **Events/Min** - Events received in last 60 seconds
3. **Error Rate** - Percentage of error/failed events
4. **Domains** - Number of unique domains seen

### Filters Panel

Expandable panel with 7 filter types:

#### 1. Domain Filter
Select from 7 Bloodbank domains:
- 🔵 **Agent** - Agent thread interactions
- 🟢 **Artifact** - File management
- 🟠 **Fireflies** - Meeting transcriptions
- 🟣 **GitHub** - GitHub integration
- 🟣 **LLM** - LLM provider interactions
- 🩷 **TheBoard** - Multi-agent meetings
- 🩵 **Workflow** - n8n workflow events

#### 2. Event Type Filter
Cascading filter based on selected domain:
- Automatically shows event types for selected domain
- Example: `fireflies` → `transcript.upload`, `transcript.ready`, etc.

#### 3. Source Application
Filter by the application that published the event:
- Dropdown populated from received events
- Example: `fireflies-watcher`, `github-webhook`, `claude-agent`

#### 4. Time Range
Filter events by recency:
- **Last 5 minutes**
- **Last 15 minutes**
- **Last hour**
- **Last 6 hours**
- **Last 24 hours**
- **All time**

#### 5. Session ID
Filter by agent session:
- Dropdown populated from events with `agent_context.session_id`
- Useful for tracking specific agent conversations

#### 6. Search in Payload
Full-text search across:
- Event type
- Payload JSON (fuzzy search)

#### 7. Errors Only
Toggle to show only error/failed events:
- Matches event types containing `error` or `failed`

### Filter Actions
- **Active filter count** - Badge showing how many filters are active
- **Clear button** - One-click to reset all filters
- **Expand/collapse** - Click header to show/hide filters

## View Modes

### 1. List View (Default)

**Event Stream (Left Panel)**
- Scrollable list of events (most recent first)
- Shows up to 50 events
- Color-coded by domain
- Icons indicate event status:
  - ⚠️ **Alert** - Error/failed events
  - ✅ **Check** - Completed/ready events
  - ⚡ **Lightning** - Started/prompt events
  - 📊 **Activity** - Other events

**Event Details (Right Panel)**
- Click any event to view details
- **Metadata:**
  - Event type
  - Source application
  - Event ID (UUID)
  - Correlation IDs
- **Payload:**
  - Syntax-highlighted JSON viewer
  - Expandable/collapsible tree
  - Copy to clipboard
  - Export individual event
  - Shows size, line count, depth

### 2. Cloud View

Animated bubble visualization:
- Each bubble represents a domain
- Size indicates event count
- Color matches domain color
- Click to explore sub-events
- Real-time pulsing animation

### 3. Flow View (NEW)

Three-column Sankey-style diagram:

**Column 1: Sources**
- Applications publishing events
- Blue progress bars
- Event counts

**Column 2: Events**
- Event types with domain colors
- Color-coded progress bars
- Event counts

**Column 3: Targets**
- Agents/systems consuming events
- Purple progress bars
- Event counts

**Flow Paths Section**
- Top 10 most active flows
- Format: `source → event → target`
- Shows flow frequency

## Export Features

### Export All Filtered Events (JSON)
1. Apply desired filters
2. Click **Export JSON** button
3. File saved as `candybar-events-{timestamp}.json`
4. Contains full event envelopes with payloads

### Export Individual Event
1. Select event in List View
2. Click **Export** in JSON viewer
3. File saved as `event-{event-id}.json`

### Export to CSV (via code)
Call `handleExportEventCSV()` function:
- Exports metadata only (no payloads)
- Columns: Event ID, Type, Timestamp, Source, Domain, Session
- Useful for spreadsheet analysis

## Performance Monitoring

Optional performance dashboard displays:
- **FPS** - Frame rate (should be 50-60)
- **Memory** - JavaScript heap usage percentage
- **Events** - Total event count

**Performance Warning:**
If FPS drops below 30, consider:
- Reducing max events buffer
- Clearing old events
- Closing other applications

## Tips & Best Practices

### Efficient Filtering
1. Start with broad filters (domain, time range)
2. Add specific filters (event type, source)
3. Use search for precise payload matching
4. Clear filters when done to see all events

### Monitoring Workflows
1. Filter by `workflow` domain
2. Add time range for recent workflows
3. Search for specific workflow names
4. Watch for `step.failed` events

### Debugging Agent Issues
1. Filter by `agent` domain
2. Select specific session ID
3. Watch for `thread.error` events
4. Export filtered events for offline analysis

### Tracking File Changes
1. Filter by `artifact` domain
2. Use search to find file names
3. Watch `file.created`, `file.updated`, `file.deleted`

### GitHub Integration Monitoring
1. Filter by `github` domain
2. Watch for `pr.created`, `pr.merged`, `commit.pushed`
3. Track correlation IDs for related events

## Keyboard Shortcuts

(To be implemented in future versions)
- `Ctrl/Cmd + F` - Focus search
- `Ctrl/Cmd + K` - Toggle filters
- `Ctrl/Cmd + 1/2/3` - Switch view modes
- `Ctrl/Cmd + E` - Export events
- `Ctrl/Cmd + R` - Reconnect
- `Escape` - Close dialogs

## Troubleshooting

### No Events Appearing
1. Check RabbitMQ connection status (green dot)
2. Verify RabbitMQ server is running
3. Check routing key configuration (should be `#`)
4. Verify Bloodbank is publishing events
5. Clear filters to ensure nothing is filtered out

### Connection Failed
1. Check RabbitMQ server is running: `sudo systemctl status rabbitmq-server`
2. Verify host/port configuration
3. Check firewall rules
4. Test with: `telnet localhost 5672`
5. Review Tauri console logs

### Slow Performance
1. Check FPS in performance dashboard
2. Reduce max events (currently 500)
3. Use time range filter to limit events
4. Clear event buffer periodically
5. Close other resource-intensive applications

### Events Not Filtered Correctly
1. Check active filter count badge
2. Verify filter selections in panel
3. Try clearing and re-applying filters
4. Check for typos in search text
5. Ensure domain/event type match

### Export Failed
1. Check file permissions in download directory
2. Verify disk space
3. Try exporting smaller event set
4. Check browser console for errors

## Advanced Features

### Correlation ID Tracking
- Events with same correlation ID are related
- Click correlation ID to filter (future feature)
- Export events with correlation IDs for analysis

### Session Tracking
- Agent sessions tracked via `agent_context.session_id`
- Filter by session to follow conversation
- Useful for debugging agent behavior

### Error Analysis
1. Toggle "Errors Only" filter
2. Review error payloads
3. Track error frequency over time
4. Export for offline analysis

## Data Privacy & Security

- **Local Only** - No data sent to external servers
- **No Persistence** - Events cleared on app close
- **Memory Limited** - Max 500 events in buffer
- **Secure Connection** - RabbitMQ credentials stored securely

## Support & Feedback

For issues or feature requests:
1. Check this user guide
2. Review implementation report in `docs/IMPLEMENTATION_REPORT.md`
3. Contact Candybar Engineering Manager
4. Refer to 33GOD ecosystem documentation

## Version Information

- **Version:** 0.1.0
- **Last Updated:** 2026-01-27
- **Platform:** Linux, macOS (planned), Windows (planned)
- **Framework:** Tauri v2 + React 19 + TypeScript
