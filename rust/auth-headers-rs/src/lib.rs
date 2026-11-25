//! Python bindings for `sms_tool/transport/auth_headers.py`: generated from the signatures in `port.rs` by rust-anything.
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

fn _browser_version_inner(impersonate: String) -> Result<String, String> {
    let out = port::_browser_version(impersonate)?;
    Ok(out)
}

/// `_browser_version`.
#[pyfunction]
#[pyo3(name = "_browser_version", signature = (impersonate))]
fn _browser_version(impersonate: String) -> PyResult<String> {
    _browser_version_inner(impersonate).map_err(error)
}

fn _full_version_list_inner(sec_ch_ua: String, full_version: String) -> Result<String, String> {
    let out = port::_full_version_list(sec_ch_ua, full_version)?;
    Ok(out)
}

/// `_full_version_list`.
#[pyfunction]
#[pyo3(name = "_full_version_list", signature = (sec_ch_ua, full_version=String::from("")))]
fn _full_version_list(sec_ch_ua: String, full_version: String) -> PyResult<String> {
    _full_version_list_inner(sec_ch_ua, full_version).map_err(error)
}

fn accept_encoding_for_inner(impersonate: String) -> Result<String, String> {
    let out = port::accept_encoding_for(impersonate)?;
    Ok(out)
}

/// `accept_encoding_for`.
#[pyfunction]
#[pyo3(name = "accept_encoding_for", signature = (impersonate))]
fn accept_encoding_for(impersonate: String) -> PyResult<String> {
    accept_encoding_for_inner(impersonate).map_err(error)
}

fn origin_from_referer_inner(referer: String) -> Result<String, String> {
    let out = port::origin_from_referer(referer)?;
    Ok(out)
}

/// `origin_from_referer`.
#[pyfunction]
#[pyo3(name = "origin_from_referer", signature = (referer=String::from("")))]
fn origin_from_referer(referer: String) -> PyResult<String> {
    origin_from_referer_inner(referer).map_err(error)
}

#[pymodule]
#[pyo3(name = "_native")]
fn native_module(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(_browser_version, m)?)?;
    m.add_function(wrap_pyfunction!(_full_version_list, m)?)?;
    m.add_function(wrap_pyfunction!(accept_encoding_for, m)?)?;
    m.add_function(wrap_pyfunction!(origin_from_referer, m)?)?;
    Ok(())
}
