use anyhow::{Result, ensure};
use std::{ops::Range, time::Duration};

use serde::{Deserialize, Deserializer};
use serde_json::{Value, json, value::RawValue};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};

pub const MAX_RECORD: usize = 1024 * 1024;
pub const MAX_SKILL_TURN_RECORD: usize = 2 * MAX_RECORD;
pub const MAX_STREAM: u32 = 1_000_000_000;
pub const IPC: u64 = 1;

pub fn write_timeout(bytes: usize) -> Duration {
    Duration::from_secs(5) + Duration::from_secs_f64(bytes as f64 / 50_000.0)
}

#[derive(Debug)]
pub struct Envelope {
    pub stream: u32,
    pub kind: String,
}

#[derive(Deserialize)]
struct Incoming<'a> {
    stream: u32,
    kind: String,
    #[serde(borrow, default, deserialize_with = "raw_payload")]
    payload: Option<&'a RawValue>,
}

fn raw_payload<'de, D: Deserializer<'de>>(
    deserializer: D,
) -> Result<Option<&'de RawValue>, D::Error> {
    // Deserialize explicit JSON null as a raw value and reject it like any non-object.
    <&RawValue>::deserialize(deserializer).map(Some)
}

#[derive(Debug)]
pub struct Frame {
    pub envelope: Envelope,
    pub body: String,
    payload_range: Option<Range<usize>>,
}

impl Frame {
    pub fn parse(body: String) -> Result<Self> {
        ensure!(
            (1..=MAX_SKILL_TURN_RECORD).contains(&body.len()),
            "invalid frame length"
        );
        let incoming: Incoming<'_> = serde_json::from_str(&body)?;
        ensure!(
            incoming.stream <= MAX_STREAM && !incoming.kind.is_empty(),
            "invalid envelope"
        );
        if body.len() > MAX_RECORD {
            ensure!(
                incoming.kind == "turn"
                    && incoming.payload.is_some_and(|raw| {
                        serde_json::from_str::<Value>(raw.get()).is_ok_and(|payload| {
                            payload.get("skill_folders").is_some_and(Value::is_array)
                        })
                    }),
                "invalid frame length"
            );
        }
        let payload_range = if let Some(payload) = incoming.payload {
            let raw = payload.get();
            ensure!(raw.starts_with('{'), "invalid frame payload");
            let start = raw.as_ptr() as usize - body.as_ptr() as usize;
            Some(start..start + raw.len())
        } else {
            None
        };
        let envelope = Envelope {
            stream: incoming.stream,
            kind: incoming.kind,
        };
        Ok(Self {
            envelope,
            body,
            payload_range,
        })
    }

    pub fn payload(&self) -> Result<Value> {
        self.payload_as()
    }

    pub fn payload_as<T: serde::de::DeserializeOwned>(&self) -> Result<T> {
        match &self.payload_range {
            Some(range) => Ok(serde_json::from_str(&self.body[range.clone()])?),
            None => Ok(serde_json::from_str("{}")?),
        }
    }

    pub fn new(stream: u32, kind: &str, payload: Value) -> Self {
        Self::try_new(stream, kind, payload).expect("locally constructed frame")
    }

    pub fn try_new(stream: u32, kind: &str, payload: Value) -> Result<Self> {
        Self::parse(json!({"stream":stream, "kind":kind, "payload":payload}).to_string())
    }
}

pub async fn read_record(input: &mut (impl AsyncRead + Unpin)) -> Result<Option<Frame>> {
    let mut prefix = [0; 4];
    if input.read(&mut prefix[..1]).await? == 0 {
        return Ok(None);
    }
    input.read_exact(&mut prefix[1..]).await?;
    let length = u32::from_be_bytes(prefix) as usize;
    ensure!(
        (1..=MAX_RECORD).contains(&length),
        "invalid runner record length"
    );
    let mut raw = vec![0; length];
    input.read_exact(&mut raw).await?;
    Ok(Some(Frame::parse(String::from_utf8(raw)?)?))
}

