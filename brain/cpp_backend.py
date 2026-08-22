"""Python side of the compiled training core in brain/cpp/rnn_core.cpp.

Loads rnn_core.dll and exposes one function, train_step(), matching what
CharRNN.forward()+backward() do together but as a single call across the
Python/C++ boundary instead of a dozen separate NumPy operations.

If the DLL hasn't been built (no compiler, or brain/cpp/rnn_core.cpp
hasn't been compiled yet), `available` is False and train.py falls back
to the pure-NumPy path automatically - this was always meant to be an
optional accelerator, not a hard dependency.
"""
import ctypes
import os

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_DLL_PATH = os.path.join(_HERE, "cpp", "rnn_core.dll")

_lib = None
available = False

if os.path.exists(_DLL_PATH):
    try:
        _lib = ctypes.CDLL(_DLL_PATH)
        _f32 = ctypes.POINTER(ctypes.c_float)
        _i32 = ctypes.POINTER(ctypes.c_int32)
        _lib.rnn_train_step.argtypes = [
            _i32, _i32,                          # x, y
            _f32, _f32, _f32, _f32, _f32, _f32,  # C, Wxh, Whh, bh, Why, by
            ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int,           # B, T, E, H, Vsize
            _f32, _f32, _f32, _f32, _f32, _f32,  # dC..dby (out)
            _f32,                                 # out_loss
            ctypes.c_float,                       # clip
        ]
        _lib.rnn_train_step.restype = None
        available = True
    except OSError:
        _lib = None


def _ptr(arr, ctype):
    return arr.ctypes.data_as(ctypes.POINTER(ctype))


def train_step(model, x, y, clip=5.0):
    """Run one fused forward+backward pass in C++.

    `model` is a brain.rnn.CharRNN - read for its current weights, not
    mutated here (the caller still owns the Adam step). Returns
    (loss, grads) with grads in the same order as model.params, so the
    result plugs straight into model.step(grads, lr=...).
    """
    if not available:
        raise RuntimeError(
            "rnn_core.dll not built - run brain/cpp/build.ps1, or use the "
            "NumPy backend instead."
        )

    C, Wxh, Whh, bh, Why, by = (np.ascontiguousarray(p, dtype=np.float32)
                                for p in model.params)
    x = np.ascontiguousarray(x, dtype=np.int32)
    y = np.ascontiguousarray(y, dtype=np.int32)
    B, T = x.shape
    E, H = model.n_embd, model.n_hidden
    Vsize = model.vocab_size

    dC = np.zeros_like(C)
    dWxh = np.zeros_like(Wxh)
    dWhh = np.zeros_like(Whh)
    dbh = np.zeros_like(bh)
    dWhy = np.zeros_like(Why)
    dby = np.zeros_like(by)
    loss = ctypes.c_float(0.0)

    _lib.rnn_train_step(
        _ptr(x, ctypes.c_int32), _ptr(y, ctypes.c_int32),
        _ptr(C, ctypes.c_float), _ptr(Wxh, ctypes.c_float),
        _ptr(Whh, ctypes.c_float), _ptr(bh, ctypes.c_float),
        _ptr(Why, ctypes.c_float), _ptr(by, ctypes.c_float),
        B, T, E, H, Vsize,
        _ptr(dC, ctypes.c_float), _ptr(dWxh, ctypes.c_float),
        _ptr(dWhh, ctypes.c_float), _ptr(dbh, ctypes.c_float),
        _ptr(dWhy, ctypes.c_float), _ptr(dby, ctypes.c_float),
        ctypes.byref(loss), ctypes.c_float(clip),
    )
    return float(loss.value), [dC, dWxh, dWhh, dbh, dWhy, dby]
