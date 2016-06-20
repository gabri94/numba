"""
Implementation of linear algebra operations.
"""

from __future__ import print_function, absolute_import, division

import contextlib

from llvmlite import ir

import numpy as np

from numba import jit, types, cgutils

from numba.targets.imputils import (lower_builtin, impl_ret_borrowed,
                                    impl_ret_new_ref, impl_ret_untracked)
from numba.typing import signature
from numba.extending import overload
from numba.numpy_support import version as numpy_version
from numba import types
from numba import numpy_support as np_support
from .arrayobj import make_array, _empty_nd_impl, array_copy
from ..errors import TypingError

ll_char = ir.IntType(8)
ll_char_p = ll_char.as_pointer()
ll_void_p = ll_char_p
ll_intc = ir.IntType(32)
ll_intc_p = ll_intc.as_pointer()
intp_t = cgutils.intp_t
ll_intp_p = intp_t.as_pointer()


# fortran int type, this needs to match the F_INT C declaration in
# _lapack.c and is present to accomodate potential future 64bit int
# based LAPACK use.
F_INT_nptype = np.int32
F_INT_nbtype = types.int32

# BLAS kinds as letters
_blas_kinds = {
    types.float32: 's',
    types.float64: 'd',
    types.complex64: 'c',
    types.complex128: 'z',
}


def get_blas_kind(dtype, func_name="<BLAS function>"):
    kind = _blas_kinds.get(dtype)
    if kind is None:
        raise TypeError("unsupported dtype for %s()" % (func_name,))
    return kind


def ensure_blas():
    try:
        import scipy.linalg.cython_blas
    except ImportError:
        raise ImportError("scipy 0.16+ is required for linear algebra")


def ensure_lapack():
    try:
        import scipy.linalg.cython_lapack
    except ImportError:
        raise ImportError("scipy 0.16+ is required for linear algebra")


def make_constant_slot(context, builder, ty, val):
    const = context.get_constant_generic(builder, ty, val)
    return cgutils.alloca_once_value(builder, const)


@contextlib.contextmanager
def make_contiguous(context, builder, sig, args):
    """
    Ensure that all array arguments are contiguous, if necessary by
    copying them.
    A new (sig, args) tuple is yielded.
    """
    newtys = []
    newargs = []
    copies = []
    for ty, val in zip(sig.args, args):
        if not isinstance(ty, types.Array) or ty.layout in 'CF':
            newty, newval = ty, val
        else:
            newty = ty.copy(layout='C')
            copysig = signature(newty, ty)
            newval = array_copy(context, builder, copysig, (val,))
            copies.append((newty, newval))
        newtys.append(newty)
        newargs.append(newval)
    yield signature(sig.return_type, *newtys), tuple(newargs)
    for ty, val in copies:
        context.nrt_decref(builder, ty, val)


def check_c_int(context, builder, n):
    """
    Check whether *n* fits in a C `int`.
    """
    _maxint = 2**31 - 1

    def impl(n):
        if n > _maxint:
            raise OverflowError("array size too large to fit in C int")

    context.compile_internal(builder, impl,
                             signature(types.none, types.intp), (n,))


def check_blas_return(context, builder, res):
    """
    Check the integer error return from one of the BLAS wrappers in
    _helperlib.c.
    """
    with builder.if_then(cgutils.is_not_null(builder, res), likely=False):
        # Those errors shouldn't happen, it's easier to just abort the process
        pyapi = context.get_python_api(builder)
        pyapi.gil_ensure()
        pyapi.fatal_error("BLAS wrapper returned with an error")


def check_lapack_return(context, builder, res):
    """
    Check the integer error return from one of the LAPACK wrappers in
    _helperlib.c.
    """
    with builder.if_then(cgutils.is_not_null(builder, res), likely=False):
        # Those errors shouldn't happen, it's easier to just abort the process
        pyapi = context.get_python_api(builder)
        pyapi.gil_ensure()
        pyapi.fatal_error("LAPACK wrapper returned with an error")


def call_xxdot(context, builder, conjugate, dtype,
               n, a_data, b_data, out_data):
    """
    Call the BLAS vector * vector product function for the given arguments.
    """
    fnty = ir.FunctionType(ir.IntType(32),
                           [ll_char, ll_char, intp_t,    # kind, conjugate, n
                            ll_void_p, ll_void_p, ll_void_p,  # a, b, out
                            ])
    fn = builder.module.get_or_insert_function(fnty, name="numba_xxdot")

    kind = get_blas_kind(dtype)
    kind_val = ir.Constant(ll_char, ord(kind))
    conjugate = ir.Constant(ll_char, int(conjugate))

    res = builder.call(fn, (kind_val, conjugate, n,
                            builder.bitcast(a_data, ll_void_p),
                            builder.bitcast(b_data, ll_void_p),
                            builder.bitcast(out_data, ll_void_p)))
    check_blas_return(context, builder, res)


def call_xxgemv(context, builder, do_trans,
                m_type, m_shapes, m_data, v_data, out_data):
    """
    Call the BLAS matrix * vector product function for the given arguments.
    """
    fnty = ir.FunctionType(ir.IntType(32),
                           [ll_char, ll_char,                 # kind, trans
                            intp_t, intp_t,                   # m, n
                            ll_void_p, ll_void_p, intp_t,     # alpha, a, lda
                            ll_void_p, ll_void_p, ll_void_p,  # x, beta, y
                            ])
    fn = builder.module.get_or_insert_function(fnty, name="numba_xxgemv")

    dtype = m_type.dtype
    alpha = make_constant_slot(context, builder, dtype, 1.0)
    beta = make_constant_slot(context, builder, dtype, 0.0)

    if m_type.layout == 'F':
        m, n = m_shapes
        lda = m_shapes[0]
    else:
        n, m = m_shapes
        lda = m_shapes[1]

    kind = get_blas_kind(dtype)
    kind_val = ir.Constant(ll_char, ord(kind))
    trans = ir.Constant(ll_char, ord('t') if do_trans else ord('n'))

    res = builder.call(fn, (kind_val, trans, m, n,
                            builder.bitcast(alpha, ll_void_p),
                            builder.bitcast(m_data, ll_void_p), lda,
                            builder.bitcast(v_data, ll_void_p),
                            builder.bitcast(beta, ll_void_p),
                            builder.bitcast(out_data, ll_void_p)))
    check_blas_return(context, builder, res)


