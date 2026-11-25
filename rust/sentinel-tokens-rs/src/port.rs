//! Rust port of `_solve_pow` in `sms_tool/registration/sentinel_tokens.py`.
//!
//! Pure-Rust FIPS-202 SHA3-512 proof-of-work loop (up to 500,000 iterations)
//! with zero heap allocation per attempt and zero `unsafe` code.

#![forbid(unsafe_code)]

const MAX_ATTEMPTS: u64 = 500_000;
const SHA3_512_RATE: usize = 72;

const RC: [u64; 24] = [
    0x0000_0000_0000_0001,
    0x0000_0000_0000_8082,
    0x8000_0000_0000_808A,
    0x8000_0000_8000_8000,
    0x0000_0000_0000_808B,
    0x0000_0000_8000_0001,
    0x8000_0000_8000_8081,
    0x8000_0000_0000_8009,
    0x0000_0000_0000_008A,
    0x0000_0000_0000_0088,
    0x0000_0000_8000_8009,
    0x0000_0000_8000_000A,
    0x0000_0000_8000_808B,
    0x8000_0000_0000_008B,
    0x8000_0000_0000_8089,
    0x8000_0000_0000_8003,
    0x8000_0000_0000_8002,
    0x8000_0000_0000_0080,
    0x0000_0000_0000_800A,
    0x8000_0000_8000_000A,
    0x8000_0000_8000_8081,
    0x8000_0000_0000_8080,
    0x0000_0000_8000_0001,
    0x8000_0000_8000_8008,
];

const RHO: [u32; 24] = [
    1, 3, 6, 10, 15, 21, 28, 36, 45, 55, 2, 14, 27, 41, 56, 8, 25, 43, 62, 18, 39, 61, 20, 44,
];

const PI: [usize; 24] = [
    10, 7, 11, 17, 18, 3, 5, 16, 8, 21, 24, 4, 15, 23, 19, 13, 12, 2, 20, 14, 22, 9, 6, 1,
];

#[inline]
fn keccak_f1600(st: &mut [u64; 25]) {
    for &rc in &RC {
        let mut bc = [0u64; 5];
        for (i, slot) in bc.iter_mut().enumerate() {
            *slot = st[i] ^ st[i + 5] ^ st[i + 10] ^ st[i + 15] ^ st[i + 20];
        }
        for i in 0..5 {
            let t = bc[(i + 4) % 5] ^ bc[(i + 1) % 5].rotate_left(1);
            for j in (0..25).step_by(5) {
                st[j + i] ^= t;
            }
        }
        let mut t = st[1];
        for (&j, &rho) in PI.iter().zip(RHO.iter()) {
            let tmp = st[j];
            st[j] = t.rotate_left(rho);
            t = tmp;
        }
        for j in (0..25).step_by(5) {
            let row = [st[j], st[j + 1], st[j + 2], st[j + 3], st[j + 4]];
            for i in 0..5 {
                st[j + i] ^= (!row[(i + 1) % 5]) & row[(i + 2) % 5];
            }
        }
        st[0] ^= rc;
    }
}

#[derive(Clone)]
struct Sha3_512State {
    st: [u64; 25],
    buf: [u8; SHA3_512_RATE],
    pos: usize,
}

impl Sha3_512State {
    fn new() -> Self {
        Self {
            st: [0u64; 25],
            buf: [0u8; SHA3_512_RATE],
            pos: 0,
        }
    }

    #[inline]
    fn absorb_block(&mut self, block: &[u8; SHA3_512_RATE]) {
        for (i, chunk) in block.as_chunks::<8>().0.iter().enumerate() {
            self.st[i] ^= u64::from_le_bytes(*chunk);
        }
        keccak_f1600(&mut self.st);
    }

