//! Sentinel token 生成层 —— 与纯 Python 版 `sentinel_py.py` 逐字节一致。
//!
//! 对应关系：
//! | Python                                | Rust                              |
//! |---------------------------------------|-----------------------------------|
//! | `_profile(**kw)`                       | [`Profile::new`]                  |
//! | `_profile_from_fingerprint(fp)`        | [`Profile::from_fingerprint`]     |
//! | `build_config(profile, sid, _)`        | [`build_config`]                  |
//! | `_pow(seed, difficulty, config)`       | [`pow`]                           |
//! | `requirements_token(profile, sid)`     | [`requirements_token`]            |
//! | `enforcement_token(challenge, ..)`     | [`enforcement_token`]             |
//! | `_b64 / _imul / _fnv1a / _js_date`     | [`b64`] / [`imul`] / [`fnv1a`] / [`js_date`] |
//!
//! 设计约束（与 Python 完全对齐，务必保留）：
//! * `config` 固定 25 个字段、顺序固定；`config[3]`=PoW attempt，`config[9]`=elapsed ms。
//! * 时间/随机全部由调用方注入（[`Rng`] / [`Clock`] / `now_ms`），函数内部不引入额外随机；
//!   Python 侧的 `uuid.uuid4()` seed 在 Rust 侧由调用方以字符串传入。
//! * JSON 序列化手工实现，浮点用 [`py_float_repr`]（CPython `repr` 规则），
//!   字符串用 serde_json 转义（与 `json.dumps(ensure_ascii=False)` 一致）。
//! * `fnv1a` 按 Unicode 标量（Python `ord(ch)`）迭代，**不是**按字节。

use serde_json::Value;

// ---------------------------------------------------------------- 常量

/// sdk.js 版本号（与 Python 侧一致）。
pub const SENTINEL_VERSION: &str = "20260810913b";

/// Python `_pow` 的 `max_attempts` 默认值。
pub const POW_MAX_ATTEMPTS: u32 = 500_000;

/// sdk.js `getConfig()` 探测的窗口字段（顺序不能变）。
pub const WINDOW_FLAGS: [&str; 7] =
    ["ai", "createPRNG", "cache", "data", "solana", "dump", "InstallTrigger"];

/// `_NAV_KEYS`
pub const NAV_KEYS: [&str; 6] = [
    "doNotTrack",
    "language",
    "platform",
    "vendor",
    "webdriver",
    "hardwareConcurrency",
];

/// `_DOC_KEYS`
pub const DOC_KEYS: [&str; 13] = [
    "head",
    "body",
    "scripts",
    "URL",
    "documentURI",
    "referrer",
    "title",
    "cookie",
    "readyState",
    "characterSet",
    "contentType",
    "location",
    "hidden",
];

/// `_WIN_KEYS`
pub const WIN_KEYS: [&str; 15] = [
    "setTimeout",
    "clearTimeout",
    "fetch",
    "location",
    "history",
    "navigator",
    "document",
    "screen",
    "localStorage",
    "sessionStorage",
    "performance",
    "cancelIdleCallback",
    "requestIdleCallback",
    "matchMedia",
    "indexedDB",
];

/// `_NAV_VALUES` 查表（命中不到时返回 `undefined`，与 Python 一致）。
pub fn nav_value(key: &str) -> &'static str {
    match key {
        "doNotTrack" => "null",
        "language" => "ja-JP",
        "platform" => "Win32",
        "vendor" => "Google Inc.",
        "webdriver" => "false",
        "hardwareConcurrency" => "12",
        _ => "undefined",
    }
}

/// `_TZ_TABLE` 的一条记录。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TzInfo {
    pub en: &'static str,
    pub ja: &'static str,
    pub zh: &'static str,
    /// 分钟，东八区 = 480
    pub offset: i32,
}

