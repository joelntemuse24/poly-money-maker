//! Rust hot-path helpers: the per-source tick ring scanned by `PriceGrid`.
//!
//! `TickRing` has the same eviction rule as `collections.deque(maxlen)`: when
//! full, appending drops the oldest entry.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// Fixed-capacity ring of `(timestamp_ms, price)` pairs.
#[pyclass(module = "polyhot")]
struct TickRing {
    ts: Vec<f64>,
    px: Vec<f64>,
    start: usize,
    len: usize,
    cap: usize,
}

impl TickRing {
    #[inline]
    fn oldest_index(&self) -> usize {
        self.start
    }

    #[inline]
    fn newest_index(&self) -> usize {
        (self.start + self.len - 1) % self.cap
    }
}

#[pymethods]
impl TickRing {
    #[new]
    #[pyo3(signature = (maxlen = 4096))]
    fn new(maxlen: usize) -> PyResult<Self> {
        if maxlen == 0 {
            return Err(PyValueError::new_err("TickRing maxlen must be >= 1"));
        }
        Ok(Self {
            ts: vec![0.0; maxlen],
            px: vec![0.0; maxlen],
            start: 0,
            len: 0,
            cap: maxlen,
        })
    }

    /// Append `(ts, px)`, dropping the oldest entry once `maxlen` is reached.
    fn append(&mut self, ts: f64, px: f64) {
        let idx = if self.len < self.cap {
            let idx = (self.start + self.len) % self.cap;
            self.len += 1;
            idx
        } else {
            let idx = self.start;
            self.start = (self.start + 1) % self.cap;
            idx
        };
        self.ts[idx] = ts;
        self.px[idx] = px;
    }

    fn __len__(&self) -> usize {
        self.len
    }

    /// Newest `(ts, px)`, or None when empty.
    fn last(&self) -> Option<(f64, f64)> {
        if self.len == 0 {
            None
        } else {
            let idx = self.newest_index();
            Some((self.ts[idx], self.px[idx]))
        }
    }

    /// `(min px, max px)` over every stored entry with `lower <= ts <= upper`.
    ///
    /// Full scan in insertion order; keeps the first extreme like Python
    /// `min()`/`max()` on floats.
    fn window_minmax(&self, lower: f64, upper: f64) -> Option<(f64, f64)> {
        let mut bounds: Option<(f64, f64)> = None;
        let mut idx = self.oldest_index();
        for _ in 0..self.len {
            let ts = self.ts[idx];
            if ts >= lower && ts <= upper {
                let p = self.px[idx];
                bounds = Some(match bounds {
                    None => (p, p),
                    Some((min_px, max_px)) => (
                        if p < min_px { p } else { min_px },
                        if p > max_px { p } else { max_px },
                    ),
                });
            }
            idx += 1;
            if idx == self.cap {
                idx = 0;
            }
        }
        bounds
    }
}

#[pymodule]
fn polyhot(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<TickRing>()?;
    Ok(())
}
