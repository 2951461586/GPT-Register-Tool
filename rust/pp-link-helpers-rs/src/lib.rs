//! Python bindings for `sms_tool/payment/pp_link_helpers.py`: generated from the signatures in `port.rs` by rust-anything.
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

fn normalize_proxy_template_inner(template: Bound<'_, PyAny>) -> Result<String, String> {
    let template: serde_json::Value = crate::pyconv::FromPy::from_py(&template)?;
    let out = port::normalize_proxy_template(template)?;
    Ok(out)
}

/// `normalize_proxy_template`.
#[pyfunction]
#[pyo3(name = "normalize_proxy_template", signature = (template))]
fn normalize_proxy_template(template: Bound<'_, PyAny>) -> PyResult<String> {
    normalize_proxy_template_inner(template).map_err(error)
}

fn proxy_for_country_template_inner(
    template: Bound<'_, PyAny>,
    country: Bound<'_, PyAny>,
) -> Result<String, String> {
    let template: serde_json::Value = crate::pyconv::FromPy::from_py(&template)?;
    let country: serde_json::Value = crate::pyconv::FromPy::from_py(&country)?;
    let out = port::proxy_for_country_template(template, country)?;
    Ok(out)
}

/// `proxy_for_country_template`.
#[pyfunction]
#[pyo3(name = "proxy_for_country_template", signature = (template, country))]
fn proxy_for_country_template(
    template: Bound<'_, PyAny>,
    country: Bound<'_, PyAny>,
) -> PyResult<String> {
    proxy_for_country_template_inner(template, country).map_err(error)
}

fn is_paypal_ba_approve_url_inner(url: Bound<'_, PyAny>) -> Result<bool, String> {
    let url: serde_json::Value = crate::pyconv::FromPy::from_py(&url)?;
    let out = port::is_paypal_ba_approve_url(url)?;
    Ok(out)
}

/// `is_paypal_ba_approve_url`.
#[pyfunction]
#[pyo3(name = "is_paypal_ba_approve_url", signature = (url))]
fn is_paypal_ba_approve_url(url: Bound<'_, PyAny>) -> PyResult<bool> {
    is_paypal_ba_approve_url_inner(url).map_err(error)
}

#[pymodule]
#[pyo3(name = "_native")]
fn native_module(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(normalize_proxy_template, m)?)?;
    m.add_function(wrap_pyfunction!(proxy_for_country_template, m)?)?;
    m.add_function(wrap_pyfunction!(is_paypal_ba_approve_url, m)?)?;
    Ok(())
}
