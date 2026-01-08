use futures_lite::stream::StreamExt;
use lapin::{
    options::*, types::FieldTable, Channel, Connection, ConnectionProperties,
    Consumer,
};
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use tauri::{AppHandle, Emitter};
use tokio::sync::Mutex;

/// RabbitMQ connection configuration
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RabbitConfig {
    pub host: String,
    pub port: u16,
    pub username: String,
    pub password: String,
    pub exchange: String,
    pub routing_keys: Vec<String>,
}

impl Default for RabbitConfig {
    fn default() -> Self {
        Self {
            host: "localhost".to_string(),
            port: 5672,
            username: "guest".to_string(),
            password: "guest".to_string(),
            exchange: "bloodbank.events".to_string(),
            routing_keys: vec!["#".to_string()], // Subscribe to all
        }
    }
}

/// Bloodbank event envelope (matches Python EventEnvelope)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BloodbankEvent {
    pub event_id: String,
    pub event_type: String,
    pub timestamp: String,
    pub source: EventSource,
    #[serde(default)]
    pub correlation_ids: Vec<String>,
    #[serde(default)]
    pub agent_context: Option<AgentContext>,
    pub payload: serde_json::Value,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EventSource {
    pub host: String,
    pub app: String,
    #[serde(rename = "type")]
    pub source_type: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentContext {
    pub agent_name: Option<String>,
    pub session_id: Option<String>,
    pub project: Option<String>,
}

/// Connection state
pub struct RabbitState {
    connection: Option<Connection>,
    channel: Option<Channel>,
    consumer_task: Option<tokio::task::JoinHandle<()>>,
}

impl RabbitState {
    pub fn new() -> Self {
        Self {
            connection: None,
            channel: None,
            consumer_task: None,
        }
    }
}

/// Connect to RabbitMQ and start consuming events
pub async fn connect_and_subscribe(
    config: RabbitConfig,
    app: AppHandle,
    state: Arc<Mutex<RabbitState>>,
) -> Result<(), String> {
    let addr = format!(
        "amqp://{}:{}@{}:{}/%2f",
        config.username, config.password, config.host, config.port
    );

    println!("Connecting to RabbitMQ at {}:{}...", config.host, config.port);

    // Connect to RabbitMQ
    let conn = Connection::connect(&addr, ConnectionProperties::default())
        .await
        .map_err(|e| format!("Failed to connect to RabbitMQ: {}", e))?;

    println!("Connected to RabbitMQ!");

    // Create channel
    let channel = conn
        .create_channel()
        .await
        .map_err(|e| format!("Failed to create channel: {}", e))?;

    // Declare exchange (topic type for routing key patterns)
    channel
        .exchange_declare(
            &config.exchange,
            lapin::ExchangeKind::Topic,
            ExchangeDeclareOptions {
                passive: false,
                durable: true,
                auto_delete: false,
                internal: false,
                nowait: false,
            },
            FieldTable::default(),
        )
        .await
        .map_err(|e| format!("Failed to declare exchange: {}", e))?;

    // Create exclusive queue for this consumer
    let queue_name = format!("candybar.{}", uuid::Uuid::new_v4());
    let queue = channel
        .queue_declare(
            &queue_name,
            QueueDeclareOptions {
                passive: false,
                durable: false,
                exclusive: true,
                auto_delete: true,
                nowait: false,
            },
            FieldTable::default(),
        )
        .await
        .map_err(|e| format!("Failed to declare queue: {}", e))?;

    println!("Created queue: {}", queue.name());

    // Bind queue to exchange with routing keys
    for routing_key in &config.routing_keys {
        channel
            .queue_bind(
                queue.name().as_str(),
                &config.exchange,
                routing_key,
                QueueBindOptions::default(),
                FieldTable::default(),
            )
            .await
            .map_err(|e| format!("Failed to bind queue: {}", e))?;
        println!("Bound to routing key: {}", routing_key);
    }

    // Start consuming
    let consumer = channel
        .basic_consume(
            queue.name().as_str(),
            "candybar-consumer",
            BasicConsumeOptions::default(),
            FieldTable::default(),
        )
        .await
        .map_err(|e| format!("Failed to start consumer: {}", e))?;

    // Spawn consumer task
    let consumer_task = tokio::spawn(consume_events(consumer, app.clone()));

    // Store state
    let mut state_guard = state.lock().await;
    state_guard.connection = Some(conn);
    state_guard.channel = Some(channel);
    state_guard.consumer_task = Some(consumer_task);

    // Emit connection status
    let _ = app.emit("rabbitmq:connected", &config);

    Ok(())
}

/// Consume events from RabbitMQ and emit to frontend
async fn consume_events(mut consumer: Consumer, app: AppHandle) {
    println!("Starting event consumer...");

    while let Some(delivery) = consumer.next().await {
        match delivery {
            Ok(delivery) => {
                // Parse the message
                let body = String::from_utf8_lossy(&delivery.data);

                match serde_json::from_str::<BloodbankEvent>(&body) {
                    Ok(event) => {
                        println!(
                            "Received event: {} from {}",
                            event.event_type, event.source.app
                        );

                        // Emit to frontend
                        if let Err(e) = app.emit("bloodbank:event", &event) {
                            eprintln!("Failed to emit event: {}", e);
                        }
                    }
                    Err(e) => {
                        eprintln!("Failed to parse event: {} - Body: {}", e, body);
                    }
                }

                // Acknowledge the message
                if let Err(e) = delivery.ack(BasicAckOptions::default()).await {
                    eprintln!("Failed to ack message: {}", e);
                }
            }
            Err(e) => {
                eprintln!("Consumer error: {}", e);
                break;
            }
        }
    }

    println!("Consumer stopped");
    let _ = app.emit("rabbitmq:disconnected", ());
}

/// Disconnect from RabbitMQ
pub async fn disconnect(state: Arc<Mutex<RabbitState>>) -> Result<(), String> {
    let mut state_guard = state.lock().await;

    // Abort consumer task
    if let Some(task) = state_guard.consumer_task.take() {
        task.abort();
    }

    // Close channel
    if let Some(channel) = state_guard.channel.take() {
        let _ = channel.close(200, "Closing").await;
    }

    // Close connection
    if let Some(conn) = state_guard.connection.take() {
        let _ = conn.close(200, "Closing").await;
    }

    Ok(())
}

// ============================================================================
// Tauri Commands
// ============================================================================

#[tauri::command]
pub async fn rabbitmq_connect(
    config: RabbitConfig,
    app: AppHandle,
    state: tauri::State<'_, Arc<Mutex<RabbitState>>>,
) -> Result<(), String> {
    connect_and_subscribe(config, app, state.inner().clone()).await
}

#[tauri::command]
pub async fn rabbitmq_disconnect(
    state: tauri::State<'_, Arc<Mutex<RabbitState>>>,
) -> Result<(), String> {
    disconnect(state.inner().clone()).await
}

#[tauri::command]
pub fn rabbitmq_default_config() -> RabbitConfig {
    RabbitConfig::default()
}
