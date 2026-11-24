//! Python bindings for `sms_tool/mailbox/mail_otp.py`: generated from the signatures in `port.rs` by rust-anything.
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

fn _extract_otp_from_text_inner(text: Bound<'_, PyAny>) -> Result<String, String> {
    let text: serde_json::Value = crate::pyconv::FromPy::from_py(&text)?;
    let out = port::_extract_otp_from_text(text)?;
    Ok(out)
}

/// `_extract_otp_from_text`.
#[pyfunction]
#[pyo3(name = "_extract_otp_from_text", signature = (text))]
fn _extract_otp_from_text(text: Bound<'_, PyAny>) -> PyResult<String> {
    _extract_otp_from_text_inner(text).map_err(error)
}

fn _looks_fake_otp_context_inner(lowered_text: Bound<'_, PyAny>) -> Result<bool, String> {
    let lowered_text: serde_json::Value = crate::pyconv::FromPy::from_py(&lowered_text)?;
    let out = port::_looks_fake_otp_context(lowered_text)?;
    Ok(out)
}

/// `_looks_fake_otp_context`.
#[pyfunction]
#[pyo3(name = "_looks_fake_otp_context", signature = (lowered_text))]
fn _looks_fake_otp_context(lowered_text: Bound<'_, PyAny>) -> PyResult<bool> {
    _looks_fake_otp_context_inner(lowered_text).map_err(error)
}

fn _message_looks_fake_otp_noise_inner(text: Bound<'_, PyAny>) -> Result<bool, String> {
    let text: serde_json::Value = crate::pyconv::FromPy::from_py(&text)?;
    let out = port::_message_looks_fake_otp_noise(text)?;
    Ok(out)
}

/// `_message_looks_fake_otp_noise`.
#[pyfunction]
#[pyo3(name = "_message_looks_fake_otp_noise", signature = (text))]
fn _message_looks_fake_otp_noise(text: Bound<'_, PyAny>) -> PyResult<bool> {
    _message_looks_fake_otp_noise_inner(text).map_err(error)
}

#[pymodule]
#[pyo3(name = "_native")]
fn native_module(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(_extract_otp_from_text, m)?)?;
    m.add_function(wrap_pyfunction!(_looks_fake_otp_context, m)?)?;
    m.add_function(wrap_pyfunction!(_message_looks_fake_otp_noise, m)?)?;
    Ok(())
}
