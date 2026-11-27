//! Python bindings for `sms_tool/registration_drivers/browser_flow/dom_fields.py`: generated from the signatures in `port.rs` by rust-anything.
//! Regenerate with `rust-anything refresh` after changing them.

#![allow(clippy::too_many_arguments)]

use pyo3::prelude::*;

pub mod js;
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

fn _is_openai_auth_url_inner(url: Option<String>) -> Result<bool, String> {
    let out = port::_is_openai_auth_url(url)?;
    Ok(out)
}

/// `_is_openai_auth_url`.
#[pyfunction]
#[pyo3(name = "_is_openai_auth_url", signature = (url))]
fn _is_openai_auth_url(url: Option<String>) -> PyResult<bool> {
    _is_openai_auth_url_inner(url).map_err(error)
}

fn _unexpected_identity_provider_inner(url: Option<String>) -> Result<bool, String> {
    let out = port::_unexpected_identity_provider(url)?;
    Ok(out)
}

/// `_unexpected_identity_provider`.
#[pyfunction]
#[pyo3(name = "_unexpected_identity_provider", signature = (url))]
fn _unexpected_identity_provider(url: Option<String>) -> PyResult<bool> {
    _unexpected_identity_provider_inner(url).map_err(error)
}

fn _session_context_closed_inner(value: Option<String>) -> Result<bool, String> {
    let out = port::_session_context_closed(value)?;
    Ok(out)
}

/// `_session_context_closed`.
#[pyfunction]
#[pyo3(name = "_session_context_closed", signature = (value))]
fn _session_context_closed(value: Option<String>) -> PyResult<bool> {
    _session_context_closed_inner(value).map_err(error)
}

fn _terminal_session_error_inner(
    status: f64,
    error_marker: Option<String>,
) -> Result<String, String> {
    let out = port::_terminal_session_error(status, error_marker)?;
    Ok(out)
}

/// `_terminal_session_error`.
#[pyfunction]
#[pyo3(name = "_terminal_session_error", signature = (status, error_marker))]
fn _terminal_session_error(status: f64, error_marker: Option<String>) -> PyResult<String> {
    _terminal_session_error_inner(status, error_marker).map_err(error)
}

#[pymodule]
#[pyo3(name = "_native")]
fn native_module(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(_is_openai_auth_url, m)?)?;
    m.add_function(wrap_pyfunction!(_unexpected_identity_provider, m)?)?;
    m.add_function(wrap_pyfunction!(_session_context_closed, m)?)?;
    m.add_function(wrap_pyfunction!(_terminal_session_error, m)?)?;
    Ok(())
}
