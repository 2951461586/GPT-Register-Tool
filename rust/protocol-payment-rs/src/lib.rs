//! Python bindings for `sms_tool/payment/protocol_codec.py`: generated from the signatures in `port.rs` by rust-anything.
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

fn encode_rv_timestamp_inner(
    rv_ts: Bound<'_, PyAny>,
    rv: Bound<'_, PyAny>,
    sv: Bound<'_, PyAny>,
) -> Result<String, String> {
    let rv_ts: serde_json::Value = crate::pyconv::FromPy::from_py(&rv_ts)?;
    let rv: serde_json::Value = crate::pyconv::FromPy::from_py(&rv)?;
    let sv: serde_json::Value = crate::pyconv::FromPy::from_py(&sv)?;
    let out = port::encode_rv_timestamp(rv_ts, rv, sv)?;
    Ok(out)
}

/// `encode_rv_timestamp`.
#[pyfunction]
#[pyo3(name = "encode_rv_timestamp", signature = (rv_ts, rv, sv))]
fn encode_rv_timestamp(
    rv_ts: Bound<'_, PyAny>,
    rv: Bound<'_, PyAny>,
    sv: Bound<'_, PyAny>,
) -> PyResult<String> {
    encode_rv_timestamp_inner(rv_ts, rv, sv).map_err(error)
}

fn _decode_rv_timestamp_dict_inner(
    value: Bound<'_, PyAny>,
) -> Result<Option<serde_json::Value>, String> {
    let value: serde_json::Value = crate::pyconv::FromPy::from_py(&value)?;
    let out = port::_decode_rv_timestamp_dict(value)?;
    Ok(out)
}

/// `_decode_rv_timestamp_dict`.
#[pyfunction]
#[pyo3(name = "_decode_rv_timestamp_dict", signature = (value))]
fn _decode_rv_timestamp_dict(py: Python<'_>, value: Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    _decode_rv_timestamp_dict_inner(value)
        .map_err(error)
        .and_then(|v| crate::pyconv::ToPy::to_py(&v, py))
}

fn _extract_rv_build_fields_dict_inner(
    text: Bound<'_, PyAny>,
) -> Result<Option<serde_json::Value>, String> {
    let text: serde_json::Value = crate::pyconv::FromPy::from_py(&text)?;
    let out = port::_extract_rv_build_fields_dict(text)?;
    Ok(out)
}

/// `_extract_rv_build_fields_dict`.
#[pyfunction]
#[pyo3(name = "_extract_rv_build_fields_dict", signature = (text))]
fn _extract_rv_build_fields_dict(py: Python<'_>, text: Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    _extract_rv_build_fields_dict_inner(text)
        .map_err(error)
        .and_then(|v| crate::pyconv::ToPy::to_py(&v, py))
}

fn rv_timestamp_from_script_inner(text: Bound<'_, PyAny>) -> Result<Option<String>, String> {
    let text: serde_json::Value = crate::pyconv::FromPy::from_py(&text)?;
    let out = port::rv_timestamp_from_script(text)?;
    Ok(out)
}

/// `rv_timestamp_from_script`.
#[pyfunction]
#[pyo3(name = "rv_timestamp_from_script", signature = (text))]
fn rv_timestamp_from_script(text: Bound<'_, PyAny>) -> PyResult<Option<String>> {
    rv_timestamp_from_script_inner(text).map_err(error)
}

fn _quote_from_payload_str_inner(
    payload: Bound<'_, PyAny>,
    default_currency: Option<Bound<'_, PyAny>>,
) -> Result<(Option<String>, String), String> {
    let payload: serde_json::Value = crate::pyconv::FromPy::from_py(&payload)?;
    let default_currency: Option<serde_json::Value> = match &default_currency {
        None => None,
        Some(v) => Some(crate::pyconv::FromPy::from_py(v)?),
    };
    let out = port::_quote_from_payload_str(payload, default_currency)?;
    Ok(out)
}

/// `_quote_from_payload_str`.
#[pyfunction]
#[pyo3(name = "_quote_from_payload_str", signature = (payload, default_currency=None))]
fn _quote_from_payload_str(
    py: Python<'_>,
    payload: Bound<'_, PyAny>,
    default_currency: Option<Bound<'_, PyAny>>,
) -> PyResult<Py<PyAny>> {
    _quote_from_payload_str_inner(payload, default_currency)
        .map_err(error)
        .and_then(|v| crate::pyconv::ToPy::to_py(&v, py))
}

#[pymodule]
#[pyo3(name = "_native")]
fn native_module(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(encode_rv_timestamp, m)?)?;
    m.add_function(wrap_pyfunction!(_decode_rv_timestamp_dict, m)?)?;
    m.add_function(wrap_pyfunction!(_extract_rv_build_fields_dict, m)?)?;
    m.add_function(wrap_pyfunction!(rv_timestamp_from_script, m)?)?;
    m.add_function(wrap_pyfunction!(_quote_from_payload_str, m)?)?;
    Ok(())
}
