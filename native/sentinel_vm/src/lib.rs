//! Pure-Rust Sentinel VM & FFI entry point (C ABI cdylib for Python ctypes).

pub mod env;
pub mod mint;
pub mod so;
pub mod token;
pub mod value;
pub mod vm;

mod ffi;

pub use ffi::{
    srs_free, srs_mint_so, srs_mint_t, srs_probe_env, srs_requirements_token,
    srs_solve_challenge, srs_version,
};
