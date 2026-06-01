## Variant: Contract guard

### Design stance
Policy-as-UI: make schema/naming violations impossible to ignore before they become replay storms.

### Key choices
- Layout: split (violations queue | rule-aware inspector)
- Typography: investigation-friendly (small caps severity + monospace type snippets)
- Color: severity-led accents (high/medium/low)
- Interaction: click violation, inspect why, trigger next action (task/mute/copy fix guidance)

### Trade-offs
- Strong at: hardening envelope correctness and preventing Candystore ingestion churn
- Weak at: broad stream awareness and long session exploration

### Best for
Reliability loops where naming/schema drift and CloudEvents contract hygiene are the top priority.
