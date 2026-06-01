## Variant: Ops overview

### Design stance
NOC-style command deck: immediate system state first, then stream, then envelope details.

### Key choices
- Layout: 3 columns (schema controls | live feed | diagnostics + envelope)
- Typography: compact mono for feed rows, sans for controls and metrics
- Color: dark operational palette with kind-specific chips (event/command/reply)
- Interaction: kind filter pills + click-to-inspect envelope

### Trade-offs
- Strong at: fast triage during incidents, quick filter toggles, seeing contract failures in context
- Weak at: deep per-session investigations (timeline is not first-class)

### Best for
Operators watching a live stream who need to suppress noise and spot transport/contract health drift quickly.