/// `_TZ_TABLE`
pub fn tz_table(name: &str) -> Option<TzInfo> {
    let info = match name {
        "Asia/Tokyo" => TzInfo {
            en: "Japan Standard Time",
            ja: "日本標準時",
            zh: "日本标准时间",
            offset: 540,
        },
        "Asia/Shanghai" => TzInfo {
            en: "China Standard Time",
            ja: "中国標準時",
            zh: "中国标准时间",
            offset: 480,
        },
        "Asia/Singapore" => TzInfo {
            en: "Singapore Standard Time",
            ja: "シンガポール標準時",
            zh: "新加坡标准时间",
            offset: 480,
        },
        "UTC" => TzInfo {
            en: "Coordinated Universal Time",
            ja: "協定世界時",
            zh: "协调世界时",
            offset: 0,
        },
        "America/New_York" => TzInfo {
            en: "Eastern Standard Time",
            ja: "東部標準時",
            zh: "东部标准时间",
            offset: -300,
        },
        "America/Los_Angeles" => TzInfo {
            en: "Pacific Standard Time",
            ja: "太平洋標準時",
            zh: "太平洋标准时间",
            offset: -480,
        },
        // 下面这批是设备指纹国家画像（`fingerprint::COUNTRY_PROFILES`）用到的时区：
        // 缺了它们，`Profile::new` 会把偏移退成默认 +540，时区名和偏移就自相矛盾。
        "Europe/London" => TzInfo {
            en: "Greenwich Mean Time",
            ja: "グリニッジ標準時",
            zh: "格林尼治标准时间",
            offset: 0,
        },
        "Europe/Berlin" => TzInfo {
            en: "Central European Time",
            ja: "中央ヨーロッパ時間",
            zh: "中欧时间",
            offset: 60,
        },
        "Europe/Paris" => TzInfo {
            en: "Central European Time",
            ja: "中央ヨーロッパ時間",
            zh: "中欧时间",
            offset: 60,
        },
        "Europe/Amsterdam" => TzInfo {
            en: "Central European Time",
            ja: "中央ヨーロッパ時間",
            zh: "中欧时间",
            offset: 60,
        },
        "Australia/Sydney" => TzInfo {
            en: "Australian Eastern Standard Time",
            ja: "オーストラリア東部標準時",
            zh: "澳大利亚东部标准时间",
            offset: 600,
        },
        "Asia/Seoul" => TzInfo {
            en: "Korea Standard Time",
            ja: "韓国標準時",
            zh: "韩国标准时间",
            offset: 540,
        },
        "America/Toronto" => TzInfo {
            en: "Eastern Standard Time",
            ja: "東部標準時",
            zh: "东部标准时间",
            offset: -300,
        },
        // ── 以下补齐 `fingerprint::COUNTRY_PROFILES`（Python `_COUNTRY_PROFILES`）
        // 用到的全部时区。缺一个就意味着 `Profile::new` 把偏移退成默认 +540，
        // 出现「时区名说 Europe/London、偏移说 +540」的自相矛盾。
        // 偏移取该时区的**标准时**（不含夏令时），与 Python 侧非夏令时时段一致。
        "Asia/Hong_Kong" => TzInfo {
            en: "Hong Kong Standard Time",
            ja: "香港標準時",
            zh: "香港标准时间",
            offset: 480,
        },
        "Asia/Taipei" => TzInfo {
            en: "Taipei Standard Time",
            ja: "台湾標準時",
            zh: "台北标准时间",
            offset: 480,
        },
        "Asia/Kuala_Lumpur" => TzInfo {
            en: "Malaysia Time",
            ja: "マレーシア時間",
            zh: "马来西亚时间",
            offset: 480,
        },
        "Asia/Bangkok" => TzInfo {
            en: "Indochina Time",
            ja: "インドシナ時間",
            zh: "印度支那时间",
            offset: 420,
        },
        "Asia/Ho_Chi_Minh" => TzInfo {
            en: "Indochina Time",
            ja: "インドシナ時間",
            zh: "印度支那时间",
            offset: 420,
        },
        "Asia/Kolkata" => TzInfo {
            en: "India Standard Time",
            ja: "インド標準時",
            zh: "印度标准时间",
            offset: 330,
        },
        "Asia/Jakarta" => TzInfo {
            en: "Western Indonesia Time",
            ja: "インドネシア西部時間",
            zh: "印度尼西亚西部时间",
            offset: 420,
        },
        "Asia/Manila" => TzInfo {
            en: "Philippine Standard Time",
            ja: "フィリピン標準時",
            zh: "菲律宾标准时间",
            offset: 480,
        },
        "Asia/Karachi" => TzInfo {
            en: "Pakistan Standard Time",
            ja: "パキスタン標準時",
            zh: "巴基斯坦标准时间",
            offset: 300,
        },
        "Asia/Dhaka" => TzInfo {
            en: "Bangladesh Standard Time",
            ja: "バングラデシュ標準時",
            zh: "孟加拉国标准时间",
            offset: 360,
        },
        "Asia/Jerusalem" => TzInfo {
            en: "Israel Standard Time",
            ja: "イスラエル標準時",
            zh: "以色列标准时间",
            offset: 120,
        },
        "Asia/Riyadh" => TzInfo {
            en: "Arabia Standard Time",
            ja: "アラビア標準時",
            zh: "阿拉伯标准时间",
            offset: 180,
        },
        "Asia/Dubai" => TzInfo {
            en: "Gulf Standard Time",
            ja: "湾岸標準時",
            zh: "海湾标准时间",
            offset: 240,
        },
        "Asia/Yekaterinburg" => TzInfo {
            en: "Yekaterinburg Standard Time",
            ja: "エカテリンブルク標準時",
            zh: "叶卡捷琳堡标准时间",
            offset: 300,
        },
        "Asia/Novosibirsk" => TzInfo {
            en: "Novosibirsk Standard Time",
            ja: "ノボシビルスク標準時",
            zh: "新西伯利亚标准时间",
            offset: 420,
        },
        "Europe/Istanbul" => TzInfo {
            en: "Turkey Time",
            ja: "トルコ時間",
            zh: "土耳其时间",
            offset: 180,
        },
        "Europe/Rome" => TzInfo {
            en: "Central European Time",
            ja: "中央ヨーロッパ時間",
            zh: "中欧时间",
            offset: 60,
        },
        "Europe/Madrid" => TzInfo {
            en: "Central European Time",
            ja: "中央ヨーロッパ時間",
            zh: "中欧时间",
            offset: 60,
        },
        "Europe/Brussels" => TzInfo {
            en: "Central European Time",
            ja: "中央ヨーロッパ時間",
            zh: "中欧时间",
            offset: 60,
        },
        "Europe/Zurich" => TzInfo {
            en: "Central European Time",
            ja: "中央ヨーロッパ時間",
            zh: "中欧时间",
            offset: 60,
        },
        "Europe/Stockholm" => TzInfo {
            en: "Central European Time",
            ja: "中央ヨーロッパ時間",
            zh: "中欧时间",
            offset: 60,
        },
        "Europe/Oslo" => TzInfo {
            en: "Central European Time",
            ja: "中央ヨーロッパ時間",
            zh: "中欧时间",
            offset: 60,
        },
        "Europe/Copenhagen" => TzInfo {
            en: "Central European Time",
            ja: "中央ヨーロッパ時間",
            zh: "中欧时间",
            offset: 60,
        },
        "Europe/Warsaw" => TzInfo {
            en: "Central European Time",
            ja: "中央ヨーロッパ時間",
            zh: "中欧时间",
            offset: 60,
        },
        "Europe/Prague" => TzInfo {
            en: "Central European Time",
            ja: "中央ヨーロッパ時間",
            zh: "中欧时间",
            offset: 60,
        },
        "Europe/Vienna" => TzInfo {
            en: "Central European Time",
            ja: "中央ヨーロッパ時間",
            zh: "中欧时间",
            offset: 60,
        },
        "Europe/Helsinki" => TzInfo {
            en: "Eastern European Standard Time",
            ja: "東ヨーロッパ標準時",
            zh: "东欧标准时间",
            offset: 120,
        },
        "Europe/Kiev" => TzInfo {
            en: "Eastern European Standard Time",
            ja: "東ヨーロッパ標準時",
            zh: "东欧标准时间",
            offset: 120,
        },
        "Europe/Athens" => TzInfo {
            en: "Eastern European Standard Time",
            ja: "東ヨーロッパ標準時",
            zh: "东欧标准时间",
            offset: 120,
        },
        "Europe/Moscow" => TzInfo {
            en: "Moscow Standard Time",
            ja: "モスクワ標準時",
            zh: "莫斯科标准时间",
            offset: 180,
        },
        "Europe/Lisbon" => TzInfo {
            en: "Western European Standard Time",
            ja: "西ヨーロッパ標準時",
            zh: "西欧标准时间",
            offset: 0,
        },
        "America/Chicago" => TzInfo {
            en: "Central Standard Time",
            ja: "中部標準時",
            zh: "中部标准时间",
            offset: -360,
        },
        "America/Denver" => TzInfo {
            en: "Mountain Standard Time",
            ja: "山地標準時",
            zh: "山地标准时间",
            offset: -420,
        },
        "America/Edmonton" => TzInfo {
            en: "Mountain Standard Time",
            ja: "山地標準時",
            zh: "山地标准时间",
            offset: -420,
        },
        "America/Vancouver" => TzInfo {
            en: "Pacific Standard Time",
            ja: "太平洋標準時",
            zh: "太平洋标准时间",
            offset: -480,
        },
        "America/Mexico_City" => TzInfo {
            en: "Central Standard Time (Mexico)",
            ja: "メキシコ中部標準時",
            zh: "墨西哥中部标准时间",
            offset: -360,
        },
        "America/Bogota" => TzInfo {
            en: "Colombia Standard Time",
            ja: "コロンビア標準時",
            zh: "哥伦比亚标准时间",
            offset: -300,
        },
        "America/Santiago" => TzInfo {
            en: "Chile Standard Time",
            ja: "チリ標準時",
            zh: "智利标准时间",
            offset: -240,
        },
        "America/Sao_Paulo" => TzInfo {
            en: "Brasilia Standard Time",
            ja: "ブラジリア標準時",
            zh: "巴西利亚标准时间",
            offset: -180,
        },
        "America/Fortaleza" => TzInfo {
            en: "Brasilia Standard Time",
            ja: "ブラジリア標準時",
            zh: "巴西利亚标准时间",
            offset: -180,
        },
        "America/Manaus" => TzInfo {
            en: "Amazon Standard Time",
            ja: "アマゾン標準時",
            zh: "亚马逊标准时间",
            offset: -240,
        },
        "America/Argentina/Buenos_Aires" => TzInfo {
            en: "Argentina Standard Time",
            ja: "アルゼンチン標準時",
            zh: "阿根廷标准时间",
            offset: -180,
        },
        "Australia/Melbourne" => TzInfo {
            en: "Australian Eastern Standard Time",
            ja: "オーストラリア東部標準時",
            zh: "澳大利亚东部标准时间",
            offset: 600,
        },
        "Australia/Brisbane" => TzInfo {
            en: "Australian Eastern Standard Time",
            ja: "オーストラリア東部標準時",
            zh: "澳大利亚东部标准时间",
            offset: 600,
        },
        "Pacific/Auckland" => TzInfo {
            en: "New Zealand Standard Time",
            ja: "ニュージーランド標準時",
            zh: "新西兰标准时间",
            offset: 720,
        },
        "Africa/Johannesburg" => TzInfo {
            en: "South Africa Standard Time",
            ja: "南アフリカ標準時",
            zh: "南非标准时间",
            offset: 120,
        },
        "Africa/Cairo" => TzInfo {
            en: "Eastern European Standard Time",
            ja: "東ヨーロッパ標準時",
            zh: "东欧标准时间",
            offset: 120,
        },
        "Africa/Lagos" => TzInfo {
            en: "West Africa Standard Time",
            ja: "西アフリカ標準時",
            zh: "西非标准时间",
            offset: 60,
        },
        "Africa/Nairobi" => TzInfo {
            en: "East Africa Time",
            ja: "東アフリカ時間",
            zh: "东非时间",
            offset: 180,
        },
        _ => return None,
    };
    Some(info)
}

