## Variant: Session investigator

### Design stance
Correlation-first investigation cockpit: one active thread, many forensic affordances.

### Key choices
- Layout: 3 columns (timeline | detail tabs | KPIs)
- Typography: compact event narrative with monospace for raw envelope segments
- Color: restrained dark palette to keep long timeline scans low-fatigue
- Interaction: click timeline steps, swap summary/raw/related tabs, quick correlation lookup

### Trade-offs
- Strong at: root-cause analysis, causation/order reasoning, high-signal event detail review
- Weak at: broad live fleet monitoring across many unrelated streams

### Best for
When an operator has a correlation ID (or suspicious session) and needs to understand exactly what happened and why.