def call_xxgemm(context, builder,
                x_type, x_shapes, x_data,
                y_type, y_shapes, y_data,
                out_type, out_shapes, out_data):
    """
    Call the BLAS matrix * matrix product function for the given arguments.
    """
    fnty = ir.FunctionType(ir.IntType(32),
                           [ll_char,                       # kind
                            ll_char, ll_char,          # transa, transb
                            intp_t, intp_t, intp_t,        # m, n, k
                            ll_void_p, ll_void_p, intp_t,  # alpha, a, lda
                            ll_void_p, intp_t, ll_void_p,  # b, ldb, beta
                            ll_void_p, intp_t,             # c, ldc
                            ])
    fn = builder.module.get_or_insert_function(fnty, name="numba_xxgemm")

    m, k = x_shapes
    _k, n = y_shapes
    dtype = x_type.dtype
    alpha = make_constant_slot(context, builder, dtype, 1.0)
    beta = make_constant_slot(context, builder, dtype, 0.0)

    trans = ir.Constant(ll_char, ord('t'))
    notrans = ir.Constant(ll_char, ord('n'))

    def get_array_param(ty, shapes, data):
        return (
            # Transpose if layout different from result's
            notrans if ty.layout == out_type.layout else trans,
            # Size of the inner dimension in physical array order
            shapes[1] if ty.layout == 'C' else shapes[0],
            # The data pointer, unit-less
            builder.bitcast(data, ll_void_p),
        )

    transa, lda, data_a = get_array_param(y_type, y_shapes, y_data)
    transb, ldb, data_b = get_array_param(x_type, x_shapes, x_data)
    _, ldc, data_c = get_array_param(out_type, out_shapes, out_data)

    kind = get_blas_kind(dtype)
    kind_val = ir.Constant(ll_char, ord(kind))

    res = builder.call(fn, (kind_val, transa, transb, n, m, k,
                            builder.bitcast(alpha, ll_void_p), data_a, lda,
                            data_b, ldb, builder.bitcast(beta, ll_void_p),
                            data_c, ldc))
    check_blas_return(context, builder, res)


def dot_2_mm(context, builder, sig, args):
    """
    np.dot(matrix, matrix)
    """
    def dot_impl(a, b):
        m, k = a.shape
        _k, n = b.shape
        out = np.empty((m, n), a.dtype)
        return np.dot(a, b, out)

    res = context.compile_internal(builder, dot_impl, sig, args)
    return impl_ret_new_ref(context, builder, sig.return_type, res)


def dot_2_vm(context, builder, sig, args):
    """
    np.dot(vector, matrix)
    """
    def dot_impl(a, b):
        m, = a.shape
        _m, n = b.shape
        out = np.empty((n, ), a.dtype)
        return np.dot(a, b, out)

    res = context.compile_internal(builder, dot_impl, sig, args)
    return impl_ret_new_ref(context, builder, sig.return_type, res)


def dot_2_mv(context, builder, sig, args):
    """
    np.dot(matrix, vector)
    """
    def dot_impl(a, b):
        m, n = a.shape
        _n, = b.shape
        out = np.empty((m, ), a.dtype)
        return np.dot(a, b, out)

    res = context.compile_internal(builder, dot_impl, sig, args)
    return impl_ret_new_ref(context, builder, sig.return_type, res)


def dot_2_vv(context, builder, sig, args, conjugate=False):
    """
    np.dot(vector, vector)
    np.vdot(vector, vector)
    """
    aty, bty = sig.args
    dtype = sig.return_type
    a = make_array(aty)(context, builder, args[0])
    b = make_array(bty)(context, builder, args[1])
    n, = cgutils.unpack_tuple(builder, a.shape)

    def check_args(a, b):
        m, = a.shape
        n, = b.shape
        if m != n:
            raise ValueError("incompatible array sizes for np.dot(a, b) "
                             "(vector * vector)")

    context.compile_internal(builder, check_args,
                             signature(types.none, *sig.args), args)
    check_c_int(context, builder, n)

    out = cgutils.alloca_once(builder, context.get_value_type(dtype))
    call_xxdot(context, builder, conjugate, dtype, n, a.data, b.data, out)
    return builder.load(out)


@lower_builtin(np.dot, types.Array, types.Array)
@lower_builtin('@', types.Array, types.Array)
def dot_2(context, builder, sig, args):
    """
    np.dot(a, b)
    a @ b
    """
    ensure_blas()

    with make_contiguous(context, builder, sig, args) as (sig, args):
        ndims = [x.ndim for x in sig.args[:2]]
        if ndims == [2, 2]:
            return dot_2_mm(context, builder, sig, args)
        elif ndims == [2, 1]:
            return dot_2_mv(context, builder, sig, args)
        elif ndims == [1, 2]:
            return dot_2_vm(context, builder, sig, args)
        elif ndims == [1, 1]:
            return dot_2_vv(context, builder, sig, args)
        else:
            assert 0


@lower_builtin(np.vdot, types.Array, types.Array)
def vdot(context, builder, sig, args):
    """
    np.vdot(a, b)
    """
    ensure_blas()

    with make_contiguous(context, builder, sig, args) as (sig, args):
        return dot_2_vv(context, builder, sig, args, conjugate=True)


