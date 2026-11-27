//! Python bindings for `sms_tool/paypal/reverse_protocol.py`: generated from the signatures in `port.rs` by rust-anything.
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

fn extract_hidden_fields_inner(form_html: String) -> Result<serde_json::Value, String> {
    let out = port::extract_hidden_fields(form_html)?;
    Ok(out)
}

/// `extract_hidden_fields`.
#[pyfunction]
#[pyo3(name = "extract_hidden_fields", signature = (form_html))]
fn extract_hidden_fields(py: Python<'_>, form_html: String) -> PyResult<Py<PyAny>> {
    extract_hidden_fields_inner(form_html)
        .map_err(error)
        .and_then(|v| crate::pyconv::ToPy::to_py(&v, py))
}

fn extract_input_fields_inner(form_html: String) -> Result<serde_json::Value, String> {
    let out = port::extract_input_fields(form_html)?;
    Ok(out)
}

/// `extract_input_fields`.
#[pyfunction]
#[pyo3(name = "extract_input_fields", signature = (form_html))]
fn extract_input_fields(py: Python<'_>, form_html: String) -> PyResult<Py<PyAny>> {
    extract_input_fields_inner(form_html)
        .map_err(error)
        .and_then(|v| crate::pyconv::ToPy::to_py(&v, py))
}

fn extract_select_fields_inner(form_html: String) -> Result<serde_json::Value, String> {
    let out = port::extract_select_fields(form_html)?;
    Ok(out)
}

/// `extract_select_fields`.
#[pyfunction]
#[pyo3(name = "extract_select_fields", signature = (form_html))]
fn extract_select_fields(py: Python<'_>, form_html: String) -> PyResult<Py<PyAny>> {
    extract_select_fields_inner(form_html)
        .map_err(error)
        .and_then(|v| crate::pyconv::ToPy::to_py(&v, py))
}

fn find_signup_form_inner(
    html: String,
    current_url: String,
) -> Result<Option<(String, serde_json::Value)>, String> {
    let out = port::find_signup_form(html, current_url)?;
    Ok(out)
}

/// `find_signup_form`.
#[pyfunction]
#[pyo3(name = "find_signup_form", signature = (html, current_url))]
fn find_signup_form(py: Python<'_>, html: String, current_url: String) -> PyResult<Py<PyAny>> {
    find_signup_form_inner(html, current_url)
        .map_err(error)
        .and_then(|v| crate::pyconv::ToPy::to_py(&v, py))
}

fn find_submit_form_inner(
    html: String,
    current_url: String,
) -> Result<Option<(String, serde_json::Value)>, String> {
    let out = port::find_submit_form(html, current_url)?;
    Ok(out)
}

/// `find_submit_form`.
#[pyfunction]
#[pyo3(name = "find_submit_form", signature = (html, current_url))]
fn find_submit_form(py: Python<'_>, html: String, current_url: String) -> PyResult<Py<PyAny>> {
    find_submit_form_inner(html, current_url)
        .map_err(error)
        .and_then(|v| crate::pyconv::ToPy::to_py(&v, py))
}

fn extract_challenge_form_inner(
    html: String,
    current_url: String,
) -> Result<Option<(String, serde_json::Value)>, String> {
    let out = port::extract_challenge_form(html, current_url)?;
    Ok(out)
}

/// `extract_challenge_form`.
#[pyfunction]
#[pyo3(name = "extract_challenge_form", signature = (html, current_url))]
fn extract_challenge_form(
    py: Python<'_>,
    html: String,
    current_url: String,
) -> PyResult<Py<PyAny>> {
    extract_challenge_form_inner(html, current_url)
        .map_err(error)
        .and_then(|v| crate::pyconv::ToPy::to_py(&v, py))
}

#[pymodule]
#[pyo3(name = "_native")]
fn native_module(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(extract_hidden_fields, m)?)?;
    m.add_function(wrap_pyfunction!(extract_input_fields, m)?)?;
    m.add_function(wrap_pyfunction!(extract_select_fields, m)?)?;
    m.add_function(wrap_pyfunction!(find_signup_form, m)?)?;
    m.add_function(wrap_pyfunction!(find_submit_form, m)?)?;
    m.add_function(wrap_pyfunction!(extract_challenge_form, m)?)?;
    Ok(())
}
