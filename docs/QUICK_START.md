# Candybar Quick Start Guide

## 🚀 5-Minute Setup

### 1. Install Dependencies
```bash
cd /home/delorenj/code/33GOD/candybar/trunk-main
npm install
```

### 2. Start RabbitMQ
```bash
# Check if running
sudo systemctl status rabbitmq-server

# Start if needed
sudo systemctl start rabbitmq-server
```

### 3. Launch Candybar
```bash
npm run dev
```

### 4. Connect to Bloodbank
1. Click **Connect** button (top-right)
2. Wait for green connection indicator
3. Events will stream automatically

## 📊 Common Tasks

### View All Recent Events
1. Set time range to "Last 5 minutes"
2. Switch to **List** view mode
3. Click events to see details

### Find Specific Event Type
1. Click **Filters** panel header to expand
2. Select domain (e.g., "Fireflies")
3. Select event type (e.g., "transcript.ready")
4. View filtered results

### Debug Agent Errors
1. Toggle **Errors Only** switch
2. Filter by **Agent** domain
3. Review error payloads in JSON viewer
4. Export for offline analysis

### Track Workflow Execution
1. Filter by **Workflow** domain
2. Search for workflow name
3. Watch `step.started` → `step.completed` flow
4. Switch to **Flow** view to visualize

### Export Events for Analysis
1. Apply desired filters
2. Click **Export JSON** button
3. Open in text editor or analysis tool

## 🎨 View Modes

- **List** - Traditional event stream + details panel
- **Cloud** - Animated domain bubbles (great for overview)
- **Flow** - Sankey diagram showing event paths (NEW!)

## 🔍 Pro Tips

1. **Combine filters** for precision targeting
2. **Use search** to find events by payload content
3. **Watch session IDs** to follow agent conversations
4. **Monitor error rate** in stats cards
5. **Export filtered sets** for sharing with team

## ⚡ Performance

- **Latency:** <250ms from Bloodbank → UI
- **Throughput:** 100+ events/second
- **Memory:** <50MB for 500 events
- **FPS:** Maintains 60fps during streaming

## 🔧 Troubleshooting

### No events showing?
✅ Check connection status (green dot)
✅ Verify RabbitMQ is running
✅ Clear all filters

### Connection failed?
✅ Check RabbitMQ: `sudo systemctl status rabbitmq-server`
✅ Verify port 5672 is open
✅ Review Tauri console logs

### Slow performance?
✅ Reduce time range filter
✅ Limit to specific domain
✅ Check FPS in performance dashboard

## 📚 Next Steps

- Read full **USER_GUIDE.md** for detailed features
- Review **IMPLEMENTATION_REPORT.md** for architecture
- Explore all 7 Bloodbank domains
- Set up custom filter presets

## 🎯 Key Features

✅ Real-time event streaming (<1s latency)
✅ 7 filter types (domain, type, source, time, session, errors, search)
✅ 3 view modes (list, cloud, flow)
✅ JSON payload viewer with syntax highlighting
✅ Export to JSON/CSV
✅ Performance monitoring
✅ Dark/light theme

---

**Need Help?** Check USER_GUIDE.md or contact Candybar Engineering Manager
