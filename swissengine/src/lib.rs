use serde::{Deserialize, Serialize};
use std::collections::VecDeque;
use std::sync::{Arc, RwLock};
use std::sync::atomic::{AtomicUsize, Ordering};

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
pub enum ProbeTier {
    Passive,
    ProtocolPing,
    ActiveFuzz,
}

impl ProbeTier {
    pub fn multiplier(self) -> u8 {
        match self {
            ProbeTier::Passive => 1,
            ProbeTier::ProtocolPing => 2,
            ProbeTier::ActiveFuzz => 3,
        }
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
pub enum SessionTier {
    Observe,
    Probe,
    Flash,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SessionState {
    pub armed: bool,
    pub tier: SessionTier,
}

#[derive(Clone)]
pub struct SwissIOSession {
    state: Arc<RwLock<SessionState>>,
}

impl SwissIOSession {
    pub fn new() -> Self {
        Self {
            state: Arc::new(RwLock::new(SessionState {
                armed: false,
                tier: SessionTier::Observe,
            })),
        }
    }

    pub fn arm_on(&self, tier: SessionTier) {
        let mut state = self.state.write().expect("session lock poisoned");
        state.armed = true;
        state.tier = tier;
    }

    pub fn arm_off(&self) {
        let mut state = self.state.write().expect("session lock poisoned");
        state.armed = false;
        state.tier = SessionTier::Observe;
    }

    pub fn snapshot(&self) -> SessionState {
        self.state.read().expect("session lock poisoned").clone()
    }

    pub fn can_write(&self) -> bool {
        let state = self.state.read().expect("session lock poisoned");
        state.armed && state.tier == SessionTier::Flash
    }
}

impl Default for SwissIOSession {
    fn default() -> Self {
        Self::new()
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct EndpointInfo {
    pub address: u8,
    pub attributes: u8,
    pub max_packet_size: u16,
    pub direction_in: bool,
    pub acknowledged: bool,
    pub descriptor_declared: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct InterfaceInfo {
    pub number: u8,
    pub class_code: u8,
    pub subclass_code: u8,
    pub protocol_code: u8,
    pub endpoints: Vec<EndpointInfo>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct EndpointMap {
    pub device_id: String,
    pub vendor_id: u16,
    pub product_id: u16,
    pub tier: ProbeTier,
    pub interfaces: Vec<InterfaceInfo>,
    pub hidden_endpoints: Vec<u8>,
    pub risk_score: u8,
}

impl EndpointMap {
    pub fn calculate_risk_score(&mut self) {
        self.hidden_endpoints = self
            .interfaces
            .iter()
            .flat_map(|itf| itf.endpoints.iter())
            .filter(|ep| ep.acknowledged && !ep.descriptor_declared)
            .map(|ep| ep.address)
            .collect();

        // Requested model: Risk = Tier * (HiddenEndpoints + 1)
        let base = self.hidden_endpoints.len() as u8;
        let mut score = self.tier.multiplier().saturating_mul(base.saturating_add(1));
        if score > 100 {
            score = 100;
        }
        self.risk_score = score;
    }

    pub fn from_endpoint_sweep<P: EndpointProber>(
        device_id: String,
        vendor_id: u16,
        product_id: u16,
        tier: ProbeTier,
        mut prober: P,
    ) -> Self {
        let mut endpoints = Vec::new();

        for address in 0x01..=0x0F {
            let response = prober.probe(address);
            if response != ProbeResponse::Stall {
                endpoints.push(EndpointInfo {
                    address,
                    attributes: 0x02,
                    max_packet_size: 64,
                    direction_in: false,
                    acknowledged: response == ProbeResponse::Ack,
                    descriptor_declared: false,
                });
            }
        }

        for address in 0x81..=0x8F {
            let response = prober.probe(address);
            if response != ProbeResponse::Stall {
                endpoints.push(EndpointInfo {
                    address,
                    attributes: 0x02,
                    max_packet_size: 64,
                    direction_in: true,
                    acknowledged: response == ProbeResponse::Ack,
                    descriptor_declared: false,
                });
            }
        }

        let mut map = Self {
            device_id,
            vendor_id,
            product_id,
            tier,
            interfaces: vec![InterfaceInfo {
                number: 0,
                class_code: 0xFF,
                subclass_code: 0x01,
                protocol_code: 0x00,
                endpoints,
            }],
            hidden_endpoints: vec![],
            risk_score: 0,
        };
        map.calculate_risk_score();
        map
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProbeResponse {
    Stall,
    Ack,
    Nack,
    Timeout,
}

pub trait EndpointProber {
    fn probe(&mut self, endpoint_address: u8) -> ProbeResponse;
}

pub fn infer_interface_type(class_code: u8) -> &'static str {
    match class_code {
        0x03 => "HID",
        0x0A => "CDC_DATA",
        0xFE => "APP_SPECIFIC",
        0xFF => "VENDOR_SPECIFIC",
        _ => "UNKNOWN",
    }
}

pub fn gated_write(session: &SwissIOSession, data: &[u8]) -> Result<usize, &'static str> {
    if !session.can_write() {
        return Err("write blocked: session not armed for Flash tier");
    }
    Ok(data.len())
}

pub struct TraceRingBuffer {
    slots: Vec<AtomicUsize>,
    capacity: usize,
    write_idx: AtomicUsize,
    read_idx: AtomicUsize,
    staged: RwLock<VecDeque<u8>>,
}

impl TraceRingBuffer {
    pub fn new(capacity: usize) -> Self {
        let cap = capacity.max(2);
        Self {
            slots: (0..cap).map(|_| AtomicUsize::new(0)).collect(),
            capacity: cap,
            write_idx: AtomicUsize::new(0),
            read_idx: AtomicUsize::new(0),
            staged: RwLock::new(VecDeque::with_capacity(cap)),
        }
    }

    pub fn push_bytes(&self, bytes: &[u8]) {
        for &b in bytes {
            let idx = self.write_idx.fetch_add(1, Ordering::AcqRel) % self.capacity;
            self.slots[idx].store(b as usize, Ordering::Release);
            if self.write_idx.load(Ordering::Acquire)
                .saturating_sub(self.read_idx.load(Ordering::Acquire))
                > self.capacity
            {
                self.read_idx.fetch_add(1, Ordering::AcqRel);
            }
            let mut staged = self.staged.write().expect("trace lock poisoned");
            if staged.len() == self.capacity {
                staged.pop_front();
            }
            staged.push_back(b);
        }
    }

    // For a 60fps UI loop, call with max_frame_bytes periodically (~16ms).
    pub fn drain_frame(&self, max_frame_bytes: usize) -> Vec<u8> {
        let mut out = Vec::new();
        let mut staged = self.staged.write().expect("trace lock poisoned");
        let n = max_frame_bytes.min(staged.len());
        for _ in 0..n {
            if let Some(b) = staged.pop_front() {
                out.push(b);
                self.read_idx.fetch_add(1, Ordering::AcqRel);
            }
        }
        out
    }
}

pub fn estimate_entropy(window: &[u8]) -> f32 {
    if window.is_empty() {
        return 0.0;
    }

    let mut histogram = [0usize; 256];
    for &b in window {
        histogram[b as usize] += 1;
    }

    let total = window.len() as f32;
    let mut entropy = 0.0;
    for &count in &histogram {
        if count == 0 {
            continue;
        }
        let p = count as f32 / total;
        entropy -= p * p.log2();
    }
    entropy
}

#[cfg(test)]
mod tests {
    use super::*;

    struct MockProber {
        ack_set: Vec<u8>,
    }

    impl EndpointProber for MockProber {
        fn probe(&mut self, endpoint_address: u8) -> ProbeResponse {
            if self.ack_set.contains(&endpoint_address) {
                ProbeResponse::Ack
            } else {
                ProbeResponse::Stall
            }
        }
    }

    #[test]
    fn session_requires_flash_tier_for_write() {
        let session = SwissIOSession::new();
        assert!(!session.can_write());

        session.arm_on(SessionTier::Probe);
        assert!(!session.can_write());

        session.arm_on(SessionTier::Flash);
        assert!(session.can_write());
    }

    #[test]
    fn endpoint_sweep_collects_non_stall_responses() {
        let prober = MockProber {
            ack_set: vec![0x03, 0x87],
        };

        let map = EndpointMap::from_endpoint_sweep(
            "dev-1".to_string(),
            0x1234,
            0x5678,
            ProbeTier::ProtocolPing,
            prober,
        );

        let addresses: Vec<u8> = map.interfaces[0]
            .endpoints
            .iter()
            .map(|ep| ep.address)
            .collect();

        assert_eq!(addresses, vec![0x03, 0x87]);
        assert_eq!(map.hidden_endpoints, vec![0x03, 0x87]);
    }

    #[test]
    fn risk_formula_matches_tier_times_hidden_plus_one() {
        let mut map = EndpointMap {
            device_id: "test-1".to_string(),
            vendor_id: 0x1A86,
            product_id: 0x7523,
            tier: ProbeTier::ActiveFuzz,
            interfaces: vec![InterfaceInfo {
                number: 0,
                class_code: 0xFF,
                subclass_code: 1,
                protocol_code: 0,
                endpoints: vec![EndpointInfo {
                    address: 0x05,
                    attributes: 0x02,
                    max_packet_size: 64,
                    direction_in: false,
                    acknowledged: true,
                    descriptor_declared: false,
                }],
            }],
            hidden_endpoints: vec![],
            risk_score: 0,
        };

        map.calculate_risk_score();
        assert_eq!(map.risk_score, 6); // 3 * (1 + 1)
    }

    #[test]
    fn entropy_estimator_distinguishes_low_and_high_entropy() {
        let low = [0u8; 256];
        let high: Vec<u8> = (0u8..=255).collect();

        let low_h = estimate_entropy(&low);
        let high_h = estimate_entropy(&high);

        assert!(low_h < 0.1);
        assert!(high_h > 7.5);
    }

    #[test]
    fn trace_ring_buffer_drains_frames() {
        let rb = TraceRingBuffer::new(8);
        rb.push_bytes(&[1, 2, 3, 4, 5]);
        assert_eq!(rb.drain_frame(3), vec![1, 2, 3]);
        assert_eq!(rb.drain_frame(8), vec![4, 5]);
    }

    #[test]
    fn infer_interface_type_handles_known_classes() {
        assert_eq!(infer_interface_type(0x03), "HID");
        assert_eq!(infer_interface_type(0xFF), "VENDOR_SPECIFIC");
        assert_eq!(infer_interface_type(0x99), "UNKNOWN");
    }
}