    fn update(&mut self, mut data: &[u8]) {
        if self.pos > 0 {
            let take = (SHA3_512_RATE - self.pos).min(data.len());
            self.buf[self.pos..self.pos + take].copy_from_slice(&data[..take]);
            self.pos += take;
            data = &data[take..];
            if self.pos == SHA3_512_RATE {
                let blk = self.buf;
                self.absorb_block(&blk);
                self.pos = 0;
            }
        }
        while data.len() >= SHA3_512_RATE {
            let mut blk = [0u8; SHA3_512_RATE];
            blk.copy_from_slice(&data[..SHA3_512_RATE]);
            self.absorb_block(&blk);
            data = &data[SHA3_512_RATE..];
        }
        if !data.is_empty() {
            self.buf[..data.len()].copy_from_slice(data);
            self.pos = data.len();
        }
    }

    #[inline]
    fn finalize(mut self) -> [u8; 64] {
        self.buf[self.pos..SHA3_512_RATE].fill(0);
        self.buf[self.pos] ^= 0x06;
        self.buf[SHA3_512_RATE - 1] ^= 0x80;
        let blk = self.buf;
        self.absorb_block(&blk);
        let mut out = [0u8; 64];
        for (i, chunk) in out.as_chunks_mut::<8>().0.iter_mut().enumerate() {
            *chunk = self.st[i].to_le_bytes();
        }
        out
    }
}

fn b64_encode_u64_be(n: u64) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let b = n.to_be_bytes();
    let mut out = [0u8; 12];
    let mut j = 0;
    for chunk in b.chunks(3) {
        let v0 = u32::from(chunk[0]);
        let v1 = u32::from(*chunk.get(1).unwrap_or(&0));
        let v2 = u32::from(*chunk.get(2).unwrap_or(&0));
        let triple = (v0 << 16) | (v1 << 8) | v2;
        out[j] = TABLE[((triple >> 18) & 0x3f) as usize];
        out[j + 1] = TABLE[((triple >> 12) & 0x3f) as usize];
        out[j + 2] = if chunk.len() > 1 {
            TABLE[((triple >> 6) & 0x3f) as usize]
        } else {
            b'='
        };
        out[j + 3] = if chunk.len() > 2 {
            TABLE[(triple & 0x3f) as usize]
        } else {
            b'='
        };
        j += 4;
    }
    // SAFETY-FREE: every byte in `out` is drawn from ASCII TABLE or b'='.
    String::from_utf8(out.to_vec()).unwrap_or_default()
}

#[inline]
fn is_py_ws(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\x1c'..='\x1f')
}

enum ParsedHex {
    Invalid,
    Negative,
    NonNegative(Vec<u8>),
}

/// Exact Python `int(s, 16)` parser returning canonical big-endian unsigned bytes.
fn parse_py_int_hex(raw: &str) -> ParsedHex {
    let s = raw.trim_matches(is_py_ws);
    if s.is_empty() {
        return ParsedHex::Invalid;
    }
    let (negative, rest) = if let Some(r) = s.strip_prefix('-') {
        (true, r)
    } else if let Some(r) = s.strip_prefix('+') {
        (false, r)
    } else {
        (false, s)
    };
    let (has_prefix, body) =
        if let Some(r) = rest.strip_prefix("0x").or_else(|| rest.strip_prefix("0X")) {
            (true, r)
        } else {
            (false, rest)
        };
    if body.is_empty() {
        return ParsedHex::Invalid;
    }
    // Python allows underscores between digits (and one immediately after 0x/0X),
    // never trailing or consecutive, and never leading without 0x prefix.
    let bytes = body.as_bytes();
    if bytes[bytes.len() - 1] == b'_' || (!has_prefix && bytes[0] == b'_') {
        return ParsedHex::Invalid;
    }
    let mut nibbles: Vec<u8> = Vec::with_capacity(bytes.len());
    let mut prev_underscore = false;
    for &b in bytes {
        if b == b'_' {
            if prev_underscore {
                return ParsedHex::Invalid;
            }
            prev_underscore = true;
            continue;
        }
        prev_underscore = false;
        let v = match b {
            b'0'..=b'9' => b - b'0',
            b'a'..=b'f' => b - b'a' + 10,
            b'A'..=b'F' => b - b'A' + 10,
            _ => return ParsedHex::Invalid,
        };
        nibbles.push(v);
    }
    if nibbles.is_empty() {
        return ParsedHex::Invalid;
    }
    let first_nz = nibbles.iter().position(|&v| v != 0);
    let Some(start) = first_nz else {
        return ParsedHex::NonNegative(Vec::new());
    };
    if negative {
        return ParsedHex::Negative;
    }
    let sig = &nibbles[start..];
    let mut out = Vec::with_capacity(sig.len().div_ceil(2));
    let mut idx = 0;
    if !sig.len().is_multiple_of(2) {
        out.push(sig[0]);
        idx = 1;
    }
    while idx < sig.len() {
        out.push((sig[idx] << 4) | sig[idx + 1]);
        idx += 2;
    }
    ParsedHex::NonNegative(out)
}