def dot_3_vm(context, builder, sig, args):
    """
    np.dot(vector, matrix, out)
    np.dot(matrix, vector, out)
    """
    xty, yty, outty = sig.args
    assert outty == sig.return_type
    dtype = xty.dtype

    x = make_array(xty)(context, builder, args[0])
    y = make_array(yty)(context, builder, args[1])
    out = make_array(outty)(context, builder, args[2])
    x_shapes = cgutils.unpack_tuple(builder, x.shape)
    y_shapes = cgutils.unpack_tuple(builder, y.shape)
    out_shapes = cgutils.unpack_tuple(builder, out.shape)
    if xty.ndim < yty.ndim:
        # Vector * matrix
        # Asked for x * y, we will compute y.T * x
        mty = yty
        m_shapes = y_shapes
        do_trans = yty.layout == 'F'
        m_data, v_data = y.data, x.data

        def check_args(a, b, out):
            m, = a.shape
            _m, n = b.shape
            if m != _m:
                raise ValueError("incompatible array sizes for "
                                 "np.dot(a, b) (vector * matrix)")
            if out.shape != (n,):
                raise ValueError("incompatible output array size for "
                                 "np.dot(a, b, out) (vector * matrix)")
    else:
        # Matrix * vector
        # We will compute x * y
        mty = xty
        m_shapes = x_shapes
        do_trans = xty.layout == 'C'
        m_data, v_data = x.data, y.data

        def check_args(a, b, out):
            m, _n = a.shape
            n, = b.shape
            if n != _n:
                raise ValueError("incompatible array sizes for np.dot(a, b) "
                                 "(matrix * vector)")
            if out.shape != (m,):
                raise ValueError("incompatible output array size for "
                                 "np.dot(a, b, out) (matrix * vector)")

    context.compile_internal(builder, check_args,
                             signature(types.none, *sig.args), args)
    for val in m_shapes:
        check_c_int(context, builder, val)

    call_xxgemv(context, builder, do_trans, mty, m_shapes, m_data,
                v_data, out.data)

    return impl_ret_borrowed(context, builder, sig.return_type,
                             out._getvalue())


def dot_3_mm(context, builder, sig, args):
    """
    np.dot(matrix, matrix, out)
    """
    xty, yty, outty = sig.args
    assert outty == sig.return_type
    dtype = xty.dtype

    x = make_array(xty)(context, builder, args[0])
    y = make_array(yty)(context, builder, args[1])
    out = make_array(outty)(context, builder, args[2])
    x_shapes = cgutils.unpack_tuple(builder, x.shape)
    y_shapes = cgutils.unpack_tuple(builder, y.shape)
    out_shapes = cgutils.unpack_tuple(builder, out.shape)
    m, k = x_shapes
    _k, n = y_shapes

    # The only case Numpy supports
    assert outty.layout == 'C'

    def check_args(a, b, out):
        m, k = a.shape
        _k, n = b.shape
        if k != _k:
            raise ValueError("incompatible array sizes for np.dot(a, b) "
                             "(matrix * matrix)")
        if out.shape != (m, n):
            raise ValueError("incompatible output array size for "
                             "np.dot(a, b, out) (matrix * matrix)")

    context.compile_internal(builder, check_args,
                             signature(types.none, *sig.args), args)
    check_c_int(context, builder, m)
    check_c_int(context, builder, k)
    check_c_int(context, builder, n)

    x_data = x.data
    y_data = y.data
    out_data = out.data

    # Check whether any of the operands is really a 1-d vector represented
    # as a (1, k) or (k, 1) 2-d array.  In those cases, it is pessimal
    # to call the generic matrix * matrix product BLAS function.
    one = ir.Constant(intp_t, 1)
    is_left_vec = builder.icmp_signed('==', m, one)
    is_right_vec = builder.icmp_signed('==', n, one)

    with builder.if_else(is_right_vec) as (r_vec, r_mat):
        with r_vec:
            with builder.if_else(is_left_vec) as (v_v, m_v):
                with v_v:
                    # V * V
                    call_xxdot(context, builder, False, dtype,
                               k, x_data, y_data, out_data)
                with m_v:
                    # M * V
                    do_trans = xty.layout == outty.layout
                    call_xxgemv(context, builder, do_trans,
                                xty, x_shapes, x_data, y_data, out_data)
        with r_mat:
            with builder.if_else(is_left_vec) as (v_m, m_m):
                with v_m:
                    # V * M
                    do_trans = yty.layout != outty.layout
                    call_xxgemv(context, builder, do_trans,
                                yty, y_shapes, y_data, x_data, out_data)
                with m_m:
                    # M * M
                    call_xxgemm(context, builder,
                                xty, x_shapes, x_data,
                                yty, y_shapes, y_data,
                                outty, out_shapes, out_data)

    return impl_ret_borrowed(context, builder, sig.return_type,
                             out._getvalue())


@lower_builtin(np.dot, types.Array, types.Array,
               types.Array)
def dot_3(context, builder, sig, args):
    """
    np.dot(a, b, out)
    """
    ensure_blas()

    with make_contiguous(context, builder, sig, args) as (sig, args):
        ndims = set(x.ndim for x in sig.args[:2])
        if ndims == set([2]):
            return dot_3_mm(context, builder, sig, args)
        elif ndims == set([1, 2]):
            return dot_3_vm(context, builder, sig, args)
        else:
            assert 0

fatal_error_sig = types.intc()
fatal_error_func = types.ExternalFunction("numba_fatal_error", fatal_error_sig)


@jit(nopython=True)
def _check_finite_matrix(a):
    for v in np.nditer(a):
        if not np.isfinite(v.item()):
            raise np.linalg.LinAlgError(
                "Array must not contain infs or NaNs.")


def _check_linalg_matrix(a, func_name):
    if not isinstance(a, types.Array):
        raise TypingError("np.linalg.%s() only supported for array types"
                          % func_name)
    if not a.ndim == 2:
        raise TypingError("np.linalg.%s() only supported on 2-D arrays."
                          % func_name)
    if not isinstance(a.dtype, (types.Float, types.Complex)):
        raise TypingError("np.linalg.%s() only supported on "
                          "float and complex arrays." % func_name)


@jit(nopython=True)
def _inv_err_handler(r):
    if r != 0:
        if r < 0:
            fatal_error_func()
            assert 0   # unreachable
        if r > 0:  # this condition should be caught already above!
            raise np.linalg.LinAlgError(
                "Matrix is singular and cannot be inverted.")