// ------------------------------------------------------- 数值/编码基础工具

/// `_imul(a, b)`：32 位截断乘法。
#[inline]
pub fn imul(a: u32, b: u32) -> u32 {
    a.wrapping_mul(b)
}

/// `_b64`：标准 base64（含 `=` 填充）编码 UTF-8 文本。
pub fn b64(text: &str) -> String {
    use base64::Engine as _;
    base64::engine::general_purpose::STANDARD.encode(text.as_bytes())
}

/// `_fnv1a`：FNV-1a（2166136261 / 16777619）+ 两轮混淆（2246822507 / 3266489909）。
/// 按 Unicode 标量迭代，返回 8 位小写十六进制。
pub fn fnv1a(text: &str) -> String {
    let mut h: u32 = 2_166_136_261;
    for ch in text.chars() {
        h ^= ch as u32;
        h = imul(h, 16_777_619);
    }
    h ^= h >> 16;
    h = imul(h, 2_246_822_507);
    h ^= h >> 13;
    h = imul(h, 3_266_489_909);
    h ^= h >> 16;
    format!("{:08x}", h)
}

/// CPython `repr(float)`：最短往返十进制，`-4 < decpt <= 16` 用定点，否则用科学计数。
pub fn py_float_repr(x: f64) -> String {
    if x.is_nan() {
        return "NaN".to_string();
    }
    if x.is_infinite() {
        return if x.is_sign_negative() {
            "-Infinity".to_string()
        } else {
            "Infinity".to_string()
        };
    }
    if x == 0.0 {
        return if x.is_sign_negative() {
            "-0.0".to_string()
        } else {
            "0.0".to_string()
        };
    }

    let neg = x.is_sign_negative();
    // Rust 的 `{:e}` 给出最短往返有效数字，例如 "1.2345e-7"
    let sci = format!("{:e}", x.abs());
    let (mant, exp) = sci.split_once('e').expect("e notation");
    let exp: i32 = exp.parse().expect("exponent");
    let digits: Vec<char> = mant.chars().filter(|c| *c != '.').collect();
    let decpt: i32 = exp + 1;

    let mut out = String::with_capacity(digits.len() + 8);
    if neg {
        out.push('-');
    }
    if decpt <= -4 || decpt > 16 {
        out.push(digits[0]);
        if digits.len() > 1 {
            out.push('.');
            out.extend(&digits[1..]);
        }
        let e = decpt - 1;
        out.push('e');
        out.push(if e < 0 { '-' } else { '+' });
        let ae = e.unsigned_abs();
        if ae < 10 {
            out.push('0');
        }
        out.push_str(&ae.to_string());
    } else if decpt <= 0 {
        out.push_str("0.");
        for _ in 0..(-decpt) {
            out.push('0');
        }
        out.extend(&digits);
    } else if decpt as usize >= digits.len() {
        out.extend(&digits);
        for _ in 0..(decpt as usize - digits.len()) {
            out.push('0');
        }
        out.push_str(".0");
    } else {
        out.extend(&digits[..decpt as usize]);
        out.push('.');
        out.extend(&digits[decpt as usize..]);
    }
    out
}

/// CPython `round(x, 17)`：正确舍入到小数点后 17 位再回转成 f64。
pub fn py_round_17(x: f64) -> f64 {
    if !x.is_finite() {
        return x;
    }
    // Rust 的 `{:.17}` 对精确值做正确舍入（半偶），与 CPython dtoa mode 3 一致；
    // 解析回来即“最近的 double”。
    format!("{:.17}", x).parse::<f64>().unwrap_or(x)
}

/// CPython `round(x)`（无 ndigits）：四舍六入五成双，返回整数。
pub fn py_round_int(x: f64) -> i64 {
    if !x.is_finite() {
        return 0;
    }
    x.round_ties_even() as i64
}

/// `json.dumps(value, ensure_ascii=False)` 的字符串转义（复用 serde_json 的 writer）。
pub fn json_string(s: &str) -> String {
    serde_json::to_string(s).expect("string serialization never fails")
}

// --------------------------------------------------------------- 日期串

const WEEKDAY_ABBR: [&str; 7] = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const MONTH_ABBR: [&str; 12] = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

fn div_floor(a: i64, b: i64) -> i64 {
    let q = a / b;
    if (a % b != 0) && ((a < 0) != (b < 0)) {
        q - 1
    } else {
        q
    }
}

/// Howard Hinnant `civil_from_days`：epoch 天数 → (year, month, day)。
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = div_floor(z, 146_097);
    let doe = (z - era * 146_097) as i64; // [0, 146096]
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365; // [0, 399]
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let d = doy - (153 * mp + 2) / 5 + 1; // [1, 31]
    let m = if mp < 10 { mp + 3 } else { mp - 9 }; // [1, 12]
    (y + if m <= 2 { 1 } else { 0 }, m as u32, d as u32)
}

