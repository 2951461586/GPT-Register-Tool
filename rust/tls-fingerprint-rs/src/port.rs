//! Rust port of `permute_extension_order` in `sms_tool/transport/tls_fingerprint.py`.
//!
//! Implements FIPS 180-4 SHA-256 and CPython 3.12's exact MT19937 (`random.Random(int)`
//! + `_randbelow_with_getrandbits` Fisher-Yates shuffle) in zero-dependency safe Rust.

#![forbid(unsafe_code)]

const FIRST_EXTENSION: &str = "0";
const PADDING_EXTENSION: &str = "21";

const SHA256_K: [u32; 64] = [
    0x428a_2f98,
    0x7137_4491,
    0xb5c0_fbcf,
    0xe9b5_dba5,
    0x3956_c25b,
    0x59f1_11f1,
    0x923f_82a4,
    0xab1c_5ed5,
    0xd807_aa98,
    0x1283_5b01,
    0x2431_85be,
    0x550c_7dc3,
    0x72be_5d74,
    0x80de_b1fe,
    0x9bdc_06a7,
    0xc19b_f174,
    0xe49b_69c1,
    0xefbe_4786,
    0x0fc1_9dc6,
    0x240c_a1cc,
    0x2de9_2c6f,
    0x4a74_84aa,
    0x5cb0_a9dc,
    0x76f9_88da,
    0x983e_5152,
    0xa831_c66d,
    0xb003_27c8,
    0xbf59_7fc7,
    0xc6e0_0bf3,
    0xd5a7_9147,
    0x06ca_6351,
    0x1429_2967,
    0x27b7_0a85,
    0x2e1b_2138,
    0x4d2c_6dfc,
    0x5338_0d13,
    0x650a_7354,
    0x766a_0abb,
    0x81c2_c92e,
    0x9272_2c85,
    0xa2bf_e8a1,
    0xa81a_664b,
    0xc24b_8b70,
    0xc76c_51a3,
    0xd192_e819,
    0xd699_0624,
    0xf40e_3585,
    0x106a_a070,
    0x19a4_c116,
    0x1e37_6c08,
    0x2748_774c,
    0x34b0_bcb5,
    0x391c_0cb3,
    0x4ed8_aa4a,
    0x5b9c_ca4f,
    0x682e_6ff3,
    0x748f_82ee,
    0x78a5_636f,
    0x84c8_7814,
    0x8cc7_0208,
    0x90be_fffa,
    0xa450_6ceb,
    0xbef9_a3f7,
    0xc671_78f2,
];

fn sha256_first_u64(data: &[u8]) -> u64 {
    let mut h: [u32; 8] = [
        0x6a09_e667,
        0xbb67_ae85,
        0x3c6e_f372,
        0xa54f_f53a,
        0x510e_527f,
        0x9b05_688c,
        0x1f83_d9ab,
        0x5be0_cd19,
    ];

    let bit_len = (data.len() as u64).wrapping_mul(8);
    let pad_len = (64 - ((data.len() + 9) % 64)) % 64;
    let total_len = data.len() + 1 + pad_len + 8;
    let mut msg = Vec::with_capacity(total_len);
    msg.extend_from_slice(data);
    msg.push(0x80);
    msg.resize(total_len - 8, 0u8);
    msg.extend_from_slice(&bit_len.to_be_bytes());

    let mut w = [0u32; 64];
    for chunk in msg.as_chunks::<64>().0 {
        for (i, word) in w.iter_mut().take(16).enumerate() {
            let o = i * 4;
            *word = u32::from_be_bytes([chunk[o], chunk[o + 1], chunk[o + 2], chunk[o + 3]]);
        }
        for i in 16..64 {
            let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
            let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16]
                .wrapping_add(s0)
                .wrapping_add(w[i - 7])
                .wrapping_add(s1);
        }

        let [mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut hh] = h;
        for i in 0..64 {
            let s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let ch = (e & f) ^ ((!e) & g);
            let temp1 = hh
                .wrapping_add(s1)
                .wrapping_add(ch)
                .wrapping_add(SHA256_K[i])
                .wrapping_add(w[i]);
            let s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let maj = (a & b) ^ (a & c) ^ (b & c);
            let temp2 = s0.wrapping_add(maj);

            hh = g;
            g = f;
            f = e;
            e = d.wrapping_add(temp1);
            d = c;
            c = b;
            b = a;
            a = temp1.wrapping_add(temp2);
        }

        h[0] = h[0].wrapping_add(a);
        h[1] = h[1].wrapping_add(b);
        h[2] = h[2].wrapping_add(c);
        h[3] = h[3].wrapping_add(d);
        h[4] = h[4].wrapping_add(e);
        h[5] = h[5].wrapping_add(f);
        h[6] = h[6].wrapping_add(g);
        h[7] = h[7].wrapping_add(hh);
    }

    ((h[0] as u64) << 32) | (h[1] as u64)
}

struct PyMt19937 {
    mt: [u32; 624],
    mti: usize,
}

impl PyMt19937 {
    fn from_u64_seed(seed: u64) -> Self {
        let mut key_buf = [0u32; 2];
        let key: &[u32] = if seed <= u32::MAX as u64 {
            key_buf[0] = seed as u32;
            &key_buf[..1]
        } else {
            key_buf[0] = seed as u32;
            key_buf[1] = (seed >> 32) as u32;
            &key_buf[..2]
        };
        Self::init_by_array(key)
    }

    fn init_genrand(s: u32) -> Self {
        let mut mt = [0u32; 624];
        mt[0] = s;
        for i in 1..624usize {
            let prev = mt[i - 1];
            mt[i] = 1_812_433_253u32
                .wrapping_mul(prev ^ (prev >> 30))
                .wrapping_add(i as u32);
        }
        Self { mt, mti: 624 }
    }