@overload(np.linalg.inv)
def inv_impl(a):
    ensure_lapack()

    _check_linalg_matrix(a, "inv")

    numba_xxgetrf_sig = types.intc(types.char,   # kind
                                   types.intp,  # m
                                   types.intp,  # n
                                   types.CPointer(a.dtype),  # a
                                   types.intp,  # lda
                                   types.CPointer(F_INT_nbtype)  # ipiv
                                   )

    numba_xxgetrf = types.ExternalFunction("numba_xxgetrf",
                                           numba_xxgetrf_sig)

    numba_ez_xxgetri_sig = types.intc(types.char,   # kind
                                      types.intp,  # n
                                      types.CPointer(a.dtype),  # a
                                      types.intp,  # lda
                                      types.CPointer(F_INT_nbtype)  # ipiv
                                      )

    numba_ez_xxgetri = types.ExternalFunction("numba_ez_xxgetri",
                                              numba_ez_xxgetri_sig)

    kind = ord(get_blas_kind(a.dtype, "inv"))

    F_layout = a.layout == 'F'

    def inv_impl(a):
        n = a.shape[-1]
        if a.shape[-2] != n:
            msg = "Last 2 dimensions of the array must be square."
            raise np.linalg.LinAlgError(msg)

        _check_finite_matrix(a)

        if F_layout:
            acpy = np.copy(a)
        else:
            acpy = np.asfortranarray(a)

        ipiv = np.empty(n, dtype=F_INT_nptype)

        r = numba_xxgetrf(kind, n, n, acpy.ctypes, n, ipiv.ctypes)
        _inv_err_handler(r)

        r = numba_ez_xxgetri(kind, n, acpy.ctypes, n, ipiv.ctypes)
        _inv_err_handler(r)

        # help liveness analysis
        acpy.size
        ipiv.size
        return acpy

    return inv_impl


@jit(nopython=True)
def _handle_err_maybe_convergence_problem(r):
    if r != 0:
        if r < 0:
            fatal_error_func()
            assert 0   # unreachable
        if r > 0:
            raise ValueError("Internal algorithm failed to converge.")


def _check_linalg_1_or_2d_matrix(a, func_name):
    # checks that a matrix is 1 or 2D
    if not isinstance(a, types.Array):
        raise TypingError("np.linalg.%s() only supported for array types "
                          % func_name)
    if not a.ndim <= 2:
        raise TypingError("np.linalg.%s() only supported on 1 and 2-D arrays "
                          % func_name)
    if not isinstance(a.dtype, (types.Float, types.Complex)):
        raise TypingError("np.linalg.%s() only supported on "
                          "float and complex arrays." % func_name)

if numpy_version >= (1, 8):

    @overload(np.linalg.cholesky)
    def cho_impl(a):
        ensure_lapack()

        _check_linalg_matrix(a, "cholesky")

        xxpotrf_sig = types.intc(types.int8, types.int8, types.intp,
                                 types.CPointer(a.dtype), types.intp)
        xxpotrf = types.ExternalFunction("numba_xxpotrf", xxpotrf_sig)

        kind = ord(get_blas_kind(a.dtype, "cholesky"))
        UP = ord('U')
        LO = ord('L')

        def cho_impl(a):
            n = a.shape[-1]
            if a.shape[-2] != n:
                msg = "Last 2 dimensions of the array must be square."
                raise np.linalg.LinAlgError(msg)

            # The output is allocated in C order
            out = a.copy()
            # Pass UP since xxpotrf() operates in F order
            # The semantics ensure this works fine
            # (out is really its Hermitian in F order, but UP instructs
            #  xxpotrf to compute the Hermitian of the upper triangle
            #  => they cancel each other)
            r = xxpotrf(kind, UP, n, out.ctypes, n)
            if r != 0:
                if r < 0:
                    fatal_error_func()
                    assert 0   # unreachable
                if r > 0:
                    raise np.linalg.LinAlgError(
                        "Matrix is not positive definite.")
            # Zero out upper triangle, in F order
            for col in range(n):
                out[:col, col] = 0
            return out

        return cho_impl

    @overload(np.linalg.eig)
    def eig_impl(a):
        ensure_lapack()

        _check_linalg_matrix(a, "eig")

        numba_ez_rgeev_sig = types.intc(types.char,  # kind
                                        types.char,  # jobvl
                                        types.char,  # jobvr
                                        types.intp,  # n
                                        types.CPointer(a.dtype),  # a
                                        types.intp,  # lda
                                        types.CPointer(a.dtype),  # wr
                                        types.CPointer(a.dtype),  # wi
                                        types.CPointer(a.dtype),  # vl
                                        types.intp,  # ldvl
                                        types.CPointer(a.dtype),  # vr
                                        types.intp  # ldvr
                                        )

        numba_ez_rgeev = types.ExternalFunction("numba_ez_rgeev",
                                                numba_ez_rgeev_sig)

        numba_ez_cgeev_sig = types.intc(types.char,  # kind
                                        types.char,  # jobvl
                                        types.char,  # jobvr
                                        types.intp,  # n
                                        types.CPointer(a.dtype),  # a
                                        types.intp,  # lda
                                        types.CPointer(a.dtype),  # w
                                        types.CPointer(a.dtype),  # vl
                                        types.intp,  # ldvl
                                        types.CPointer(a.dtype),  # vr
                                        types.intp  # ldvr
                                        )

        numba_ez_cgeev = types.ExternalFunction("numba_ez_cgeev",
                                                numba_ez_cgeev_sig)

        kind = ord(get_blas_kind(a.dtype, "eig"))

        JOBVL = ord('N')
        JOBVR = ord('V')

        F_layout = a.layout == 'F'

        def real_eig_impl(a):
            """
            eig() implementation for real arrays.
            """
            n = a.shape[-1]
            if a.shape[-2] != n:
                msg = "Last 2 dimensions of the array must be square."
                raise np.linalg.LinAlgError(msg)

            _check_finite_matrix(a)

            if F_layout:
                acpy = np.copy(a)
            else:
                acpy = np.asfortranarray(a)

            ldvl = 1
            ldvr = n
            wr = np.empty(n, dtype=a.dtype)
            wi = np.empty(n, dtype=a.dtype)
            vl = np.empty((n, ldvl), dtype=a.dtype)
            vr = np.empty((n, ldvr), dtype=a.dtype)

            r = numba_ez_rgeev(kind,
                               JOBVL,
                               JOBVR,
                               n,
                               acpy.ctypes,
                               n,
                               wr.ctypes,
                               wi.ctypes,
                               vl.ctypes,
                               ldvl,
                               vr.ctypes,
                               ldvr)
            _handle_err_maybe_convergence_problem(r)

            # By design numba does not support dynamic return types, however,
            # Numpy does. Numpy uses this ability in the case of returning
            # eigenvalues/vectors of a real matrix. The return type of
            # np.linalg.eig(), when operating on a matrix in real space
            # depends on the values present in the matrix itself (recalling
            # that eigenvalues are the roots of the characteristic polynomial
            # of the system matrix, which will by construction depend on the
            # values present in the system matrix). As numba cannot handle
            # the case of a runtime decision based domain change relative to
            # the input type, if it is required numba raises as below.
            if np.any(wi):
                raise ValueError(
                    "eig() argument must not cause a domain change.")

            # put these in to help with liveness analysis,
            # `.ctypes` doesn't keep the vars alive
            acpy.size
            vl.size
            vr.size
            wr.size
            wi.size
            return (wr, vr.T)

        def cmplx_eig_impl(a):
            """
            eig() implementation for complex arrays.
            """
            n = a.shape[-1]
            if a.shape[-2] != n:
                msg = "Last 2 dimensions of the array must be square."
                raise np.linalg.LinAlgError(msg)

            _check_finite_matrix(a)

            if F_layout:
                acpy = np.copy(a)
            else:
                acpy = np.asfortranarray(a)

            ldvl = 1
            ldvr = n
            w = np.empty(n, dtype=a.dtype)
            vl = np.empty((n, ldvl), dtype=a.dtype)
            vr = np.empty((n, ldvr), dtype=a.dtype)

            r = numba_ez_cgeev(kind,
                               JOBVL,
                               JOBVR,
                               n,
                               acpy.ctypes,
                               n,
                               w.ctypes,
                               vl.ctypes,
                               ldvl,
                               vr.ctypes,
                               ldvr)
            _handle_err_maybe_convergence_problem(r)

            # put these in to help with liveness analysis,
            # `.ctypes` doesn't keep the vars alive
            acpy.size
            vl.size
            vr.size
            w.size
            return (w, vr.T)

        if isinstance(a.dtype, types.scalars.Complex):
            return cmplx_eig_impl
        else:
            return real_eig_impl

    @overload(np.linalg.svd)
    def svd_impl(a, full_matrices=1):
        ensure_lapack()

        _check_linalg_matrix(a, "svd")

        F_layout = a.layout == 'F'

        # convert typing floats to numpy floats for use in the impl
        s_type = getattr(a.dtype, "underlying_float", a.dtype)
        if s_type.bitwidth == 32:
            s_dtype = np.float32
        else:
            s_dtype = np.float64

        numba_ez_gesdd_sig = types.intc(
            types.char,  # kind
            types.char,  # jobz
            types.intp,  # m
            types.intp,  # n
            types.CPointer(a.dtype),  # a
            types.intp,  # lda
            types.CPointer(s_type),  # s
            types.CPointer(a.dtype),  # u
            types.intp,  # ldu
            types.CPointer(a.dtype),  # vt
            types.intp  # ldvt
        )

        numba_ez_gesdd = types.ExternalFunction("numba_ez_gesdd",
                                                numba_ez_gesdd_sig)

        kind = ord(get_blas_kind(a.dtype, "svd"))

        JOBZ_A = ord('A')
        JOBZ_S = ord('S')

        def svd_impl(a, full_matrices=1):
            n = a.shape[-1]
            m = a.shape[-2]

            _check_finite_matrix(a)

            if F_layout:
                acpy = np.copy(a)
            else:
                acpy = np.asfortranarray(a)

            ldu = m
            minmn = min(m, n)

            if full_matrices:
                JOBZ = JOBZ_A
                ucol = m
                ldvt = n
            else:
                JOBZ = JOBZ_S
                ucol = minmn
                ldvt = minmn

            u = np.empty((ucol, ldu), dtype=a.dtype)
            s = np.empty(minmn, dtype=s_dtype)
            vt = np.empty((n, ldvt), dtype=a.dtype)

            r = numba_ez_gesdd(
                kind,  # kind
                JOBZ,  # jobz
                m,  # m
                n,  # n
                acpy.ctypes,  # a
                m,  # lda
                s.ctypes,  # s
                u.ctypes,  # u
                ldu,  # ldu
                vt.ctypes,  # vt
                ldvt          # ldvt
            )
            _handle_err_maybe_convergence_problem(r)

            # help liveness analysis
            acpy.size
            vt.size
            u.size
            s.size

            return (u.T, s, vt.T)

        return svd_impl


