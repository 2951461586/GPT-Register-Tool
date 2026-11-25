//! Python bindings for `sms_tool/proxy/entry.py`: generated from the signatures in `port.rs` by rust-anything.
//! Regenerate with `rust-anything refresh` after changing them.

#![allow(clippy::too_many_arguments)]

use pyo3::prelude::*;

pub mod port;

fn error(message: String) -> PyErr {
    use pyo3::exceptions as e;
    /// `Err("Kind: text")` for a `Kind` the match below does not know: a built-in exception's name or
    /// `package.module.Class`, called with the text. None when it names no `Exception` subclass or the
    /// class cannot be made from the text alone (its constructor takes more arguments).
    fn named_error(kind: &str, text: &str) -> Option<PyErr> {
        Python::attach(|py| {
            // Each dotted part must be a Python identifier, by Python's own rules (`str.isidentifier`).
            let identifier = |part: &str| {
                pyo3::types::PyString::new(py, part)
                    .call_method0("isidentifier")
                    .and_then(|ok| ok.extract::<bool>())
                    .unwrap_or(false)
            };
            if !kind.split('.').all(identifier) {
                return None;
            }
            let (module, name) = kind.rsplit_once('.').unwrap_or(("builtins", kind));
            let class = py.import(module).ok()?.getattr(name).ok()?;
            let class = class.cast_into::<pyo3::types::PyType>().ok()?;
            if !class
                .is_subclass_of::<pyo3::exceptions::PyException>()
                .ok()?
            {
                return None;
            }
            let value = class.call1((text,)).ok()?;
            if !value.is_instance(&class).ok()? {
                return None;
            }
            Some(PyErr::from_value(value))
        })
    }
    let (kind, text) = message.split_once(": ").unwrap_or(("", message.as_str()));
    let text = text.to_string();
    match kind {
        "TypeError" => e::PyTypeError::new_err(text),
        "KeyError" => e::PyKeyError::new_err(text),
        "IndexError" => e::PyIndexError::new_err(text),
        "OverflowError" => e::PyOverflowError::new_err(text),
        "ZeroDivisionError" => e::PyZeroDivisionError::new_err(text),
        "AttributeError" => e::PyAttributeError::new_err(text),
        "RuntimeError" => e::PyRuntimeError::new_err(text),
        "NotImplementedError" => e::PyNotImplementedError::new_err(text),
        "LookupError" => e::PyLookupError::new_err(text),
        "ArithmeticError" => e::PyArithmeticError::new_err(text),
        "UnicodeError" => e::PyUnicodeError::new_err(text),
        "ValueError" => e::PyValueError::new_err(text),
        _ => named_error(kind, &text).unwrap_or_else(|| e::PyValueError::new_err(message)),
    }
}

fn normalize_proxy_url_inner(
    proxy: Option<String>,
    default_scheme: String,
) -> Result<String, String> {
    let out = port::normalize_proxy_url(proxy, default_scheme)?;
    Ok(out)
}

/// `normalize_proxy_url`.
#[pyfunction]
#[pyo3(name = "normalize_proxy_url", signature = (proxy, default_scheme=String::from("http")))]
fn normalize_proxy_url(proxy: Option<String>, default_scheme: String) -> PyResult<String> {
    normalize_proxy_url_inner(proxy, default_scheme).map_err(error)
}

fn redact_proxy_url_inner(
    proxy: Option<String>,
    empty_placeholder: String,
) -> Result<String, String> {
    let out = port::redact_proxy_url(proxy, empty_placeholder)?;
    Ok(out)
}

/// `redact_proxy_url`.
#[pyfunction]
#[pyo3(name = "redact_proxy_url", signature = (proxy, empty_placeholder=String::from("DIRECT")))]
fn redact_proxy_url(proxy: Option<String>, empty_placeholder: String) -> PyResult<String> {
    redact_proxy_url_inner(proxy, empty_placeholder).map_err(error)
}

fn strip_session_id_inner(proxy: String) -> Result<String, String> {
    let out = port::strip_session_id(proxy)?;
    Ok(out)
}

/// `strip_session_id`.
#[pyfunction]
#[pyo3(name = "strip_session_id", signature = (proxy))]
fn strip_session_id(proxy: String) -> PyResult<String> {
    strip_session_id_inner(proxy).map_err(error)
}

fn infer_proxy_country_inner(proxy: String) -> Result<String, String> {
    let out = port::infer_proxy_country(proxy)?;
    Ok(out)
}

/// `infer_proxy_country`.
#[pyfunction]
#[pyo3(name = "infer_proxy_country", signature = (proxy))]
fn infer_proxy_country(proxy: String) -> PyResult<String> {
    infer_proxy_country_inner(proxy).map_err(error)
}

fn _mask_credentials_inner(value: String) -> Result<String, String> {
    let out = port::_mask_credentials(value)?;
    Ok(out)
}

/// `_mask_credentials`.
#[pyfunction]
#[pyo3(name = "_mask_credentials", signature = (value))]
fn _mask_credentials(value: String) -> PyResult<String> {
    _mask_credentials_inner(value).map_err(error)
}

fn redact_proxy_text_inner(value: Option<String>, proxy: Option<String>) -> Result<String, String> {
    let out = port::redact_proxy_text(value, proxy)?;
    Ok(out)
}

/// `redact_proxy_text`.
#[pyfunction]
#[pyo3(name = "redact_proxy_text", signature = (value, proxy=None))]
fn redact_proxy_text(value: Option<String>, proxy: Option<String>) -> PyResult<String> {
    redact_proxy_text_inner(value, proxy).map_err(error)
}

fn retarget_region_inner(proxy: String, iso_code: String) -> Result<String, String> {
    let out = port::retarget_region(proxy, iso_code)?;
    Ok(out)
}

/// `retarget_region`.
#[pyfunction]
#[pyo3(name = "retarget_region", signature = (proxy, iso_code))]
fn retarget_region(proxy: String, iso_code: String) -> PyResult<String> {
    retarget_region_inner(proxy, iso_code).map_err(error)
}

fn infer_region_inner(proxy: String) -> Result<String, String> {
    let out = port::infer_region(proxy)?;
    Ok(out)
}

/// `infer_region`.
#[pyfunction]
#[pyo3(name = "infer_region", signature = (proxy))]
fn infer_region(proxy: String) -> PyResult<String> {
    infer_region_inner(proxy).map_err(error)
}

#[pymodule]
#[pyo3(name = "_native")]
fn native_module(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(normalize_proxy_url, m)?)?;
    m.add_function(wrap_pyfunction!(redact_proxy_url, m)?)?;
    m.add_function(wrap_pyfunction!(strip_session_id, m)?)?;
    m.add_function(wrap_pyfunction!(infer_proxy_country, m)?)?;
    m.add_function(wrap_pyfunction!(_mask_credentials, m)?)?;
    m.add_function(wrap_pyfunction!(redact_proxy_text, m)?)?;
    m.add_function(wrap_pyfunction!(retarget_region, m)?)?;
    m.add_function(wrap_pyfunction!(infer_region, m)?)?;
    Ok(())
}
