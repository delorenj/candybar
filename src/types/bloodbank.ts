/**
 * Bloodbank Event Types - Auto-generated from Bloodbank domain definitions
 * Source: /home/delorenj/code/bloodbank/trunk-main/event_producers/events/domains/
 */

// ============================================================================
// Domain Registry - All known top-level domains
// ============================================================================

export const BLOODBANK_DOMAINS = {
  agent: {
    label: 'Agent',
    description: 'Agent thread interactions and orchestration',
    color: '#3B82F6', // blue
  },
  artifact: {
    label: 'Artifact',
    description: 'File and artifact management',
    color: '#10B981', // green
  },
  fireflies: {
    label: 'Fireflies',
    description: 'Meeting transcription and processing',
    color: '#F59E0B', // amber
  },
  github: {
    label: 'GitHub',
    description: 'GitHub integration events',
    color: '#6366F1', // indigo
  },
  llm: {
    label: 'LLM',
    description: 'LLM provider interactions',
    color: '#8B5CF6', // violet
  },
  theboard: {
    label: 'TheBoard',
    description: 'Multi-agent meeting orchestration',
    color: '#EC4899', // pink
  },
  workflow: {
    label: 'Workflow',
    description: 'n8n workflow events',
    color: '#14B8A6', // teal
  },
} as const;

export type DomainKey = keyof typeof BLOODBANK_DOMAINS;

// ============================================================================
// Event Type Registry - All known routing keys by domain
// ============================================================================

export const EVENT_TYPES: Record<DomainKey, string[]> = {
  agent: [
    'agent.thread.prompt',
    'agent.thread.response',
    'agent.thread.error',
  ],
  artifact: [
    'artifact.file.created',
    'artifact.file.updated',
    'artifact.file.deleted',
  ],
  fireflies: [
    'fireflies.transcript.upload',
    'fireflies.transcript.ready',
    'fireflies.transcript.processed',
    'fireflies.transcript.failed',
  ],
  github: [
    'github.pr.created',
    'github.pr.merged',
    'github.pr.closed',
    'github.commit.pushed',
  ],
  llm: [
    'llm.prompt',
    'llm.response',
    'llm.error',
  ],
  theboard: [
    'theboard.meeting.created',
    'theboard.meeting.started',
    'theboard.meeting.round_completed',
    'theboard.meeting.comment_extracted',
    'theboard.meeting.converged',
    'theboard.meeting.completed',
    'theboard.meeting.failed',
    'theboard.meeting.participant.added',
    'theboard.meeting.participant.turn.completed',
  ],
  workflow: [
    'workflow.step.started',
    'workflow.step.completed',
    'workflow.step.failed',
  ],
};

// ============================================================================
// Event Envelope - Standard Bloodbank event wrapper
// ============================================================================

export interface EventSource {
  host: string;
  app: string;
  type: 'webhook' | 'api' | 'internal' | 'agent';
}

export interface AgentContext {
  agent_name?: string;
  session_id?: string;
  project?: string;
}

export interface BloodbankEvent<T = unknown> {
  event_id: string;
  event_type: string;
  timestamp: string;
  source: EventSource;
  correlation_ids: string[];
  agent_context?: AgentContext;
  payload: T;
}

// ============================================================================
// Connection Configuration
// ============================================================================

export interface RabbitMQConfig {
  host: string;
  port: number;
  username: string;
  password: string;
  exchange: string;
  queue?: string;
  routingKeys: string[];
}

export const DEFAULT_RABBITMQ_CONFIG: RabbitMQConfig = {
  host: 'localhost',
  port: 5672,
  username: 'guest',
  password: 'guest',
  exchange: 'bloodbank.events.v1',
  routingKeys: ['#'], // Subscribe to all events
};

// ============================================================================
// Helpers
// ============================================================================

/**
 * Extract domain from event type (e.g., "fireflies.transcript.ready" -> "fireflies")
 */
export function getDomainFromEventType(eventType: string): DomainKey | 'unknown' {
  const domain = eventType.split('.')[0];
  if (domain in BLOODBANK_DOMAINS) {
    return domain as DomainKey;
  }
  return 'unknown';
}

/**
 * Get sub-event from event type (e.g., "fireflies.transcript.ready" -> "transcript.ready")
 */
export function getSubEventFromEventType(eventType: string): string {
  return eventType.split('.').slice(1).join('.');
}

/**
 * Get domain color for visualization
 */
export function getDomainColor(domain: DomainKey | 'unknown'): string {
  if (domain === 'unknown') return '#6B7280'; // gray
  return BLOODBANK_DOMAINS[domain].color;
}
