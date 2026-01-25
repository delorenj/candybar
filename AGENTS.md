No final analysis generated

# Project Directory Structure
---


<project_structure>
├── 📁 .claude
├── 📁 .claude-flow
│   └── 📁 metrics
│       ├── 📋 agent-metrics.json
│       ├── 📋 performance.json
│       └── 📋 task-metrics.json
├── 📁 .github
│   ├── 📁 workflows
│   │   ├── 📋 build-macos.yml
│   │   ├── 📋 build.yml
│   │   └── 📋 eslint_clippy.yml
│   ├── 📋 dependabot.yml
│   └── 📋 FUNDING.yml
├── 📁 .swarm
├── 📁 src
│   ├── 📁 components
│   │   ├── 📁 magicui
│   │   │   ├── ⚛️ border-beam.tsx
│   │   │   ├── ⚛️ dot-pattern.tsx
│   │   │   ├── ⚛️ number-ticker.tsx
│   │   │   └── ⚛️ shine-border.tsx
│   │   ├── 📁 ui
│   │   │   ├── ⚛️ badge.tsx
│   │   │   ├── ⚛️ button.tsx
│   │   │   ├── ⚛️ card.tsx
│   │   │   ├── ⚛️ scroll-area.tsx
│   │   │   └── ⚛️ separator.tsx
│   │   ├── ⚛️ EventCloud.tsx
│   │   ├── ⚛️ EventGraph.tsx
│   │   ├── ⚛️ ModeToggle.tsx
│   │   └── ⚛️ ThemeProvider.tsx
│   ├── 📁 hooks
│   │   └── 💠 useRabbitMQ.ts
│   ├── 📁 lib
│   │   └── 💠 utils.ts
│   ├── 📁 pages
│   │   └── ⚛️ Home.tsx
│   ├── 📁 types
│   │   └── 💠 bloodbank.ts
│   ├── ⚛️ App.tsx
│   ├── 🎨 index.css
│   └── ⚛️ main.tsx
├── 📁 src-next
│   ├── 📁 app
│   │   ├── ⚛️ layout.tsx
│   │   └── ⚛️ page.tsx
│   ├── 📁 components
│   │   ├── 📁 AppLayout
│   │   │   ├── ⚛️ index.tsx
│   │   │   └── 🎨 style.css
│   │   ├── 📁 HomeBlock
│   │   │   ├── ⚛️ index.tsx
│   │   │   └── 🎨 style.css
│   │   ├── 📁 magicui
│   │   │   ├── ⚛️ border-beam.tsx
│   │   │   ├── ⚛️ dot-pattern.tsx
│   │   │   ├── ⚛️ number-ticker.tsx
│   │   │   └── ⚛️ shine-border.tsx
│   │   ├── 📁 ui
│   │   │   ├── ⚛️ badge.tsx
│   │   │   ├── ⚛️ button.tsx
│   │   │   ├── ⚛️ card.tsx
│   │   │   ├── ⚛️ scroll-area.tsx
│   │   │   └── ⚛️ separator.tsx
│   │   ├── ⚛️ ModeToggle.tsx
│   │   └── ⚛️ ThemeProvider.tsx
│   ├── 📁 containers
│   │   └── 📁 Home
│   │       ├── ⚛️ index.tsx
│   │       └── 🎨 style.css
│   ├── 📁 css
│   │   └── 🎨 index.css
│   ├── 📁 lib
│   │   └── 💠 utils.ts
│   └── 📋 .eslintrc.json
├── 📁 src-tauri
│   ├── 📁 .claude-flow
│   │   └── 📁 metrics
│   │       ├── 📋 agent-metrics.json
│   │       ├── 📋 performance.json
│   │       └── 📋 task-metrics.json
│   ├── 📁 capabilities
│   │   ├── 📋 desktop.json
│   │   └── 📋 migrated.json
│   ├── 📁 gen
│   │   └── 📁 schemas
│   │       ├── 📋 acl-manifests.json
│   │       ├── 📋 capabilities.json
│   │       ├── 📋 desktop-schema.json
│   │       └── 📋 linux-schema.json
│   ├── 📁 icons
│   │   └── 📄 icon.icns
│   ├── 📁 src
│   │   ├── 📄 main.rs
│   │   └── 📄 rabbitmq.rs
│   ├── 📄 build.rs
│   ├── 📄 Cargo.lock
│   ├── 📄 Cargo.toml
│   └── 📋 tauri.conf.json
├── 📄 .mise.toml
├── 📝 AGENTS.md
├── 📄 bun.lock
├── 📋 components.json
├── 🌐 index.html
├── 📦 package.json
├── 📋 test-config.json
└── 💠 vite.config.ts
</project_structure>

<skills_system priority="1">

## Available Skills