/// `_js_date(profile)`：`%a %b %d %Y %H:%M:%S GMT+HHMM (tz_label)`。
///
/// `now_ms` 为 UTC epoch 毫秒；本地时间 = UTC + `timezone_offset` 分钟。
pub fn js_date(profile: &Profile, now_ms: f64) -> String {
    let offset_minutes = profile.timezone_offset as i64;
    let sec = (now_ms / 1000.0).floor() as i64;
    let local = sec + offset_minutes * 60;
    let days = div_floor(local, 86_400);
    let sod = local - days * 86_400;
    let (year, month, day) = civil_from_days(days);
    let weekday = (days % 7 + 7 + 3) % 7; // 1970-01-01 是周四（Mon=0）
    let (hh, mm, ss) = (sod / 3600, (sod % 3600) / 60, sod % 60);
    let sign = if offset_minutes >= 0 { '+' } else { '-' };
    let off = offset_minutes.abs();

    format!(
        "{} {} {:02} {:04} {:02}:{:02}:{:02} GMT{}{:02}{:02} ({})",
        WEEKDAY_ABBR[weekday as usize],
        MONTH_ABBR[(month - 1) as usize],
        day,
        year,
        hh,
        mm,
        ss,
        sign,
        off / 60,
        off % 60,
        profile.tz_label
    )
}

/// `_tz_label(timezone_name, language)`。
pub fn tz_label(timezone_name: &str, language: &str) -> String {
    let info = match tz_table(timezone_name) {
        Some(info) => info,
        None => return timezone_name.to_string(),
    };
    let lang_src = if language.is_empty() { "en" } else { language };
    let lang = lang_src
        .split('-')
        .next()
        .unwrap_or("en")
        .to_lowercase();
    let picked = match lang.as_str() {
        "ja" => Some(info.ja),
        "zh" => Some(info.zh),
        "en" => Some(info.en),
        _ => None,
    };
    match picked {
        Some(s) if !s.is_empty() => s.to_string(),
        _ => info.en.to_string(),
    }
}

// ------------------------------------------------------------ 随机 / 时钟

/// 随机源。所有取值都统一走 `next_f64()`（语义 = Python `random.random()`），
/// `choice` / `uniform` 都从它派生，保证与 Python 打桩后的行为一致。
pub enum Rng {
    /// `random.random() == v`
    Const(f64),
    /// `state = (1103515245*state + 12345) mod 2**31; u = state / 2**31`
    Lcg { state: u64 },
    /// 生产路径：真随机（rand crate）
    System(rand::rngs::ThreadRng),
}

impl Rng {
    pub fn constant(v: f64) -> Self {
        Rng::Const(v)
    }

    pub fn lcg(seed: u64) -> Self {
        Rng::Lcg { state: seed }
    }

    pub fn system() -> Self {
        Rng::System(rand::thread_rng())
    }

    /// `random.random()`
    pub fn next_f64(&mut self) -> f64 {
        match self {
            Rng::Const(v) => *v,
            Rng::Lcg { state } => {
                *state = (1_103_515_245u64
                    .wrapping_mul(*state)
                    .wrapping_add(12_345))
                    & 0x7fff_ffff;
                (*state as f64) / 2_147_483_648.0
            }
            Rng::System(r) => {
                use rand::Rng as _;
                r.gen::<f64>()
            }
        }
    }

    /// `random.uniform(a, b)` = `a + (b - a) * random()`
    pub fn uniform(&mut self, a: f64, b: f64) -> f64 {
        a + (b - a) * self.next_f64()
    }

    /// `random.choice(seq)` 的索引：`int(random() * len)`
    pub fn choice_index(&mut self, len: usize) -> usize {
        if len == 0 {
            return 0;
        }
        let idx = (self.next_f64() * len as f64) as usize;
        idx.min(len - 1)
    }
}

/// PoW 里 `time.time()*1000` 的来源。
pub enum Clock {
    /// 固定时刻（用于复现/比对）
    Fixed(f64),
    /// 生产路径：单调时钟（elapsed 语义，比墙钟更稳）
    System(std::time::Instant),
}

impl Clock {
    pub fn fixed(ms: f64) -> Self {
        Clock::Fixed(ms)
    }

    pub fn system() -> Self {
        Clock::System(std::time::Instant::now())
    }

    pub fn now_ms(&self) -> f64 {
        match self {
            Clock::Fixed(v) => *v,
            Clock::System(t) => t.elapsed().as_secs_f64() * 1000.0,
        }
    }
}

/// 注入到 token 生成层的全部外部输入。
pub struct TokenCtx {
    /// `build_config` 看到的 `time.time() * 1000`
    pub now_ms: f64,
    pub rng: Rng,
    pub clock: Clock,
    pub max_attempts: u32,
}

impl TokenCtx {
    pub fn new(now_ms: f64, rng: Rng) -> Self {
        TokenCtx {
            now_ms,
            rng,
            clock: Clock::fixed(now_ms),
            max_attempts: POW_MAX_ATTEMPTS,
        }
    }

    pub fn with_clock(now_ms: f64, rng: Rng, clock: Clock) -> Self {
        TokenCtx {
            now_ms,
            rng,
            clock,
            max_attempts: POW_MAX_ATTEMPTS,
        }
    }

    /// 生产用：墙钟 + 单调时钟 + 真随机。
    pub fn system() -> Self {
        let now_ms = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs_f64() * 1000.0)
            .unwrap_or(0.0);
        TokenCtx {
            now_ms,
            rng: Rng::system(),
            clock: Clock::system(),
            max_attempts: POW_MAX_ATTEMPTS,
        }
    }
}

// --------------------------------------------------------------- Profile

/// `ua_brands` 的一项（由 `sec_ch_ua` 解析出的 brand/version）。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Brand {
    pub brand: String,
    pub version: String,
}

/// 与 Python `profile` dict 一一对应的结构体。
#[derive(Clone, Debug, PartialEq)]
pub struct Profile {
    pub browser_family: String,
    pub user_agent: String,
    pub language: String,
    pub languages: Vec<String>,
    pub timezone_name: String,
    /// 分钟，东八区 = 480
    pub timezone_offset: i32,
    pub tz_label: String,
    pub screen_width: i64,
    pub screen_height: i64,
    pub hardware_concurrency: i64,
    pub platform: String,
    pub vendor: String,
    pub device_memory: i64,
    pub js_heap_size_limit: i64,
    pub device_pixel_ratio: f64,
    pub max_touch_points: i64,
    pub ua_brands: Vec<Brand>,
    pub ua_platform_version: String,
    pub ua_arch: String,
    pub ua_bitness: String,
    pub is_mobile: bool,
    pub script_src: Option<String>,
    pub page_url: String,
    pub cookie: String,
    /// `time.time()*1000 - random.uniform(8, 40)` 或由调用方注入的 `time_origin`
    pub page_time_origin_ms: f64,
    pub time_origin_explicit: bool,
}

