# Candybar — Agent Guide

Cross-platform desktop app for real-time Bloodbank event observability.

## Tech Stack

### Frontend (Vite + React)

- **Framework:** React 19 + Vite 6
- **Language:** TypeScript 5.9
- **Styling:** Tailwind CSS 4, tailwindcss-animate, class-variance-authority
- **Animations:** Framer Motion
- **Charts:** Recharts
- **UI Components:** Radix UI (scroll-area, select, separator, switch, slot)

### Backend (Tauri 2)

- **Framework:** Tauri 2.9
- **Language:** Rust (edition 2021, rust-version 1.70)
- **Plugins:** dialog, fs, shell, clipboard, global-shortcut, http, notification, os, process
- **Messaging:** See ../bloodbank/README.md

## Commands

| Task            | Command                      |
| --------------- | ---------------------------- |
| Dev (Tauri)     | `npm run dev` or `tauri dev` |
| Dev (Vite only) | `npm run vite:dev`           |
| Build           | `npm run build` (Vite build) |
| Lint (TS)       | `npm run lint` (eslint)      |
| Lint (Rust)     | `npm run clippy`             |
| Stream Events   | `npm run stream`             |

## Key Directories

- `src/` — React frontend (components, hooks, pages, types)
- `src-tauri/` — Rust backend (Tauri commands, RabbitMQ integration)
- `src/hooks/useRabbitMQ.ts` — Real-time event subscription hook
- `src/types/bloodbank.ts` — Event type definitions

## Conventions

- Radix UI for accessible primitives
- Stitch for Design Iteration and Ideation (stitch-loop)
- Real-time events via WebSocket (socket.io-client) to Bloodbank WS relay
- Rust backend handles native OS integration and secure connections