<!-- SKILLS_TABLE_START -->
<usage>
When users ask you to perform tasks, check if any of the available skills below can help complete the task more effectively. Skills provide specialized capabilities and domain knowledge.

How to use skills:
- Invoke: `npx openskills read <skill-name>` (run in your shell)
  - For multiple: `npx openskills read skill-one,skill-two`
- The skill content will load with detailed instructions on how to complete the task
- Base directory provided in output for resolving bundled resources (references/, scripts/, assets/)

Usage notes:
- Only use skills listed in <available_skills> below
- Do not invoke a skill that is already loaded in your context
- Each skill invocation is stateless
</usage>

<available_skills>

<skill>
<name>33god-development-lifecycle</name>
<description>Meta-level orchestration for 33GOD platform development. Use when (1) coordinating cross-component features across multiple services, (2) assessing platform-wide status and component maturity, (3) managing hierarchical BMAD workflows (platform-level + component-level), (4) delegating tasks to component teams via Zellij, (5) generating platform architecture artifacts (integration maps, component inventories, system status), (6) planning strategic roadmap for multi-component ecosystem.</description>
<location>global</location>
</skill>

<skill>
<name>33god-imi-worktree-management</name>
<description>></description>
<location>global</location>
</skill>

<skill>
<name>33god-service-development</name>
<description>"Guide for creating, registering, and deploying new microservices in the 33GOD event-driven ecosystem. Use when (1) Creating new event consumer services, (2) Registering services in the Bloodbank registry, (3) Implementing FastStream-based event handlers, (4) Setting up service infrastructure (Docker, dependencies, testing), (5) Understanding 33GOD architecture and event patterns, (6) Migrating services to FastStream from legacy patterns."</description>
<location>global</location>
</skill>

<skill>
<name>33god-system-expert</name>
<description>Deep knowledge expert for the 33GOD agentic pipeline system, understands component relationships and suggests feature implementations based on actual codebase state</description>
<location>global</location>
</skill>

<skill>
<name>33god-workflow-generator</name>
<description>Generate complete workflow implementations from semantic descriptions for the 33GOD ecosystem. Use when (1) implementing new event-driven workflows, (2) creating services that span filesystem → API → Bloodbank → processing, (3) adding orchestration flows that combine Node-RED + Python services, (4) registering new services in the 33GOD registry, or (5) when the user provides a workflow description like "watch directory, process files, publish events."</description>
<location>global</location>
</skill>

<skill>
<name>architect</name>
<description>System architecture and technical design specialist</description>
<location>global</location>
</skill>

<skill>
<name>bmad-bmb-builder</name>
<description>Custom agent and workflow creation specialist</description>
<location>global</location>
</skill>

<skill>
<name>bmad-bmb-hardware-engineer</name>
<description>DIY hardware engineering guidance for software engineers building physical products. Use when (1) selecting microcontrollers, displays, audio components, or peripherals, (2) creating hardware BOMs with cost/lead-time analysis, (3) making 3D printing decisions (materials, finishes, enclosure design), (4) designing circuits or wiring diagrams, (5) planning hardware integration tests, (6) generating assembly instructions, (7) visualizing enclosure concepts before CAD. Triggers on phrases like "hardware tech spec", "component comparison", "BOM", "3D print", "enclosure", "circuit design", "wiring diagram", or when working on physical product development.</description>
<location>global</location>
</skill>

<skill>
<name>bmad-bmb-mise-architect</name>
<description>Task orchestration and build system architect specializing in mise configuration, cross-service task patterns, and hierarchical workflow composition for multi-service architectures</description>
<location>global</location>
</skill>

<skill>
<name>bmad-bmm-analyst</name>
<description>Product discovery and requirements analysis specialist</description>
<location>global</location>
</skill>

<skill>
<name>bmad-bmm-architect</name>
<description>System architecture and technical design specialist</description>
<location>global</location>
</skill>

<skill>
<name>bmad-bmm-developer</name>
<description>Story implementation and code development specialist</description>
<location>global</location>
</skill>

<skill>
<name>bmad-bmm-plane-captain</name>
<description>Intelligent ticket lifecycle manager with context awareness and BMAD integration</description>
<location>global</location>
</skill>

<skill>
<name>bmad-bmm-pm</name>
<description>Product requirements and planning specialist</description>
<location>global</location>
</skill>

<skill>
<name>bmad-bmm-scrum-master</name>
<description>Sprint planning and agile workflow specialist</description>
<location>global</location>
</skill>

<skill>
<name>bmad-bmm-ux-designer</name>
<description>User experience and interface design specialist</description>
<location>global</location>
</skill>

<skill>
<name>bmad-cis-creative-intelligence</name>
<description>Brainstorming and research automation specialist</description>
<location>global</location>
</skill>

<skill>
<name>bmad-core-master</name>
<description>Core BMAD Method orchestrator and workflow manager</description>
<location>global</location>
</skill>