/// `_profile(**kw)` 的参数（对应 Python 关键字参数）。
#[derive(Clone, Debug, Default)]
pub struct ProfileOptions {
    pub user_agent: Option<String>,
    pub screen: Option<String>,
    /// 对应 `lang`
    pub lang: Option<String>,
    /// 对应 `languages` / `lang_full`（逗号分隔，可带 `;q=`）
    pub languages: Option<String>,
    pub platform: Option<String>,
    pub vendor: Option<String>,
    pub hardware_concurrency: Option<i64>,
    pub device_memory: Option<i64>,
    pub device_pixel_ratio: Option<f64>,
    pub timezone: Option<String>,
    /// `navigator.userAgentData.brands`（由 `sec-ch-ua` 解析出的 brand/version）
    pub ua_brands: Option<Vec<Brand>>,
    /// `sec-ch-ua-platform-version` 的高熵真值（Win11 = `15.0.0`）
    pub ua_platform_version: Option<String>,
    /// `sec-ch-ua-arch` 的高熵真值（桌面 Chrome = `x86`）
    pub ua_arch: Option<String>,
    /// `sec-ch-ua-bitness` 的高熵真值（桌面 Chrome = `64`）
    pub ua_bitness: Option<String>,
    /// `navigator.maxTouchPoints`（桌面 Chrome = 0）
    pub max_touch_points: Option<i64>,
    /// `navigator.userAgentData.mobile`（桌面 Chrome = false）
    pub is_mobile: Option<bool>,
}

/// `_country_defaults()` 的输入（Python 侧来自 `fingerprint._COUNTRY_PROFILES`）。
#[derive(Clone, Debug, Default)]
pub struct CountryDefaults {
    pub timezone: Option<String>,
    pub languages: Vec<String>,
}

impl Profile {
    /// `_profile(**kw)`
    pub fn new(opts: &ProfileOptions, now_ms: f64, rng: &mut Rng) -> Profile {
        let timezone_name = opts
            .timezone
            .clone()
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "Asia/Tokyo".to_string());
        let tz_offset = tz_table(&timezone_name).map(|t| t.offset).unwrap_or(540);

        let screen = opts
            .screen
            .clone()
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "1920x1080".to_string());
        let (screen_width, screen_height) = parse_screen(&screen);

        let mut langs: Vec<String> = match &opts.languages {
            Some(raw) if !raw.is_empty() => raw
                .split(',')
                .map(|x| x.split(';').next().unwrap_or("").trim().to_string())
                .filter(|x| !x.is_empty())
                .collect(),
            _ => {
                let first = opts
                    .lang
                    .clone()
                    .filter(|s| !s.is_empty())
                    .unwrap_or_else(|| "en-US".to_string());
                vec![first, "en".to_string()]
            }
        };
        if langs.is_empty() {
            langs = vec!["en-US".to_string(), "en".to_string()];
        }

        let language = opts
            .lang
            .clone()
            .filter(|s| !s.is_empty())
            .or_else(|| langs.first().cloned())
            .unwrap_or_else(|| "en-US".to_string());

        Profile {
            browser_family: "chrome".to_string(),
            user_agent: opts
                .user_agent
                .clone()
                .filter(|s| !s.is_empty())
                .unwrap_or_else(default_user_agent),
            language: language.clone(),
            languages: langs,
            tz_label: tz_label(&timezone_name, &language),
            timezone_name,
            timezone_offset: tz_offset,
            screen_width,
            screen_height,
            hardware_concurrency: opts.hardware_concurrency.filter(|v| *v != 0).unwrap_or(8),
            platform: opts
                .platform
                .clone()
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| "Win32".to_string()),
            vendor: opts
                .vendor
                .clone()
                .unwrap_or_else(|| "Google Inc.".to_string()),
            device_memory: opts.device_memory.filter(|v| *v != 0).unwrap_or(8),
            js_heap_size_limit: 4_294_967_296,
            device_pixel_ratio: opts.device_pixel_ratio.filter(|v| *v != 0.0).unwrap_or(1.0),
            max_touch_points: opts.max_touch_points.filter(|v| *v >= 0).unwrap_or(0),
            ua_brands: opts.ua_brands.clone().unwrap_or_default(),
            ua_platform_version: opts
                .ua_platform_version
                .clone()
                .filter(|s| !s.is_empty())
                .unwrap_or_default(),
            ua_arch: opts
                .ua_arch
                .clone()
                .filter(|s| !s.is_empty())
                .unwrap_or_default(),
            ua_bitness: opts
                .ua_bitness
                .clone()
                .filter(|s| !s.is_empty())
                .unwrap_or_default(),
            is_mobile: opts.is_mobile.unwrap_or(false),
            script_src: None,
            page_url: String::new(),
            cookie: String::new(),
            page_time_origin_ms: now_ms - rng.uniform(8.0, 40.0),
            time_origin_explicit: false,
        }
    }

    /// `_profile_from_fingerprint(fp)`（无国家默认时区表、时区偏移取自内置表/540）。
    pub fn from_fingerprint(fp: &Value, now_ms: f64, rng: &mut Rng) -> Result<Profile, String> {
        Self::from_fingerprint_full(fp, now_ms, rng, None, None)
    }

    /// `_profile_from_fingerprint(fp)` 完整版：
    /// `country` 对应 `_country_defaults()`，`tz_offset` 覆盖 zoneinfo 求得的偏移
    /// （Python 用 zoneinfo，Rust 无该依赖；夏令时区请显式传入）。
    pub fn from_fingerprint_full(
        fp: &Value,
        now_ms: f64,
        rng: &mut Rng,
        country: Option<&CountryDefaults>,
        tz_offset: Option<i32>,
    ) -> Result<Profile, String> {
        if !fp.is_object() {
            return Err("fingerprint 不是 JSON 对象".to_string());
        }
        let def_tz = country.and_then(|c| c.timezone.clone()).filter(|s| !s.is_empty());
        let def_langs: Vec<String> = country.map(|c| c.languages.clone()).unwrap_or_default();

        let mut language = str_or_empty(fp, "locale");
        if language.is_empty() {
            language = str_or_empty(fp, "lang");
        }

        let mut langs: Vec<String> = match fp.get("languages").and_then(|v| v.as_array()) {
            Some(arr) if !arr.is_empty() => arr.iter().map(value_to_string).collect(),
            _ => Vec::new(),
        };
        if langs.is_empty() {
            let raw = str_or_empty(fp, "lang_full");
            langs = raw
                .split(',')
                .map(|x| x.split(';').next().unwrap_or("").trim().to_string())
                .filter(|x| !x.is_empty())
                .collect();
        }

        let mut tz = str_or_empty(fp, "timezone");
        if tz.is_empty() {
            if let Some(d) = &def_tz {
                tz = d.clone();
            }
        }
        if tz.is_empty() {
            tz = "Asia/Tokyo".to_string();
        }
        if let Some(d) = &def_tz {
            if &tz != d {
                tz = d.clone();
            }
        }

        if !def_langs.is_empty() {
            let filtered: Vec<String> = langs
                .iter()
                .filter(|l| def_langs.contains(l))
                .cloned()
                .collect();
            langs = if filtered.is_empty() {
                def_langs.clone()
            } else {
                filtered
            };
        }
        if langs.is_empty() {
            let first = if language.is_empty() {
                "en-US".to_string()
            } else {
                language.clone()
            };
            langs = vec![first, "en".to_string()];
        }
        if language.is_empty() || (!def_langs.is_empty() && !def_langs.contains(&language)) {
            language = langs[0].clone();
        }
        if langs[0] != language {
            let mut reordered = vec![language.clone()];
            reordered.extend(langs.iter().filter(|l| **l != language).cloned());
            langs = reordered;
        }

        let (parsed_w, parsed_h) = non_empty(fp, "screen")
            .map(|s| parse_screen(&s))
            .unwrap_or((1920, 1080));
        let screen_width = int_field(fp, "screen_width", parsed_w);
        let screen_height = int_field(fp, "screen_height", parsed_h);
        let fp_tz_offset = optional_int_field(fp, "timezone_offset_minutes")
            .or_else(|| optional_int_field(fp, "timezone_offset"))
            .map(|v| v as i32);
        let resolved_offset = tz_offset
            .or(fp_tz_offset)
            .or_else(|| tz_table(&tz).map(|t| t.offset))
            .unwrap_or(540);
        let user_agent = str_or_empty(fp, "user_agent");
        let browser_family = infer_browser_family(fp, &user_agent);
        let is_firefox = browser_family == "firefox";
        let vendor = if is_firefox {
            String::new()
        } else {
            non_empty(fp, "navigator_vendor").unwrap_or_else(|| "Google Inc.".to_string())
        };
        let tz_label_resolved = non_empty(fp, "timezone_name")
            .unwrap_or_else(|| tz_label(&tz, &language));
        let explicit_origin = float_field(fp, "time_origin", 0.0);
        let explicit_origin = if explicit_origin > 0.0 {
            explicit_origin
        } else {
            float_field(fp, "page_time_origin", 0.0)
        };
        let (page_time_origin_ms, time_origin_explicit) = if explicit_origin > 0.0 {
            (explicit_origin, true)
        } else {
            (now_ms - rng.uniform(8.0, 40.0), false)
        };
        let js_heap_size_limit = int_field(fp, "js_heap_size_limit", 4_294_967_296);

        Ok(Profile {
            browser_family,
            user_agent,
            language: language.clone(),
            languages: langs.iter().map(|s| s.to_string()).collect(),
            tz_label: tz_label_resolved,
            timezone_name: tz,
            timezone_offset: resolved_offset,
            screen_width,
            screen_height,
            hardware_concurrency: int_field(fp, "hardware_concurrency", 8),
            platform: non_empty(fp, "navigator_platform").unwrap_or_else(|| "Win32".to_string()),
            vendor,
            device_memory: int_field(fp, "device_memory", 8),
            js_heap_size_limit,
            device_pixel_ratio: float_field(fp, "device_pixel_ratio", 1.0),
            max_touch_points: int_field(fp, "max_touch_points", 0),
            ua_brands: parse_brands(&str_or_empty(fp, "sec_ch_ua")),
            ua_platform_version: non_empty(fp, "sec_ch_ua_platform_version")
                .or_else(|| non_empty(fp, "ua_platform_version"))
                .unwrap_or_default(),
            ua_arch: non_empty(fp, "sec_ch_ua_arch")
                .or_else(|| non_empty(fp, "ua_arch"))
                .unwrap_or_default(),
            ua_bitness: non_empty(fp, "sec_ch_ua_bitness")
                .or_else(|| non_empty(fp, "ua_bitness"))
                .unwrap_or_default(),
            is_mobile: str_or_empty(fp, "sec_ch_ua_mobile") == "?1",
            script_src: non_empty(fp, "script_src"),
            page_url: str_or_empty(fp, "page_url"),
            cookie: str_or_empty(fp, "cookie"),
            page_time_origin_ms,
            time_origin_explicit,
        })
    }
}

