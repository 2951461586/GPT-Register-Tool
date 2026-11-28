//! Pure Rust port of `_derive_seeded_device_ids` from `sms_tool/registration/state.py`.

const NAMESPACE_URL: [u8; 16] = [
    0x6b, 0xa7, 0xb8, 0x11, 0x9d, 0xad, 0x11, 0xd1, 0x80, 0xb4, 0x00, 0xc0, 0x4f, 0xd4, 0x30, 0xc8,
];
const DEVICE_SEED_SALT: &str = "gpt-register-tool:registration:";
const HEX_LOWER: &[u8; 16] = b"0123456789abcdef";

fn sha1_digest(parts: &[&[u8]]) -> [u8; 20] {
    let mut h0: u32 = 0x6745_2301;
    let mut h1: u32 = 0xEFCD_AB89;
    let mut h2: u32 = 0x98BA_DCFE;
    let mut h3: u32 = 0x1032_5476;
    let mut h4: u32 = 0xC3D2_E1F0;

    let total_len: usize = parts.iter().map(|p| p.len()).sum();
    let mut buf = Vec::with_capacity(total_len + 72);
    for p in parts {
        buf.extend_from_slice(p);
    }
    buf.push(0x80);
    while (buf.len() % 64) != 56 {
        buf.push(0);
    }
    let bit_len = (total_len as u64) * 8;
    buf.extend_from_slice(&bit_len.to_be_bytes());

    let (chunks, _) = buf.as_chunks::<64>();
    for chunk in chunks {
        let mut w = [0_u32; 80];
        let (words, _) = chunk.as_chunks::<4>();
        for (slot, &block) in w[..16].iter_mut().zip(words) {
            *slot = u32::from_be_bytes(block);
        }
        for i in 16..80 {
            w[i] = (w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16]).rotate_left(1);
        }

        let (mut a, mut b, mut c, mut d, mut e) = (h0, h1, h2, h3, h4);
        for (i, &wi) in w.iter().enumerate() {
            let (f, k) = match i {
                0..=19 => ((b & c) | ((!b) & d), 0x5A82_7999_u32),
                20..=39 => (b ^ c ^ d, 0x6ED9_EBA1_u32),
                40..=59 => ((b & c) | (b & d) | (c & d), 0x8F1B_BCDC_u32),
                _ => (b ^ c ^ d, 0xCA62_C1D6_u32),
            };
            let temp = a
                .rotate_left(5)
                .wrapping_add(f)
                .wrapping_add(e)
                .wrapping_add(k)
                .wrapping_add(wi);
            e = d;
            d = c;
            c = b.rotate_left(30);
            b = a;
            a = temp;
        }
        h0 = h0.wrapping_add(a);
        h1 = h1.wrapping_add(b);
        h2 = h2.wrapping_add(c);
        h3 = h3.wrapping_add(d);
        h4 = h4.wrapping_add(e);
    }

    let mut out = [0_u8; 20];
    out[0..4].copy_from_slice(&h0.to_be_bytes());
    out[4..8].copy_from_slice(&h1.to_be_bytes());
    out[8..12].copy_from_slice(&h2.to_be_bytes());
    out[12..16].copy_from_slice(&h3.to_be_bytes());
    out[16..20].copy_from_slice(&h4.to_be_bytes());
    out
}

fn uuid5_bytes(prefix: &[u8], key: &[u8]) -> [u8; 16] {
    let digest = sha1_digest(&[&NAMESPACE_URL, prefix, DEVICE_SEED_SALT.as_bytes(), key]);
    let mut bytes = [0_u8; 16];
    bytes.copy_from_slice(&digest[..16]);
    bytes[6] = (bytes[6] & 0x0f) | 0x50;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    bytes
}

fn format_uuid_hyphenated(bytes: &[u8; 16]) -> String {
    let mut out = Vec::with_capacity(36);
    for (idx, &b) in bytes.iter().enumerate() {
        if matches!(idx, 4 | 6 | 8 | 10) {
            out.push(b'-');
        }
        out.push(HEX_LOWER[(b >> 4) as usize]);
        out.push(HEX_LOWER[(b & 0x0f) as usize]);
    }
    String::from_utf8(out).unwrap_or_default()
}

fn format_uuid_hex(bytes: &[u8; 16]) -> String {
    let mut out = Vec::with_capacity(32);
    for &b in bytes {
        out.push(HEX_LOWER[(b >> 4) as usize]);
        out.push(HEX_LOWER[(b & 0x0f) as usize]);
    }
    String::from_utf8(out).unwrap_or_default()
}

pub fn _derive_seeded_device_ids(key: String) -> (String, String) {
    if key.is_empty() {
        return (String::new(), String::new());
    }
    let key_bytes = key.as_bytes();
    let device_bytes = uuid5_bytes(b"device_id:", key_bytes);
    let logging_bytes = uuid5_bytes(b"auth_session_logging_id:", key_bytes);
    (
        format_uuid_hyphenated(&device_bytes),
        format_uuid_hex(&logging_bytes),
    )
}