@overload(np.linalg.qr)
def qr_impl(a):
    ensure_lapack()

    _check_linalg_matrix(a, "qr")

    # Need two functions, the first computes R, storing it in the upper
    # triangle of A with the below diagonal part of A containing elementary
    # reflectors needed to construct Q. The second turns the below diagonal
    # entries of A into Q, storing Q in A (creates orthonormal columns from
    # the elementary reflectors).

    numba_ez_geqrf_sig = types.intc(
        types.char,  # kind
        types.intp,  # m
        types.intp,  # n
        types.CPointer(a.dtype),  # a
        types.intp,  # lda
        types.CPointer(a.dtype),  # tau
    )

    numba_ez_geqrf = types.ExternalFunction("numba_ez_geqrf",
                                            numba_ez_geqrf_sig)

    numba_ez_xxgqr_sig = types.intc(
        types.char,  # kind
        types.intp,  # m
        types.intp,  # n
        types.intp,  # k
        types.CPointer(a.dtype),  # a
        types.intp,  # lda
        types.CPointer(a.dtype),  # tau
    )

    numba_ez_xxgqr = types.ExternalFunction("numba_ez_xxgqr",
                                            numba_ez_xxgqr_sig)

    kind = ord(get_blas_kind(a.dtype, "qr"))

    F_layout = a.layout == 'F'

    def qr_impl(a):
        n = a.shape[-1]
        m = a.shape[-2]

        _check_finite_matrix(a)

        # copy A as it will be destroyed
        if F_layout:
            q = np.copy(a)
        else:
            q = np.asfortranarray(a)

        lda = m

        minmn = min(m, n)
        tau = np.empty((minmn), dtype=a.dtype)

        ret = numba_ez_geqrf(
            kind,  # kind
            m,  # m
            n,  # n
            q.ctypes,  # a
            m,  # lda
            tau.ctypes  # tau
        )
        if ret < 0:
            fatal_error_func()
            assert 0   # unreachable

        # pull out R, this is transposed because of Fortran
        r = np.zeros((n, minmn), dtype=a.dtype).T

        # the triangle in R
        for i in range(minmn):
            for j in range(i + 1):
                r[j, i] = q[j, i]

        # and the possible square in R
        for i in range(minmn, n):
            for j in range(minmn):
                r[j, i] = q[j, i]

        ret = numba_ez_xxgqr(
            kind,  # kind
            m,  # m
            minmn,  # n
            minmn,  # k
            q.ctypes,  # a
            m,  # lda
            tau.ctypes  # tau
        )
        _handle_err_maybe_convergence_problem(ret)

        # help liveness analysis
        tau.size
        q.size

        return (q[:, :minmn], r)

    return qr_impl


