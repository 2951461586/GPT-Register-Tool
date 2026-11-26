//! Python bindings for `sms_tool/providers/mailbox_icloud_url.py`: generated from the signatures in `port.rs` by rust-anything.
//! Regenerate with `rust-anything refresh` after changing them.

#![allow(clippy::too_many_arguments)]

use pyo3::prelude::*;

pub mod js;
pub mod port;
pub mod pyconv;
pub mod pynum;

#[allow(unused_imports)]
use pynum::Num;

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

fn _strip_html_to_visible_text_inner(raw_html: Bound<'_, PyAny>) -> Result<String, String> {
    let raw_html: serde_json::Value = crate::pyconv::FromPy::from_py(&raw_html)?;
    let out = port::_strip_html_to_visible_text(raw_html)?;
    Ok(out)
}

/// `_strip_html_to_visible_text`.
#[pyfunction]
#[pyo3(name = "_strip_html_to_visible_text", signature = (raw_html))]
fn _strip_html_to_visible_text(raw_html: Bound<'_, PyAny>) -> PyResult<String> {
    _strip_html_to_visible_text_inner(raw_html).map_err(error)
}

fn _latest_mail_received_at_inner(value: Bound<'_, PyAny>) -> Result<String, String> {
    let value: serde_json::Value = crate::pyconv::FromPy::from_py(&value)?;
    let out = port::_latest_mail_received_at(value)?;
    Ok(out)
}

/// `_latest_mail_received_at`.
#[pyfunction]
#[pyo3(name = "_latest_mail_received_at", signature = (value))]
fn _latest_mail_received_at(value: Bound<'_, PyAny>) -> PyResult<String> {
    _latest_mail_received_at_inner(value).map_err(error)
}

fn _decode_js_string_inner(raw: String) -> Result<String, String> {
    let out = port::_decode_js_string(raw)?;
    Ok(out)
}

/// `_decode_js_string`.
#[pyfunction]
#[pyo3(name = "_decode_js_string", signature = (raw))]
fn _decode_js_string(raw: String) -> PyResult<String> {
    _decode_js_string_inner(raw).map_err(error)
}

fn _with_message_limit_inner(url: String, limit: Bound<'_, PyAny>) -> Result<String, String> {
    let limit: serde_json::Value = crate::pyconv::FromPy::from_py(&limit)?;
    let out = port::_with_message_limit(url, limit)?;
    Ok(out)
}

/// `_with_message_limit`.
#[pyfunction]
#[pyo3(name = "_with_message_limit", signature = (url, limit))]
fn _with_message_limit(url: String, limit: Bound<'_, PyAny>) -> PyResult<String> {
    _with_message_limit_inner(url, limit).map_err(error)
}

fn _same_origin_inner(base_url: String, candidate_url: String) -> Result<bool, String> {
    let out = port::_same_origin(base_url, candidate_url)?;
    Ok(out)
}

/// `_same_origin`.
#[pyfunction]
#[pyo3(name = "_same_origin", signature = (base_url, candidate_url))]
fn _same_origin(base_url: String, candidate_url: String) -> PyResult<bool> {
    _same_origin_inner(base_url, candidate_url).map_err(error)
}

fn _extract_url_credentials_inner(
    url: String,
    fallback_email: String,
) -> Result<(String, String, bool), String> {
    let out = port::_extract_url_credentials(url, fallback_email)?;
    Ok(out)
}

/// `_extract_url_credentials`.
#[pyfunction]
#[pyo3(name = "_extract_url_credentials", signature = (url, fallback_email))]
fn _extract_url_credentials(
    py: Python<'_>,
    url: String,
    fallback_email: String,
) -> PyResult<Py<PyAny>> {
    _extract_url_credentials_inner(url, fallback_email)
        .map_err(error)
        .and_then(|v| crate::pyconv::ToPy::to_py(&v, py))
}

#[pymodule]
#[pyo3(name = "_native")]
fn native_module(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(_strip_html_to_visible_text, m)?)?;
    m.add_function(wrap_pyfunction!(_latest_mail_received_at, m)?)?;
    m.add_function(wrap_pyfunction!(_decode_js_string, m)?)?;
    m.add_function(wrap_pyfunction!(_with_message_limit, m)?)?;
    m.add_function(wrap_pyfunction!(_same_origin, m)?)?;
    m.add_function(wrap_pyfunction!(_extract_url_credentials, m)?)?;
    Ok(())
}