    fn init_by_array(init_key: &[u32]) -> Self {
        let mut rng = Self::init_genrand(19_650_218u32);
        let mut i = 1usize;
        let mut j = 0usize;
        let mut k = 624usize.max(init_key.len());
        while k > 0 {
            let prev = rng.mt[i - 1];
            rng.mt[i] = (rng.mt[i] ^ ((prev ^ (prev >> 30)).wrapping_mul(1_664_525u32)))
                .wrapping_add(init_key[j])
                .wrapping_add(j as u32);
            i += 1;
            j += 1;
            if i >= 624 {
                rng.mt[0] = rng.mt[623];
                i = 1;
            }
            if j >= init_key.len() {
                j = 0;
            }
            k -= 1;
        }
        for _ in 0..623 {
            let prev = rng.mt[i - 1];
            rng.mt[i] = (rng.mt[i] ^ ((prev ^ (prev >> 30)).wrapping_mul(1_566_083_941u32)))
                .wrapping_sub(i as u32);
            i += 1;
            if i >= 624 {
                rng.mt[0] = rng.mt[623];
                i = 1;
            }
        }
        rng.mt[0] = 0x8000_0000;
        rng
    }

    fn genrand_uint32(&mut self) -> u32 {
        const N: usize = 624;
        const M: usize = 397;
        const MATRIX_A: u32 = 0x9908_b0df;
        const UPPER_MASK: u32 = 0x8000_0000;
        const LOWER_MASK: u32 = 0x7fff_ffff;

        if self.mti >= N {
            for kk in 0..(N - M) {
                let y = (self.mt[kk] & UPPER_MASK) | (self.mt[kk + 1] & LOWER_MASK);
                self.mt[kk] = self.mt[kk + M] ^ (y >> 1) ^ if y & 1 != 0 { MATRIX_A } else { 0 };
            }
            for kk in (N - M)..(N - 1) {
                let y = (self.mt[kk] & UPPER_MASK) | (self.mt[kk + 1] & LOWER_MASK);
                self.mt[kk] =
                    self.mt[kk + M - N] ^ (y >> 1) ^ if y & 1 != 0 { MATRIX_A } else { 0 };
            }
            let y = (self.mt[N - 1] & UPPER_MASK) | (self.mt[0] & LOWER_MASK);
            self.mt[N - 1] = self.mt[M - 1] ^ (y >> 1) ^ if y & 1 != 0 { MATRIX_A } else { 0 };
            self.mti = 0;
        }

        let mut y = self.mt[self.mti];
        self.mti += 1;
        y ^= y >> 11;
        y ^= (y << 7) & 0x9d2c_5680;
        y ^= (y << 15) & 0xefc6_0000;
        y ^= y >> 18;
        y
    }

    #[inline]
    fn getrandbits(&mut self, k: u32) -> u32 {
        self.genrand_uint32() >> (32 - k)
    }

    #[inline]
    fn randbelow(&mut self, n: usize) -> usize {
        let k = 32 - (n as u32).leading_zeros();
        loop {
            let r = self.getrandbits(k) as usize;
            if r < n {
                return r;
            }
        }
    }

    fn shuffle<T>(&mut self, items: &mut [T]) {
        for i in (1..items.len()).rev() {
            let j = self.randbelow(i + 1);
            items.swap(i, j);
        }
    }
}

pub fn permute_extension_order(base: String, seed: String) -> Result<String, String> {
    if seed.is_empty() {
        return Ok(String::new());
    }
    let parts: Vec<&str> = base.split('-').filter(|p| !p.is_empty()).collect();
    if parts.len() < 3 {
        return Ok(String::new());
    }
    let has_head = parts[0] == FIRST_EXTENSION;
    let rest = if has_head { &parts[1..] } else { &parts[..] };
    let mut middle: Vec<&str> = Vec::with_capacity(rest.len());
    let mut has_tail = false;
    for &part in rest {
        if part == PADDING_EXTENSION {
            has_tail = true;
        } else {
            middle.push(part);
        }
    }
    if middle.len() < 2 {
        return Ok(String::new());
    }

    let mut payload = Vec::with_capacity(13 + seed.len());
    payload.extend_from_slice(b"tls-order/v1/");
    payload.extend_from_slice(seed.as_bytes());
    let seed_u64 = sha256_first_u64(&payload);
    let mut rng = PyMt19937::from_u64_seed(seed_u64);
    rng.shuffle(&mut middle);

    let mut out = String::with_capacity(base.len() + 4);
    if has_head {
        out.push_str(FIRST_EXTENSION);
    }
    for part in middle {
        if !out.is_empty() {
            out.push('-');
        }
        out.push_str(part);
    }
    if has_tail {
        if !out.is_empty() {
            out.push('-');
        }
        out.push_str(PADDING_EXTENSION);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn preserves_head_tail_and_set() {
        let base = "0-23-65281-10-11-35-16-5-34-18-51-43-13-45-28-27-65037-21";
        let out = permute_extension_order(base.to_string(), "device-1|salt".to_string()).unwrap();
        let parts: Vec<&str> = out.split('-').collect();
        assert_eq!(parts.first().copied(), Some("0"));
        assert_eq!(parts.last().copied(), Some("21"));
        assert_eq!(parts.len(), 18);
    }

    #[test]
    fn empty_or_short_returns_empty() {
        assert_eq!(
            permute_extension_order("0-23".to_string(), "s".to_string()).unwrap(),
            ""
        );
        assert_eq!(
            permute_extension_order("0-23-10".to_string(), "".to_string()).unwrap(),
            ""
        );
    }
}