pub async fn write_record(output: &mut (impl AsyncWrite + Unpin), body: &str) -> Result<()> {
    ensure!(
        (1..=MAX_SKILL_TURN_RECORD).contains(&body.len()),
        "invalid runner record length"
    );
    output.write_all(&(body.len() as u32).to_be_bytes()).await?;
    output.write_all(body.as_bytes()).await?;
    output.flush().await?;
    Ok(())
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Operation {
    Turn,
    Login,
    Status,
    Quota,
    SessionCollect,
}

impl Operation {
    pub fn start(kind: &str) -> Option<Self> {
        match kind {
            "turn" => Some(Self::Turn),
            "login" => Some(Self::Login),
            "status" => Some(Self::Status),
            "quota" => Some(Self::Quota),
            "session_collect" => Some(Self::SessionCollect),
            _ => None,
        }
    }

    pub fn cancel(self) -> &'static str {
        match self {
            Self::Turn => "cancel",
            Self::Login => "login_cancel",
            Self::Status => "status_cancel",
            Self::Quota => "quota_cancel",
            Self::SessionCollect => "session_collect_cancel",
        }
    }

    pub fn output(self, kind: &str) -> Option<bool> {
        match (self, kind) {
            (Self::Turn, "delta" | "event" | "mcp_request")
            | (Self::Login, "login_output")
            | (Self::SessionCollect, "session_collect_batch") => Some(false),
            (Self::Turn, "result" | "error")
            | (Self::Login, "login_exit" | "login_error")
            | (Self::Status, "status_result" | "status_error")
            | (Self::Quota, "quota_result" | "quota_error")
            | (Self::SessionCollect, "session_collect_done" | "session_collect_error") => {
                Some(true)
            }
            _ => None,
        }
    }

    pub fn error(self, stream: u32, message: &str, partial: &str) -> Frame {
        match self {
            Self::Turn => Frame::new(
                stream,
                "error",
                json!({
                    "message":message, "retryable":false, "diagnostics":{},
                    "partial_result":{"text":partial}
                }),
            ),
            Self::Login => Frame::new(stream, "login_error", json!({"message":message})),
            Self::Status => Frame::new(stream, "status_error", json!({"message":message})),
            Self::Quota => Frame::new(stream, "quota_error", json!({"message":message})),
            Self::SessionCollect => {
                Frame::new(stream, "session_collect_error", json!({"error":message}))
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use base64::{Engine, engine::general_purpose::STANDARD};
    use serde::Deserialize;
    use tokio::io::AsyncWriteExt;

    use super::*;

    #[test]
    fn write_budget_adds_time_for_large_frames() {
        assert_eq!(write_timeout(0), Duration::from_secs(5));
        assert_eq!(write_timeout(50_000), Duration::from_secs(6));
        assert!(write_timeout(MAX_RECORD) > Duration::from_secs(25));
    }

    #[tokio::test]
    async fn only_folder_turns_can_exceed_the_legacy_record_limit() {
        let payload = json!({"skill_folders": [{"name": "demo", "files": {"asset": "x".repeat(MAX_RECORD)}}]});
        let frame = Frame::try_new(1, "turn", payload.clone()).unwrap();
        assert!(frame.body.len() > MAX_RECORD);
        assert!(Frame::try_new(1, "delta", payload).is_err());
        assert!(Frame::try_new(1, "turn", json!({"text": "x".repeat(MAX_RECORD)})).is_err());
        assert!(
            Frame::try_new(
                1,
                "turn",
                json!({"skill_folders": ["x".repeat(MAX_SKILL_TURN_RECORD)]})
            )
            .is_err()
        );

        let mut record = Vec::new();
        write_record(&mut record, &frame.body).await.unwrap();
        assert_eq!(&record[4..], frame.body.as_bytes());
    }

    #[derive(Deserialize)]
    struct GoldenFrame {
        name: String,
        raw: String,
        rust: bool,
        repeat: usize,
    }

    #[test]
    fn shared_frames_validate_envelope_and_preserve_json() {
        let cases: Vec<GoldenFrame> = serde_json::from_str(include_str!(
            "../../tests/fixtures/child_frames/frames.json"
        ))
        .unwrap();
        for case in cases {
            let raw = case.raw.replace("~repeat~", &"x".repeat(case.repeat));
            match Frame::parse(raw.clone()) {
                Ok(frame) => {
                    assert!(case.rust, "{}", case.name);
                    assert_eq!(frame.body, raw);
                    let value: Value = serde_json::from_str(&raw).unwrap();
                    assert_eq!(
                        frame.payload().unwrap(),
                        value.get("payload").cloned().unwrap_or(json!({}))
                    );
                }
                Err(_) => assert!(!case.rust, "{}", case.name),
            }
        }
    }

    #[derive(Deserialize)]
    struct GoldenRecord {
        name: String,
        bytes: String,
        outcome: String,
        spaces: usize,
    }

    #[tokio::test]
    async fn shared_records_cover_fragmentation_eof_and_size_limits() {
        let cases: Vec<GoldenRecord> = serde_json::from_str(include_str!(
            "../../tests/fixtures/child_frames/records.json"
        ))
        .unwrap();
        for case in cases {
            let mut bytes = STANDARD.decode(case.bytes).unwrap();
            bytes.extend(std::iter::repeat_n(b' ', case.spaces));
            let (mut write, mut read) = tokio::io::duplex(4096);
            let written = async {
                // Split the length header and the UTF-8 payload across writes.
                let stride = if bytes.len() < 100 { 1 } else { 4093 };
                for chunk in bytes.chunks(stride) {
                    if write.write_all(chunk).await.is_err() {
                        break;
                    }
                    tokio::task::yield_now().await;
                }
                drop(write);
            };
            let read_frame = async {
                let result = read_record(&mut read).await;
                drop(read);
                result
            };
            let (_, result) = tokio::join!(written, read_frame);
            match case.outcome.as_str() {
                "frame" => {
                    let frame = result.unwrap().unwrap();
                    let mut output = Vec::new();
                    write_record(&mut output, &frame.body).await.unwrap();
                    assert_eq!(output, bytes, "{}", case.name);
                }
                "eof" => assert!(result.unwrap().is_none(), "{}", case.name),
                _ => assert!(result.is_err(), "{}", case.name),
            }
        }
    }
}