# helpers and jitted specialisations required for np.linalg.lstsq
# and np.linalg.solve. These functions have "system" in their name
# as a differentiator.

def _get_system_copy_in_b_impl(b):
    # gets an implementation for correctly copying 'b' into the
    # scratch space for 'b'
    ndim = b.ndim
    if ndim == 1:
        @jit(nopython=True)
        def oneD_impl(bcpy, b, nrhs):
            bcpy[:b.shape[-1], 0] = b
        return oneD_impl
    else:
        @jit(nopython=True)
        def twoD_impl(bcpy, b, nrhs):
            bcpy[:b.shape[-2], :nrhs] = b
        return twoD_impl


def _get_system_compute_nrhs(b):
    # gets and implementation for computing the number of right hand
    # sides in the system of equations
    ndim = b.ndim
    if ndim == 1:
        @jit(nopython=True)
        def oneD_impl(b):
            return 1
        return oneD_impl
    else:
        @jit(nopython=True)
        def twoD_impl(b):
            return b.shape[-1]
        return twoD_impl


def _get_system_check_dimensionally_valid_impl(a, b):
    # gets and implementation for checking that AX=B style system
    # input is dimensionally valid
    ndim = b.ndim
    if ndim == 1:
        @jit(nopython=True)
        def oneD_impl(a, b):
            am = a.shape[-2]
            bm = b.shape[-1]
            if am != bm:
                raise np.linalg.LinAlgError(
                    "Incompatible array sizes, system is not dimensionally valid.")
        return oneD_impl
    else:
        @jit(nopython=True)
        def twoD_impl(a, b):
            am = a.shape[-2]
            bm = b.shape[-2]
            if am != bm:
                raise np.linalg.LinAlgError(
                    "Incompatible array sizes, system is not dimensionally valid.")
        return twoD_impl

# jitted specialisations for np.linalg.lstsq()


def _get_lstsq_res_impl(dtype, real_dtype, b):
    # gets an implementation for computing the residual
    ndim = b.ndim
    if ndim == 1:
        if isinstance(dtype, (types.Complex)):
            @jit(nopython=True)
            def cmplx_impl(b, n, nrhs):
                res = np.empty((1,), dtype=real_dtype)
                res[0] = np.sum(np.abs(b[n:, 0])**2)
                return res
            return cmplx_impl
        else:
            @jit(nopython=True)
            def real_impl(b, n, nrhs):
                res = np.empty((1,), dtype=real_dtype)
                res[0] = np.sum(b[n:, 0]**2)
                return res
            return real_impl
    else:
        if isinstance(dtype, (types.Complex)):
            @jit(nopython=True)
            def cmplx_impl(b, n, nrhs):
                res = np.empty((nrhs), dtype=real_dtype)
                for k in range(nrhs):
                    res[k] = np.sum(np.abs(b[n:, k])**2)
                return res
            return cmplx_impl
        else:
            @jit(nopython=True)
            def real_impl(b, n, nrhs):
                res = np.empty((nrhs), dtype=real_dtype)
                for k in range(nrhs):
                    res[k] = np.sum(b[n:, k]**2)
                return res
            return real_impl


def _get_lstsq_compute_return_impl(b):
    # gets an implementation for extracting 'x' (the solution) from
    # the 'b' scratch space
    ndim = b.ndim
    if ndim == 1:
        @jit(nopython=True)
        def oneD_impl(b, n):
            return b.T.ravel()[:n]
        return oneD_impl
    else:
        @jit(nopython=True)
        def twoD_impl(b, n):
            return b[:n, :].copy()
        return twoD_impl


@overload(np.linalg.lstsq)
def lstsq_impl(a, b, rcond=-1.0):
    ensure_lapack()

    _check_linalg_matrix(a, "lstsq")

    # B can be 1D or 2D.
    _check_linalg_1_or_2d_matrix(b, "lstsq")

    a_F_layout = a.layout == 'F'
    b_F_layout = b.layout == 'F'

    # the typing context is not easily accessible in `@overload` mode
    # so type unification etc. is done manually below
    a_np_dt = np_support.as_dtype(a.dtype)
    b_np_dt = np_support.as_dtype(b.dtype)

    np_shared_dt = np.promote_types(a_np_dt, b_np_dt)
    nb_shared_dt = np_support.from_dtype(np_shared_dt)

    # convert typing floats to np floats for use in the impl
    r_type = getattr(nb_shared_dt, "underlying_float", nb_shared_dt)
    if r_type.bitwidth == 32:
        real_dtype = np.float32
    else:
        real_dtype = np.float64

    # the lapack wrapper signature
    numba_ez_gelsd_sig = types.intc(
        types.char,  # kind
        types.intp,  # m
        types.intp,  # n
        types.intp,  # nrhs
        types.CPointer(nb_shared_dt),  # a
        types.intp,  # lda
        types.CPointer(nb_shared_dt),  # b
        types.intp,  # ldb
        types.CPointer(r_type),  # S
        types.float64,  # rcond
        types.CPointer(types.intc)  # rank
    )

    # the lapack wrapper function
    numba_ez_gelsd = types.ExternalFunction("numba_ez_gelsd",
                                            numba_ez_gelsd_sig)

    kind = ord(get_blas_kind(nb_shared_dt, "lstsq"))

    # The following functions select specialisations based on
    # information around 'b', a lot of this effort is required
    # as 'b' can be either 1D or 2D, and then there are
    # some optimisations available depending on real or complex
    # space.

    # get a specialisation for computing the number of RHS
    b_nrhs = _get_system_compute_nrhs(b)

    # get a specialised residual computation based on the dtype
    compute_res = _get_lstsq_res_impl(nb_shared_dt, real_dtype, b)

    # b copy function
    b_copy_in = _get_system_copy_in_b_impl(b)

    # return blob function
    b_ret = _get_lstsq_compute_return_impl(b)

    # check system is dimensionally valid function
    check_dimensionally_valid = _get_system_check_dimensionally_valid_impl(
        a, b)

    def lstsq_impl(a, b, rcond=-1.0):
        n = a.shape[-1]
        m = a.shape[-2]
        nrhs = b_nrhs(b)

        # check the systems have no inf or NaN
        _check_finite_matrix(a)
        _check_finite_matrix(b)

        # check the systems is dimensionally valid
        check_dimensionally_valid(a, b)

        minmn = min(m, n)
        maxmn = max(m, n)

        # a is destroyed on exit, copy it
        acpy = a.astype(np_shared_dt)
        if a_F_layout:
            acpy = np.copy(acpy)
        else:
            acpy = np.asfortranarray(acpy)

        # b is overwritten on exit with the solution, copy allocate
        bcpy = np.empty((nrhs, maxmn), dtype=np_shared_dt).T
        # specialised copy in due to b being 1 or 2D
        b_copy_in(bcpy, b, nrhs)

        # Allocate returns
        s = np.empty(minmn, dtype=real_dtype)
        rank_ptr = np.empty(1, dtype=np.int32)

        r = numba_ez_gelsd(
            kind,  # kind
            m,  # m
            n,  # n
            nrhs,  # nrhs
            acpy.ctypes,  # a
            m,  # lda
            bcpy.ctypes,  # a
            maxmn,  # ldb
            s.ctypes,  # s
            rcond,  # rcond
            rank_ptr.ctypes  # rank
        )
        _handle_err_maybe_convergence_problem(r)

        # set rank to that which was computed
        rank = rank_ptr[0]

        # compute residuals
        if rank < n or m <= n:
            res = np.empty((0), dtype=real_dtype)
        else:
            # this requires additional dispatch as there's a faster
            # impl if the result is in the real domain (no abs() required)
            res = compute_res(bcpy, n, nrhs)

        # extract 'x', the solution
        x = b_ret(bcpy, n)

        # help liveness analysis
        acpy.size
        bcpy.size
        s.size
        rank_ptr.size

        return (x, res, rank, s[:minmn])

    return lstsq_impl

