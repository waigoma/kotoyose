use std::{
    cell::RefCell,
    collections::{HashMap, VecDeque},
    rc::Rc,
};

use tokio::sync::Notify;

use crate::frame::{Frame, Operation};

const STREAM_FRAMES: usize = 4096;
const DATA_BYTES: usize = 8 * 1024 * 1024;
const PRIORITY_BYTES: usize = 2 * 1024 * 1024;
const PRIORITY_FRAMES: usize = 128;

#[derive(Default)]
struct Queues {
    lanes: HashMap<u32, VecDeque<Frame>>,
    rotation: VecDeque<u32>,
    bytes: usize,
    priority: VecDeque<Frame>,
    priority_bytes: usize,
    data_enabled: bool,
}

#[derive(Clone, Default)]
pub struct Outbox(Rc<(RefCell<Queues>, Notify)>);

impl Outbox {
    pub fn enable_data(&self, enabled: bool) {
        self.0.0.borrow_mut().data_enabled = enabled;
        self.0.1.notify_one();
    }

    pub fn push(&self, frame: Frame) -> bool {
        let mut queues = self.0.0.borrow_mut();
        let size = frame.body.len();
        let id = frame.envelope.stream;
        if queues.bytes + size > DATA_BYTES
            || queues
                .lanes
                .get(&id)
                .is_some_and(|lane| lane.len() >= STREAM_FRAMES)
        {
            return false;
        }
        if !queues.lanes.contains_key(&id) {
            queues.rotation.push_back(id);
        }
        queues.lanes.entry(id).or_default().push_back(frame);
        queues.bytes += size;
        self.0.1.notify_one();
        true
    }

    pub fn priority(&self, frame: Frame) -> bool {
        let mut queues = self.0.0.borrow_mut();
        let size = frame.body.len();
        if queues.priority.len() >= PRIORITY_FRAMES || queues.priority_bytes + size > PRIORITY_BYTES
        {
            return false;
        }
        queues.priority.push_back(frame);
        queues.priority_bytes += size;
        self.0.1.notify_one();
        true
    }

    pub fn clear_priority(&self) {
        let mut queues = self.0.0.borrow_mut();
        queues.priority.clear();
        queues.priority_bytes = 0;
    }

    pub fn start_pending(&self, id: u32) -> bool {
        self.0
            .0
            .borrow()
            .lanes
            .get(&id)
            .and_then(|lane| lane.front())
            .is_some_and(|frame| Operation::start(&frame.envelope.kind).is_some())
    }

    /// Returns whether the start was removed before the writer began sending it.
    pub fn discard(&self, id: u32) -> bool {
        let mut queues = self.0.0.borrow_mut();
        let Some(lane) = queues.lanes.remove(&id) else {
            return false;
        };
        queues.bytes -= lane.iter().map(|frame| frame.body.len()).sum::<usize>();
        queues.rotation.retain(|stream| *stream != id);
        lane.front()
            .is_some_and(|frame| Operation::start(&frame.envelope.kind).is_some())
    }

    fn pop(&self) -> Option<Frame> {
        let mut queues = self.0.0.borrow_mut();
        if let Some(frame) = queues.priority.pop_front() {
            queues.priority_bytes -= frame.body.len();
            return Some(frame);
        }
        if !queues.data_enabled {
            return None;
        }
        let id = queues.rotation.pop_front()?;
        let lane = queues.lanes.get_mut(&id).expect("rotating data lane");
        let frame = lane.pop_front().expect("nonempty data lane");
        if lane.is_empty() {
            queues.lanes.remove(&id);
        } else {
            queues.rotation.push_back(id);
        }
        queues.bytes -= frame.body.len();
        Some(frame)
    }

    pub fn is_empty(&self) -> bool {
        let queues = self.0.0.borrow();
        queues.priority.is_empty() && queues.lanes.is_empty()
    }

    pub async fn next(&self) -> Frame {
        loop {
            if let Some(frame) = self.pop() {
                return frame;
            }
            self.0.1.notified().await;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn priority_rotation_and_discard() {
        let out = Outbox::default();
        assert!(out.push(Frame::new(1, "turn", json!({}))));
        assert!(out.push(Frame::new(1, "delta", json!({}))));
        assert!(out.push(Frame::new(2, "turn", json!({}))));
        assert!(out.priority(Frame::new(0, "heartbeat", json!({}))));
        assert_eq!(out.pop().unwrap().envelope.stream, 0);
        assert!(out.pop().is_none());
        out.enable_data(true);
        assert_eq!(out.pop().unwrap().envelope.kind, "turn");
        assert!(out.discard(2));
        assert_eq!(out.pop().unwrap().envelope.kind, "delta");
        assert!(out.pop().is_none());
        assert_eq!(out.0.0.borrow().bytes, 0);
    }

    #[test]
    fn overflow_keeps_priority_and_other_lanes_available() {
        let out = Outbox::default();
        for _ in 0..STREAM_FRAMES {
            assert!(out.push(Frame::new(1, "delta", json!({}))));
        }
        assert!(!out.push(Frame::new(1, "delta", json!({}))));
        out.discard(1);
        let data = json!({"text":"x".repeat(crate::frame::MAX_RECORD - 80)});
        for id in 2..=9 {
            assert!(out.push(Frame::new(id, "delta", data.clone())));
        }
        assert!(!out.push(Frame::new(10, "delta", data)));
        assert!(out.priority(Frame::new(1, "error", json!({}))));
        assert_eq!(out.pop().unwrap().envelope.kind, "error");
        out.discard(2);
        assert!(out.push(Frame::new(10, "delta", json!({}))));
    }
}