#[inline]
fn fmt_u64_decimal(mut n: u64, buf: &mut [u8; 20]) -> &[u8] {
    if n == 0 {
        buf[19] = b'0';
        return &buf[19..];
    }
    let mut i = 20;
    while n > 0 {
        i -= 1;
        buf[i] = b'0' + (n % 10) as u8;
        n /= 10;
    }
    &buf[i..]
}

/// Port of `_solve_pow` (`sms_tool/registration/sentinel_tokens.py:208`).
pub fn _solve_pow(seed: String, difficulty_hex: String) -> Result<String, String> {
    let diff_be = match parse_py_int_hex(&difficulty_hex) {
        ParsedHex::Invalid => return Ok(String::new()),
        ParsedHex::Negative => {
            let prefix_len = difficulty_hex.chars().count().div_ceil(2);
            if prefix_len > 64 {
                return Err("IndexError: index out of range".to_string());
            }
            return Ok(String::new());
        }
        ParsedHex::NonNegative(be) => be,
    };

    let prefix_len = difficulty_hex.chars().count().div_ceil(2);
    if prefix_len > 64 {
        return Err("IndexError: index out of range".to_string());
    }
    if diff_be.len() > prefix_len {
        return Ok(b64_encode_u64_be(0));
    }

    let mut target = [0u8; 64];
    let pad = prefix_len - diff_be.len();
    target[pad..prefix_len].copy_from_slice(&diff_be);
    let target_slice = &target[..prefix_len];

    let mut base = Sha3_512State::new();
    base.update(seed.as_bytes());
    let mut num_buf = [0u8; 20];

    for n in 0..MAX_ATTEMPTS {
        let mut h = base.clone();
        h.update(fmt_u64_decimal(n, &mut num_buf));
        let digest = h.finalize();
        if &digest[..prefix_len] <= target_slice {
            return Ok(b64_encode_u64_be(n));
        }
    }
    Ok(String::new())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sha3_512_empty_matches_nist_vector() {
        let digest = Sha3_512State::new().finalize();
        let hex: String = digest.iter().map(|b| format!("{b:02x}")).collect();
        assert_eq!(
            hex,
            "a69f73cca23a9ac5c8b567dc185a756e97c982164fe25859e0d1dcc1475c80a615b2123af1f5f94c11e3e9402c3ac558f500199d95b6d3e301758586281dcd26"
        );
    }

    #[test]
    fn solve_pow_known_seed_and_invalid_hex() {
        assert_eq!(_solve_pow("abc".into(), "not-hex".into()).expect("ok"), "");
        assert_eq!(_solve_pow("abc".into(), "".into()).expect("ok"), "");
        assert_eq!(_solve_pow("abc".into(), "-1".into()).expect("ok"), "");
        let solved = _solve_pow("seed".into(), "ffff".into()).expect("ok");
        assert_eq!(solved, "AAAAAAAAAAA=");
    }
}