# specialisations for np.linalg.solve


def _get_solve_compute_return_impl(b):
    # gets an implementation for extracting 'x' (the solution) from
    # the 'b' scratch space
    ndim = b.ndim
    if ndim == 1:
        @jit(nopython=True)
        def oneD_impl(b):
            return b.T.ravel()
        return oneD_impl
    else:
        @jit(nopython=True)
        def twoD_impl(b):
            return b
        return twoD_impl


@overload(np.linalg.solve)
def solve_impl(a, b):
    ensure_lapack()

    _check_linalg_matrix(a, "solve")
    _check_linalg_1_or_2d_matrix(b, "solve")

    a_F_layout = a.layout == 'F'
    b_F_layout = b.layout == 'F'

    # the typing context is not easily accessible in `@overload` mode
    # so type unification etc. is done manually below
    a_np_dt = np_support.as_dtype(a.dtype)
    b_np_dt = np_support.as_dtype(b.dtype)

    np_shared_dt = np.promote_types(a_np_dt, b_np_dt)
    nb_shared_dt = np_support.from_dtype(np_shared_dt)

    # the lapack wrapper signature
    numba_xgesv_sig = types.intc(
        types.char,  # kind
        types.intp,  # n
        types.intp,  # nhrs
        types.CPointer(nb_shared_dt),  # a
        types.intp,  # lda
        types.CPointer(F_INT_nbtype),  # ipiv
        types.CPointer(nb_shared_dt),  # b
        types.intp  # ldb
    )

    # the lapack wrapper function
    numba_xgesv = types.ExternalFunction("numba_xgesv", numba_xgesv_sig)

    kind = ord(get_blas_kind(nb_shared_dt, "solve"))

    # get a specialisation for computing the number of RHS
    b_nrhs = _get_system_compute_nrhs(b)

    # check system is valid
    check_dimensionally_valid = _get_system_check_dimensionally_valid_impl(
        a, b)

    # b copy function
    b_copy_in = _get_system_copy_in_b_impl(b)

    # b return function
    b_ret = _get_solve_compute_return_impl(b)

    def solve_impl(a, b):
        n = a.shape[-1]
        nrhs = b_nrhs(b)

        # check the systems have no inf or NaN
        _check_finite_matrix(a)
        _check_finite_matrix(b)

        # check the systems are dimensionally valid
        check_dimensionally_valid(a, b)

        # a is destroyed on exit, copy it
        acpy = a.astype(np_shared_dt)
        if a_F_layout:
            acpy = np.copy(acpy)
        else:
            acpy = np.asfortranarray(acpy)

        # b is overwritten on exit with the solution, copy allocate
        bcpy = np.empty((nrhs, n), dtype=np_shared_dt).T
        # specialised copy in due to b being 1 or 2D
        b_copy_in(bcpy, b, nrhs)

        # allocate pivot array (needs to be fortran int size)
        ipiv = np.empty(n, dtype=F_INT_nptype)

        r = numba_xgesv(
            kind,  # kind
            n,  # n
            nrhs,  # nhrs
            acpy.ctypes,  # a
            n,  # lda
            ipiv.ctypes,  # ipiv
            bcpy.ctypes,  # b
            n  # ldb
        )

        if r != 0:
            if r < 0:
                fatal_error_func()
                assert 0   # unreachable

            if r > 0:
                raise np.linalg.LinAlgError(
                    "Matrix is singular to machine precision.")

        # help liveness analysis
        acpy.size
        bcpy.size
        ipiv.size

        return b_ret(bcpy)

    return solve_impl