/// ⚠️ 这个默认 UA 必须与 Python `sentinel_py._profile()` 的内置默认**逐字一致**
/// （`Chrome/145.0.0.0`），否则 `token_check` 的 `explicit` 用例会出现 PoW attempt 漂移
/// （UA 进了 embedded JSON → 哈希不同 → attempt 不同）。
///
/// 它**不**决定注册时的 UA：真实注册走设备指纹（[`crate::fingerprint::DeviceFingerprint`]），
/// UA 与 TLS 档位同版本（`Chrome/140|145|146|149.0.0.0`）；只有调用方连 UA 都没给时
/// 才会退到这里（Python oracle 的默认档，逐字对齐）。
const DEFAULT_UA: &str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 \
(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36";

fn default_user_agent() -> String {
    DEFAULT_UA.to_string()
}

fn parse_screen(screen: &str) -> (i64, i64) {
    let lowered = screen.to_lowercase();
    let mut parts = lowered.splitn(2, 'x');
    let w = parts.next().and_then(|s| s.trim().parse::<i64>().ok());
    let h = parts.next().and_then(|s| s.trim().parse::<i64>().ok());
    match (w, h) {
        (Some(w), Some(h)) => (w, h),
        _ => (1920, 1080),
    }
}

fn value_to_string(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

/// `str(fp.get(key) or "")`
fn str_or_empty(fp: &Value, key: &str) -> String {
    non_empty(fp, key).unwrap_or_default()
}

/// Python 真值语义下的 `fp.get(key) or None`（空串/null/false/0 都算假）。
fn non_empty(fp: &Value, key: &str) -> Option<String> {
    match fp.get(key) {
        None | Some(Value::Null) => None,
        Some(Value::String(s)) => {
            if s.is_empty() {
                None
            } else {
                Some(s.clone())
            }
        }
        Some(Value::Bool(b)) => {
            if *b {
                Some("true".to_string())
            } else {
                None
            }
        }
        Some(Value::Number(n)) => {
            if n.as_f64() == Some(0.0) {
                None
            } else {
                Some(n.to_string())
            }
        }
        Some(_) => None,
    }
}

/// `int(fp.get(key))` when present (including `0`, unlike `int_field` which treats `0` as missing).
fn optional_int_field(fp: &Value, key: &str) -> Option<i64> {
    match fp.get(key) {
        None | Some(Value::Null) => None,
        Some(Value::Number(n)) => n.as_i64().or_else(|| n.as_f64().map(|f| f as i64)),
        Some(Value::String(s)) => {
            let t = s.trim();
            if t.is_empty() {
                None
            } else {
                t.parse::<i64>().ok()
            }
        }
        _ => None,
    }
}

fn infer_browser_family(fp: &Value, user_agent: &str) -> String {
    if let Some(explicit) = non_empty(fp, "browser_family") {
        let low = explicit.to_ascii_lowercase();
        if low.starts_with("firefox") {
            return "firefox".to_string();
        }
        if low.starts_with("safari") {
            return "safari".to_string();
        }
        if low.starts_with("edge") {
            return "edge".to_string();
        }
        if low.starts_with("chrome") {
            return "chrome".to_string();
        }
    }
    if let Some(imp) = non_empty(fp, "impersonate") {
        let low = imp.to_ascii_lowercase();
        if low.starts_with("firefox") {
            return "firefox".to_string();
        }
        if low.starts_with("safari") {
            return "safari".to_string();
        }
        if low.starts_with("edge") {
            return "edge".to_string();
        }
    }
    if user_agent.contains("Firefox/") {
        return "firefox".to_string();
    }
    if user_agent.contains("Safari/") && !user_agent.contains("Chrome/") && !user_agent.contains("Chromium/") {
        return "safari".to_string();
    }
    "chrome".to_string()
}

/// `int(fp.get(key) or default)`
fn int_field(fp: &Value, key: &str, default: i64) -> i64 {
    match fp.get(key) {
        None | Some(Value::Null) => default,
        Some(Value::Bool(b)) => {
            if *b {
                1
            } else {
                default
            }
        }
        Some(Value::Number(n)) => {
            if let Some(i) = n.as_i64() {
                if i == 0 {
                    default
                } else {
                    i
                }
            } else if let Some(f) = n.as_f64() {
                if f == 0.0 {
                    default
                } else {
                    f as i64
                }
            } else {
                default
            }
        }
        Some(Value::String(s)) => {
            let t = s.trim();
            if t.is_empty() {
                default
            } else {
                t.parse::<i64>().unwrap_or(default)
            }
        }
        Some(_) => default,
    }
}

/// `float(fp.get(key) or default)`
fn float_field(fp: &Value, key: &str, default: f64) -> f64 {
    match fp.get(key) {
        None | Some(Value::Null) => default,
        Some(Value::Bool(b)) => {
            if *b {
                1.0
            } else {
                default
            }
        }
        Some(Value::Number(n)) => match n.as_f64() {
            Some(f) if f != 0.0 => f,
            _ => default,
        },
        Some(Value::String(s)) => {
            let t = s.trim();
            if t.is_empty() {
                default
            } else {
                t.parse::<f64>().unwrap_or(default)
            }
        }
        Some(_) => default,
    }
}

/// `_parse_brands(sec_ch_ua)`：抽出所有 `"brand";v="version"`。
pub fn parse_brands(sec_ch_ua: &str) -> Vec<Brand> {
    let mut out = Vec::new();
    let mut rest = sec_ch_ua;
    while let Some(q1) = rest.find('"') {
        let after = &rest[q1 + 1..];
        let q2 = match after.find('"') {
            Some(v) => v,
            None => break,
        };
        let brand = &after[..q2];
        let tail = &after[q2 + 1..];
        if let Some(stripped) = tail.strip_prefix(";v=\"") {
            match stripped.find('"') {
                Some(q3) => {
                    out.push(Brand {
                        brand: brand.to_string(),
                        version: stripped[..q3].to_string(),
                    });
                    rest = &stripped[q3 + 1..];
                }
                None => break,
            }
        } else {
            rest = tail;
        }
    }
    out
}

// ---------------------------------------------------------------- Config

/// `config` 数组里的一个元素（Python list 里的 int/float/str/None）。
#[derive(Clone, Debug, PartialEq)]
pub enum CfgVal {
    Null,
    Int(i64),
    Float(f64),
    Str(String),
}

/// sdk.js `getConfig()` 的 25 字段数组。
#[derive(Clone, Debug, PartialEq)]
pub struct Config {
    pub fields: Vec<CfgVal>,
}

impl Config {
    pub fn new(fields: Vec<CfgVal>) -> Config {
        Config { fields }
    }

    pub fn len(&self) -> usize {
        self.fields.len()
    }

    pub fn is_empty(&self) -> bool {
        self.fields.is_empty()
    }

    /// 读取整数字段（`_pow` 会写 `config[3]` / `config[9]`）。
    pub fn int_at(&self, idx: usize) -> Option<i64> {
        match self.fields.get(idx) {
            Some(CfgVal::Int(v)) => Some(*v),
            _ => None,
        }
    }

    /// 写整数字段（对应 Python `config[3] = attempt`）。
    pub fn set_int(&mut self, idx: usize, value: i64) {
        if idx < self.fields.len() {
            self.fields[idx] = CfgVal::Int(value);
        }
    }

    /// 单个字段的 JSON 文本（用于逐字段比对）。
    pub fn field_json(&self, idx: usize) -> String {
        match self.fields.get(idx) {
            None => String::from("<missing>"),
            Some(CfgVal::Null) => String::from("null"),
            Some(CfgVal::Int(v)) => v.to_string(),
            Some(CfgVal::Float(f)) => py_float_repr(*f),
            Some(CfgVal::Str(s)) => json_string(s),
        }
    }

    pub fn fields_json(&self) -> Vec<String> {
        (0..self.fields.len()).map(|i| self.field_json(i)).collect()
    }

    /// `json.dumps(config, ensure_ascii=False, separators=(",", ":"))`
    pub fn to_json_string(&self) -> String {
        let mut out = String::with_capacity(320);
        out.push('[');
        for (i, _) in self.fields.iter().enumerate() {
            if i > 0 {
                out.push(',');
            }
            out.push_str(&self.field_json(i));
        }
        out.push(']');
        out
    }
}

fn nav_probe_value(key: &str, profile: &Profile) -> String {
    match key {
        "doNotTrack" => "null".to_string(),
        "language" => profile.language.clone(),
        "platform" => profile.platform.clone(),
        "vendor" => profile.vendor.clone(),
        "webdriver" => "false".to_string(),
        "hardwareConcurrency" => profile.hardware_concurrency.to_string(),
        _ => "undefined".to_string(),
    }
}

/// `build_config(profile, sid, page_age_ms)`（`page_age_ms` 在 Python 侧未被使用，已省略）。
///
/// 随机消耗顺序必须与 Python 完全一致：
/// `choice(nav)` → `random()`(p3) → `random()`(p9) → `choice(doc)` → `choice(win)`。
pub fn build_config(profile: &Profile, sid: &str, now_ms: f64, rng: &mut Rng) -> Config {
    let time_origin = profile.page_time_origin_ms;
    let perf_now = (now_ms - time_origin).max(0.0);

    let nav_key = NAV_KEYS[rng.choice_index(NAV_KEYS.len())];
    let nav_val = nav_probe_value(nav_key, profile);
    let v3 = py_round_17(rng.next_f64());
    let v9 = py_round_17(rng.next_f64());
    let doc = DOC_KEYS[rng.choice_index(DOC_KEYS.len())];
    let win = WIN_KEYS[rng.choice_index(WIN_KEYS.len())];
    let family = profile.browser_family.as_str();
    let is_chromium = family != "firefox" && family != "safari";
    let is_firefox = family == "firefox";
    let heap_val = if is_chromium {
        CfgVal::Int(profile.js_heap_size_limit)
    } else {
        CfgVal::Null
    };
    let script_val = match &profile.script_src {
        Some(src) if !src.is_empty() => CfgVal::Str(src.clone()),
        _ => CfgVal::Null,
    };
    let elapsed_slot = if profile.time_origin_explicit {
        CfgVal::Int(py_round_int(perf_now.max(1000.0)))
    } else {
        CfgVal::Int(py_round_int(perf_now.min(40.0)))
    };

    let fields = vec![
        CfgVal::Int(profile.screen_width + profile.screen_height),
        CfgVal::Str(js_date(profile, now_ms)),
        heap_val,
        CfgVal::Float(v3),
        CfgVal::Str(profile.user_agent.clone()),
        script_val,
        CfgVal::Null,
        CfgVal::Str(profile.language.clone()),
        CfgVal::Str(profile.languages.join(",")),
        CfgVal::Float(v9),
        CfgVal::Str(format!("{}\u{2212}{}", nav_key, nav_val)),
        CfgVal::Str(doc.to_string()),
        CfgVal::Str(win.to_string()),
        elapsed_slot,
        CfgVal::Str(sid.to_string()),
        CfgVal::Str(String::new()),
        CfgVal::Int(profile.hardware_concurrency),
        CfgVal::Int(time_origin as i64),
        CfgVal::Int(0),
        CfgVal::Int(0),
        CfgVal::Int(0),
        CfgVal::Int(0),
        CfgVal::Int(0),
        CfgVal::Int(0),
        CfgVal::Int(if is_firefox { 1 } else { 0 }),
    ];
    Config::new(fields)
}

// ------------------------------------------------------------------- PoW

/// `_pow(seed, difficulty, config)`：返回命中的 `base64(JSON)` 串。
///
/// 每轮写入 `config[3] = attempt`、`config[9] = round(elapsed_ms)`，编码成
/// `_b64(json.dumps(config))` 后用 `fnv1a(seed + encoded)[:len(difficulty)] <= difficulty`
/// 判定（字符串字典序），命中则返回这个 base64 串（**不含** `gAAAAA*` 前缀与 `~S` 后缀）。
pub fn pow(
    seed: &str,
    difficulty: &str,
    config: &mut Config,
    clock: &Clock,
    max_attempts: u32,
) -> Result<String, String> {
    if difficulty.is_empty() {
        return Err("difficulty 为空".to_string());
    }
    let diff_len = difficulty.len();
    let started = clock.now_ms();
    for attempt in 0..max_attempts {
        config.set_int(3, attempt as i64);
        config.set_int(9, py_round_int(clock.now_ms() - started));
        let encoded = b64(&config.to_json_string());
        let mut combined = String::with_capacity(seed.len() + encoded.len());
        combined.push_str(seed);
        combined.push_str(&encoded);
        let hash = fnv1a(&combined);
        let cut = diff_len.min(hash.len());
        if hash[..cut] <= *difficulty {
            return Ok(encoded);
        }
    }
    Err(format!("PoW exhausted ({} 次未命中)", max_attempts))
}

// ------------------------------------------------------------ token 入口

/// `requirements_token(profile, sid)` → `"gAAAAAC" + b64(json) + "~S"`。
///
/// Python 内部 seed 是 `str(uuid.uuid4())`；为保持确定性，由调用方传入 `seed`，
/// 本函数不引入任何额外随机。
pub fn requirements_token(
    profile: &Profile,
    sid: &str,
    seed: &str,
    ctx: &mut TokenCtx,
) -> Result<String, String> {
    let mut config = build_config(profile, sid, ctx.now_ms, &mut ctx.rng);
    let encoded = pow(seed, "0", &mut config, &ctx.clock, ctx.max_attempts)?;
    Ok(format!("gAAAAAC{}~S", encoded))
}

/// `enforcement_token(challenge, profile, sid)` → `"gAAAAAB" + b64(json) + "~S"`。
///
/// 返回 `Ok(None)` 表示 Python 侧同样返回 `None` 的情形
/// （`proofofwork.required` 为假，或 seed / difficulty 为空）。
pub fn enforcement_token(
    challenge: &Value,
    profile: &Profile,
    sid: &str,
    ctx: &mut TokenCtx,
) -> Result<Option<String>, String> {
    let pow_data = challenge.get("proofofwork").cloned().unwrap_or(Value::Null);
    let required = pow_data
        .get("required")
        .map(|v| match v {
            Value::Bool(b) => *b,
            Value::Null => false,
            Value::Number(n) => n.as_f64().map(|f| f != 0.0).unwrap_or(false),
            Value::String(s) => !s.is_empty(),
            _ => true,
        })
        .unwrap_or(false);
    if !required {
        return Ok(None);
    }

    let seed = challenge_string(&pow_data, "seed");
    let difficulty = challenge_string(&pow_data, "difficulty");
    if seed.is_empty() || difficulty.is_empty() {
        return Ok(None);
    }

    let mut config = build_config(profile, sid, ctx.now_ms, &mut ctx.rng);
    let encoded = pow(&seed, &difficulty, &mut config, &ctx.clock, ctx.max_attempts)?;
    Ok(Some(format!("gAAAAAB{}~S", encoded)))
}

/// `str(pow_data.get(key) or "")`
fn challenge_string(pow_data: &Value, key: &str) -> String {
    match pow_data.get(key) {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Bool(b)) => b.to_string(),
        Some(Value::Number(n)) => n.to_string(),
        Some(_) => String::new(),
    }
}

// ------------------------------------------------------------------ tests

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fnv1a_known_values() {
        // 由 Python oracle `_fnv1a` 得到
        assert_eq!(fnv1a(""), "ab3e7c0b");
        assert_eq!(fnv1a("abc"), "1cc93dbc");
        assert_eq!(fnv1a("日本標準時シンガポール日本語"), "4f5bfb2c");
    }

    #[test]
    fn float_repr_matches_cpython() {
        assert_eq!(py_float_repr(0.5), "0.5");
        assert_eq!(py_float_repr(1e16), "1e+16");
        assert_eq!(py_float_repr(1e15), "1000000000000000.0");
        assert_eq!(py_float_repr(1e-5), "1e-05");
        assert_eq!(py_float_repr(0.0001), "0.0001");
        assert_eq!(py_float_repr(-0.0), "-0.0");
        assert_eq!(py_float_repr(100.0), "100.0");
        assert_eq!(py_float_repr(1.0 / 3.0), "0.3333333333333333");
    }

    #[test]
    fn config_has_25_fields() {
        let mut rng = Rng::constant(0.5);
        let profile = Profile::new(&ProfileOptions::default(), 1_793_036_746_210.0, &mut rng);
        let cfg = build_config(&profile, "sid", 1_793_036_746_210.0, &mut rng);
        assert_eq!(cfg.len(), 25);
    }
}