<skill>
<name>bmad-master</name>
<description>Core BMAD Method orchestrator and workflow manager</description>
<location>global</location>
</skill>

<skill>
<name>builder</name>
<description>Custom agent and workflow creation specialist</description>
<location>global</location>
</skill>

<skill>
<name>chrome-extension-developer</name>
<description></description>
<location>global</location>
</skill>

<skill>
<name>claude-flow-hook-customizing</name>
<description>Use this skill when creating, optimizing, or maintaining claude hooks.</description>
<location>global</location>
</skill>

<skill>
<name>coding-conventions</name>
<description>version: 1.0.0</description>
<location>global</location>
</skill>

<skill>
<name>creating-workflows</name>
<description>Use this skill when creating automated multi-phase workflows for development tasks using claude-flow orchestration. Implements patterns for implementation kickoff, verification, retry loops, and failure recovery.</description>
<location>global</location>
</skill>

<skill>
<name>creative-intelligence</name>
<description>Brainstorming and research automation specialist</description>
<location>global</location>
</skill>

<skill>
<name>debugging-complex-multi-layer-systems</name>
<description>A reasoning pattern for diagnosing and fixing bugs that span multiple abstraction layers in complex systems.</description>
<location>global</location>
</skill>

<skill>
<name>developer</name>
<description>Story implementation and code development specialist</description>
<location>global</location>
</skill>

<skill>
<name>ecosystem-patterns</name>
<description>"Use this when creating new projects, generating documentation, cleaning/organizing a repo, suggesting architecture, deploying containers and services, naming files/folders, or when the user references 'ecosystem', 'patterns', or 'containers'. This skill outlines naming conventions, stack preferences, project organization (iMi worktrees), Docker patterns, and PRD structures from past conversations."</description>
<location>global</location>
</skill>

<skill>
<name>letta</name>
<description>Letta framework for building stateful AI agents with long-term memory. Use for AI agent development, memory management, tool integration, and multi-agent systems.</description>
<location>global</location>
</skill>

<skill>
<name>managing-tickets-and-tasks-in-plane</name>
<description>|</description>
<location>global</location>
</skill>

<skill>
<name>mise-architect</name>
<description>Task orchestration and build system architect specializing in mise configuration, cross-service task patterns, and hierarchical workflow composition for multi-service architectures</description>
<location>global</location>
</skill>

<skill>
<name>mise-task-managing</name>
<description>Expert guidance for mise task runners including TOML task configuration, file-based tasks, task orchestration with dependencies, sources/outputs caching, parallel execution, and workflow patterns. Use when working with mise.toml task definitions, mise-tasks/ directory, task dependencies, build caching, or mise task automation.</description>
<location>global</location>
</skill>

<skill>
<name>notebooklm</name>
<description>Use this skill to query your Google NotebookLM notebooks directly from Claude Code for source-grounded, citation-backed answers from Gemini. Browser automation, library management, persistent auth. Zero hallucinations, just your knowledge base.</description>
<location>global</location>
</skill>

<skill>
<name>pjangler-dev</name>
<description>|</description>
<location>global</location>
</skill>

<skill>
<name>plane-captain</name>
<description>Intelligent ticket lifecycle manager with context awareness and BMAD integration</description>
<location>global</location>
</skill>

<skill>
<name>pm</name>
<description>Product requirements and planning specialist</description>
<location>global</location>
</skill>

<skill>
<name>scrum-master</name>
<description>Sprint planning and agile workflow specialist</description>
<location>global</location>
</skill>

<skill>
<name>shadcn-components</name>
<description>Add or customize shadcn/ui components in the shared UI package. Use when adding new components from shadcn registry or updating existing component variants.</description>
<location>global</location>
</skill>

<skill>
<name>skill-creator</name>
<description>Guide for creating effective skills. This skill should be used when users want to create a new skill (or update an existing skill) that extends Claude's capabilities with specialized knowledge, workflows, or tool integrations.</description>
<location>global</location>
</skill>

<skill>
<name>tauri</name>
<description>Tauri framework for building cross-platform desktop and mobile apps. Use for desktop app development, native integrations, Rust backend, and web-based UIs.</description>
<location>global</location>
</skill>

<skill>
<name>using-reasoningbank-to-manage-memories</name>
<description></description>
<location>global</location>
</skill>

<skill>
<name>ux-designer</name>
<description>User experience and interface design specialist</description>
<location>global</location>
</skill>

<skill>
<name>zellij-driver</name>
<description>></description>
<location>global</location>
</skill>

<skill>
<name>zellij-plugin-dev</name>
<description>Develop Zellij plugins with Rust/WASM, API reference, event system, UI rendering, plugin lifecycle, and real-world examples from diverse open source plugins</description>
<location>global</location>
</skill>

</available_skills>
<!-- SKILLS_TABLE_END -->

</skills_system>