@overload(np.linalg.pinv)
def pinv_impl(a, rcond=1.e-15):
    ensure_lapack()

    _check_linalg_matrix(a, "pinv")

    # convert typing floats to numpy floats for use in the impl
    s_type = getattr(a.dtype, "underlying_float", a.dtype)
    if s_type.bitwidth == 32:
        s_dtype = np.float32
    else:
        s_dtype = np.float64

    numba_ez_gesdd_sig = types.intc(
        types.char,              # kind
        types.char,              # jobz
        types.intp,              # m
        types.intp,              # n
        types.CPointer(a.dtype),  # a
        types.intp,              # lda
        types.CPointer(s_type),  # s
        types.CPointer(a.dtype),  # u
        types.intp,              # ldu
        types.CPointer(a.dtype),  # vt
        types.intp               # ldvt
    )

    numba_ez_gesdd = types.ExternalFunction("numba_ez_gesdd",
                                            numba_ez_gesdd_sig)

    dt = np_support.from_dtype(np_support.as_dtype(a.dtype))
    numba_xxgemm_sig = types.intc(
        types.char,              # kind
        types.char,              # TRANSA
        types.char,              # TRANSB
        types.intp,              # M
        types.intp,              # N
        types.intp,              # K
        types.CPointer(a.dtype),  # ALPHA
        types.CPointer(a.dtype),  # A
        types.intp,              # LDA
        types.CPointer(a.dtype),  # B
        types.intp,              # LDB
        types.CPointer(a.dtype),  # BETA
        types.CPointer(a.dtype),  # C
        types.intp               # LDC
    )

    numba_xxgemm = types.ExternalFunction("numba_xxgemm",
                                          numba_xxgemm_sig)

    F_layout = a.layout == 'F'

    kind = ord(get_blas_kind(a.dtype, "pinv"))
    JOB = ord('S')

    # need conjugate transposes
    TRANSA = ord('C')
    TRANSB = ord('C')

    def pinv_impl(a, rcond=1.e-15):

        # The idea is to build the pseudo-inverse via inverting the singular
        # value decomposition of a matrix `A`. Mathematically, this is roughly
        # A = U*S*V^H        [The SV decomposition of A]
        # A^+ = V*(S^+)*U^H  [The inverted SV decomposition of A]
        # where ^+ is pseudo inversion and ^H is Hermitian transpose.
        # As V and U are unitary, their inverses are simply their Hermitian
        # transpose. S has singular values on its diagonal and zero elsewhere,
        # it is inverted trivially by reciprocal of the diagonal values with
        # the exception that zero singular values remain as zero.
        #
        # The practical implementation can take advantage of a few things:
        # * A is destroyed by the SVD algorithm from LAPACK so a copy is
        #   required, this memory is exactly the right size in which to return
        #   the pseudo-inverse and so can be resued for this purpose.
        # * The pseudo-inverse of S can be applied to either V or U^H, this
        #   then leaves a GEMM operation to compute the inverse via either:
        #   A^+ = (V*(S^+))*U^H
        #   or
        #   A^+ = V*((S^+)*U^H)
        #   however application of S^+ to V^H or U is more convenient as they
        #   are the result of the SVD algorithm. The application of the
        #   diagonal system is just a matrix multiplication which results in a
        #   row/column scaling (direction depending). To save effort, this
        #   "matrix multiplication" is applied to the smallest of U or V^H and
        #   only up to the point of "cut-off" (see next note) just as a direct
        #   scaling.
        # * The cut-off level for application of S^+ can be used to reduce
        #   total effort, this cut-off can come via rcond or may just naturally
        #   be present as a result of zeros in the singular values. Regardless
        #   there's no need to multiply by zeros in the application of S^+ to
        #   V^H or U as above. Further, the GEMM operation can be shrunk in
        #   effort by noting that the possible zero block generated by the
        #   presence of zeros in S^+ has no effect apart from wasting cycles as
        #   it is all fmadd()s where one operand is zero. The inner dimension
        #   of the GEMM operation can therefore be set as shrunk accordingly!

        n = a.shape[-1]
        m = a.shape[-2]

        _check_finite_matrix(a)

        if F_layout:
            acpy = np.copy(a)
        else:
            acpy = np.asfortranarray(a)

        minmn = min(m, n)

        u = np.empty((minmn, m), dtype=a.dtype)
        s = np.empty(minmn, dtype=s_dtype)
        vt = np.empty((n, minmn), dtype=a.dtype)

        r = numba_ez_gesdd(
            kind,        # kind
            JOB,         # job
            m,           # m
            n,           # n
            acpy.ctypes,  # a
            m,           # lda
            s.ctypes,    # s
            u.ctypes,    # u
            m,           # ldu
            vt.ctypes,   # vt
            minmn        # ldvt
        )
        _handle_err_maybe_convergence_problem(r)

        # Invert singular values under threshold. Also find the index of
        # the threshold value as this is the upper limit for the application
        # of the inverted singular values. Finding this value saves
        # multiplication by a block of zeros that would be created by the
        # application of these values to either U or V^H ahead of multiplying
        # them together. This is done by simply in BLAS parlance via
        # restricting the `k` dimension in `xgemm` whilst keeping the leading
        # dimensions correct.

        cut_at = s[0] * rcond
        cut_idx = 0
        for k in range(minmn):
            if s[k] > cut_at:
                s[k] = 1. / s[k]
                cut_idx = k
        cut_idx += 1

        zero = np.array(0., dtype=a.dtype)
        one = np.array(1., dtype=a.dtype)

        # Use cut_idx so there's no scaling by 0, instead just ignore the
        # offending zero block in the GEMM later!
        if m >= n:
            # U is largest so apply A^+ to V^H.
            for i in range(n):
                for j in range(cut_idx):
                    vt[i, j] = vt[i, j] * s[j]
        else:
            # V^H is largest so apply S^+ to U.
            for i in range(cut_idx):
                s_local = s[i]
                for j in range(minmn):
                    u[i, j] = u[i, j] * s_local

        # Do (v^H)^H*U^H (obviously one of the matrices includes the S^+
        # scaling) and write back to acpy. Note the innner dimension of cut_idx
        # taking account of the possible zero block.
        # We can accumulate the pinv in acpy, given we had to create it
        # for use in the SVD, and it is now redundant and the right size
        # but wrong shape.

        r = numba_xxgemm(
            kind,
            TRANSA,      # TRANSA
            TRANSB,      # TRANSB
            n,           # M
            m,           # N
            cut_idx,     # K
            one.ctypes,  # ALPHA
            vt.ctypes,   # A
            minmn,       # LDA
            u.ctypes,    # B
            m,           # LDB
            zero.ctypes,  # BETA
            acpy.ctypes,  # C
            n            # LDC
        )

        # help liveness analysis
        acpy.size
        vt.size
        u.size
        s.size
        one.size
        zero.size

        return acpy.T.ravel().reshape(a.shape).T

    return pinv_impl
