from __future__ import print_function, division, absolute_import

import math
import re
import sys
import types as pytypes
import dis

import numpy as np

import numba
from numba import unittest_support as unittest
from numba import njit, prange
from numba import compiler, typing
from numba.targets import cpu
from numba import types
from numba.targets.registry import cpu_target
from numba import config
from numba.annotations import type_annotations
from numba.ir_utils import copy_propagate, apply_copy_propagate, get_name_var_table, remove_dels, remove_dead
from numba import ir
from numba.compiler import compile_isolated, Flags


class TestParforsBase(unittest.TestCase):
    """
    Base class for testing parfors.
    Provides functions for compilation and three way comparison between
    python functions, njit'd functions and parfor njit'd functions.
    """

    def __init__(self, *args):
        # flags for njit()
        self.cflags = Flags()
        self.cflags.set('nrt')

        # flags for njit(parallel=True)
        self.pflags = Flags()
        self.pflags.set('auto_parallel')
        self.pflags.set('nrt')
        super(TestParforsBase, self).__init__(*args)

    def _compile_this(self, func, sig, flags):
        return compile_isolated(func, sig, flags=flags)

    def compile_parallel(self, func, sig):
        return self._compile_this(func, sig, flags=self.pflags)

    def compile_njit(self, func, sig):
        return self._compile_this(func, sig, flags=self.cflags)

    def compile_all(self, pyfunc, *args, **kwargs):
        sig = tuple([numba.typeof(x) for x in args])

        # compile the prange injected function
        cpfunc = self.compile_parallel(pyfunc, sig)

        # compile a standard njit of the original function
        cfunc = self.compile_njit(pyfunc, sig)

        return cfunc, cpfunc

    def check_three_way(self, pyfunc, cfunc, cpfunc, *args, **kwargs):
        """
        Checks python, njit and parfor impls produce the same result.

        Arguments:
            args - arguments for the function being tested
            kwargs - to pass to np.testing.assert_almost_equal
                     'decimal' is supported.
        """

        # python result
        py_expected = pyfunc(*args)

        # njit result
        njit_output = cfunc.entry_point(*args)

        # parfor result
        parfor_output = cpfunc.entry_point(*args)

        np.testing.assert_almost_equal(njit_output, py_expected, **kwargs)
        np.testing.assert_almost_equal(parfor_output, py_expected, **kwargs)

        # make sure parfor set up scheduling
        self.assertIn('@do_scheduling', cpfunc.library.get_llvm_str())


def test1(sptprice, strike, rate, volatility, timev):
    # blackscholes example
    logterm = np.log(sptprice / strike)
    powterm = 0.5 * volatility * volatility
    den = volatility * np.sqrt(timev)
    d1 = (((rate + powterm) * timev) + logterm) / den
    d2 = d1 - den
    NofXd1 = 0.5 + 0.5 * 2.0 * d1
    NofXd2 = 0.5 + 0.5 * 2.0 * d2
    futureValue = strike * np.exp(- rate * timev)
    c1 = futureValue * NofXd2
    call = sptprice * NofXd1 - c1
    put = call - futureValue + sptprice
    return put


def test2(Y, X, w, iterations):
    # logistic regression example
    for i in range(iterations):
        w -= np.dot(((1.0 / (1.0 + np.exp(-Y * np.dot(X, w))) - 1.0) * Y), X)
    return w


def countParfors(func_ir):
    ret_count = 0

    for label, block in func_ir.blocks.items():
        for i, inst in enumerate(block.body):
            if isinstance(inst, numba.parfor.Parfor):
                ret_count += 1

    return ret_count


class TestPipeline(object):
    def __init__(self, typingctx, targetctx, args, test_ir):
        typingctx.refresh()
        targetctx.refresh()
        self.typingctx = typingctx
        self.targetctx = targetctx
        self.args = args
        self.func_ir = test_ir
        self.typemap = None
        self.return_type = None
        self.calltypes = None


class TestParfors(TestParforsBase):

    def __init__(self, *args):
        TestParforsBase.__init__(self, *args)
        # these are used in the mass of simple tests
        m = np.reshape(np.arange(12.), (3, 4))
        self.simple_args = [np.arange(3.), np.arange(4.), m, m.T]

    def check(self, pyfunc, *args, **kwargs):
        cfunc, cpfunc = self.compile_all(pyfunc, *args)
        self.check_three_way(pyfunc, cfunc, cpfunc, *args, **kwargs)

    def test_arraymap(self):
        def test_impl(a, x, y):
            return a * x + y

        A = np.linspace(0, 1, 10)
        X = np.linspace(2, 1, 10)
        Y = np.linspace(1, 2, 10)

        self.check(test_impl, A, X, Y)

    def test_mvdot(self):
        def test_impl(a, v):
            return np.dot(a, v)

        A = np.linspace(0, 1, 20).reshape(2, 10)
        v = np.linspace(2, 1, 10)

        self.check(test_impl, A, v)

    def test_2d_parfor(self):
        def test_impl():
            X = np.ones((10, 12))
            Y = np.zeros((10, 12))
            return np.sum(X + Y)

        self.check(test_impl)

    def test_pi(self):
        def test_impl(n):
            x = 2 * np.random.ranf(n) - 1
            y = 2 * np.random.ranf(n) - 1
            return 4 * np.sum(x**2 + y**2 < 1) / n

        self.check(test_impl, 100000, decimal=1)

    def test_test1(self):
        typingctx = typing.Context()
        targetctx = cpu.CPUContext(typingctx)
        test_ir = compiler.run_frontend(test1)
        with cpu_target.nested_context(typingctx, targetctx):
            one_arg = numba.types.npytypes.Array(
                numba.types.scalars.Float(name="float64"), 1, 'C')
            args = (one_arg, one_arg, one_arg, one_arg, one_arg)
            tp = TestPipeline(typingctx, targetctx, args, test_ir)

            numba.rewrites.rewrite_registry.apply(
                'before-inference', tp, tp.func_ir)

            tp.typemap, tp.return_type, tp.calltypes = compiler.type_inference_stage(
                tp.typingctx, tp.func_ir, tp.args, None)

            type_annotation = type_annotations.TypeAnnotation(
                func_ir=tp.func_ir,
                typemap=tp.typemap,
                calltypes=tp.calltypes,
                lifted=(),
                lifted_from=None,
                args=tp.args,
                return_type=tp.return_type,
                html_output=config.HTML)

            numba.rewrites.rewrite_registry.apply(
                'after-inference', tp, tp.func_ir)

            parfor_pass = numba.parfor.ParforPass(
                tp.func_ir, tp.typemap, tp.calltypes, tp.return_type)
            parfor_pass.run()
            self.assertTrue(countParfors(test_ir) == 1)

    def test_test2(self):
        typingctx = typing.Context()
        targetctx = cpu.CPUContext(typingctx)
        test_ir = compiler.run_frontend(test2)
        with cpu_target.nested_context(typingctx, targetctx):
            oneD_arg = numba.types.npytypes.Array(
                numba.types.scalars.Float(name="float64"), 1, 'C')
            twoD_arg = numba.types.npytypes.Array(
                numba.types.scalars.Float(name="float64"), 2, 'C')
            args = (oneD_arg, twoD_arg, oneD_arg, types.int64)
            tp = TestPipeline(typingctx, targetctx, args, test_ir)

            numba.rewrites.rewrite_registry.apply(
                'before-inference', tp, tp.func_ir)

            tp.typemap, tp.return_type, tp.calltypes = compiler.type_inference_stage(
                tp.typingctx, tp.func_ir, tp.args, None)

            type_annotation = type_annotations.TypeAnnotation(
                func_ir=tp.func_ir,
                typemap=tp.typemap,
                calltypes=tp.calltypes,
                lifted=(),
                lifted_from=None,
                args=tp.args,
                return_type=tp.return_type,
                html_output=config.HTML)

            numba.rewrites.rewrite_registry.apply(
                'after-inference', tp, tp.func_ir)

            parfor_pass = numba.parfor.ParforPass(
                tp.func_ir, tp.typemap, tp.calltypes, tp.return_type)
            parfor_pass.run()
            self.assertTrue(countParfors(test_ir) == 1)

    def test_simple1(self):
        def test_impl():
            return np.ones(())
        with self.assertRaises(AssertionError) as raises:
            self.check(test_impl)
        self.assertIn("\'@do_scheduling\' not found", str(raises.exception))

    def test_simple2(self):
        def test_impl():
            return np.ones((1,))
        self.check(test_impl)

    def test_simple3(self):
        def test_impl():
            return np.ones((1, 2))
        self.check(test_impl)

    def test_simple4(self):
        def test_impl():
            return np.ones(1)
        self.check(test_impl)

    def test_simple5(self):
        def test_impl():
            return np.ones([1])
        self.check(test_impl)

    #TODO: Fails
    @unittest.skip('list comp ctor patch needed')
    def test_simple5(self):
        def test_impl():
            return np.ones([x for x in range(3)])
        self.check(test_impl)

    def test_simple6(self):
        def test_impl():
            return np.ones((1, 2), dtype=np.complex128)
        self.check(test_impl)

    def test_simple7(self):
        def test_impl():
            return np.ones((1, 2)) + np.ones((1, 2))
        self.check(test_impl)

    def test_simple8(self):
        def test_impl():
            return np.ones((1, 1))
        self.check(test_impl)

    def test_simple9(self):
        def test_impl():
            return np.ones((0, 0))
        self.check(test_impl)

    def test_simple10(self):
        def test_impl():
            return np.ones((10, 10)) + 1.
        self.check(test_impl)

    def test_simple11(self):
        def test_impl():
            return np.ones((10, 10)) + np.complex128(1.)
        self.check(test_impl)

    def test_simple12(self):
        def test_impl():
            return np.complex128(1.)
        with self.assertRaises(AssertionError) as raises:
            self.check(test_impl)
        self.assertIn("\'@do_scheduling\' not found", str(raises.exception))

    def test_simple13(self):
        def test_impl():
            return np.ones((10, 10))[0::20]
        self.check(test_impl)

    def test_simple14(self):
        def test_impl(v1, v2, m1, m2):
            return v1 + v1
        self.check(test_impl, *self.simple_args)

    def test_simple15(self):
        def test_impl(v1, v2, m1, m2):
            return m1 + m1
        self.check(test_impl, *self.simple_args)

    def test_simple16(self):
        def test_impl(v1, v2, m1, m2):
            return m2 + v1
        self.check(test_impl, *self.simple_args)

    def test_simple17(self):
        def test_impl(v1, v2, m1, m2):
            return m1 + np.linalg.svd(m2)[0][:-1, :]
        self.check(test_impl, *self.simple_args)

    def test_simple18(self):
        def test_impl(v1, v2, m1, m2):
            return np.dot(m1, v2)
        self.check(test_impl, *self.simple_args)

    def test_simple19(self):
        def test_impl(v1, v2, m1, m2):
            return np.dot(m1, m2)
        # gemm is left to BLAS
        with self.assertRaises(AssertionError) as raises:
            self.check(test_impl, *self.simple_args)
        self.assertIn("\'@do_scheduling\' not found", str(raises.exception))

    #TODO: Fails
    def test_simple20(self):
        def test_impl(v1, v2, m1, m2):
            return np.dot(v1, v1)
        self.check(test_impl, *self.simple_args)

    def test_simple21(self):
        def test_impl(v1, v2, m1, m2):
            return np.sum(v1 + v1)
        self.check(test_impl, *self.simple_args)

    def test_simple22(self):
        def test_impl(v1, v2, m1, m2):
            x = 2 * v1
            y = 2 * v1
            return 4 * np.sum(x**2 + y**2 < 1) / 10
        self.check(test_impl, *self.simple_args)


class TestPrange(TestParforsBase):

    def prange_tester(self, pyfunc, *args, patch_instance=None, **kwargs):
        """
        The `prange` tester
        This is a hack. It basically switches out range calls for prange.
        It does this by copying the live code object of a function
        containing 'range' then copying the .co_names and mutating it so
        that 'range' is replaced with 'prange'. It then creates a new code
        object containing the mutation and instantiates a function to contain
        it. At this point three results are created:
        1. The result of calling the original python function.
        2. The result of calling a njit compiled version of the original
            python function.
        3. The result of calling a njit(parallel=True) version of the mutated
           function containing `prange`.
        The three results are then compared and the `prange` based function's
        llvm_ir is inspected to ensure the scheduler code is present.

        Arguments:
         pyfunc - the python function to test
         patch_instance - iterable containing which instances of `range` to
                          replace.

        Example:
            def foo():
                acc = 0
                for x in range(5):
                    for y in range(10):
                        acc +=1
                return acc

            # calling as
            prange_tester(foo)
            # will test code equivalent to
            # def foo():
            #     acc = 0
            #     for x in prange(5): # <- changed
            #         for y in prange(10): # <- changed
            #             acc +=1
            #     return acc

            # calling as
            prange_tester(foo, patch_instance=[1])
            # will test code equivalent to
            # def foo():
            #     acc = 0
            #     for x in range(5): # <- outer loop (0) unchanged
            #         for y in prange(10): # <- inner loop (1) changed
            #             acc +=1
            #     return acc

        """

        pyfunc_code = pyfunc.__code__

        prange_names = list(pyfunc_code.co_names)

        if patch_instance is None:
            # patch all instances, cheat by just switching
            # range for prange
            prange_names = tuple([x if x != 'range' else 'prange'
                                  for x in pyfunc_code.co_names])
            new_code = bytes(pyfunc_code.co_code)
        else:
            # patch specified instances...
            # find where 'range' is in co_names
            range_idx = pyfunc_code.co_names.index('range')
            range_locations = []
            # look for LOAD_GLOBALs that point to 'range'
            for l, b in enumerate(pyfunc_code.co_code):
                if b == dis.opmap['LOAD_GLOBAL']:
                    if pyfunc_code.co_code[l + 1] == range_idx:
                        range_locations.append(l + 1)
            # add in 'prange' ref
            prange_names.append('prange')
            prange_names = tuple(prange_names)
            prange_idx = len(prange_names) - 1
            new_code = bytearray(pyfunc_code.co_code)
            assert len(patch_instance) <= len(range_locations)
            # patch up the new byte code
            for i in patch_instance:
                idx = range_locations[i]
                new_code[idx] = prange_idx
            new_code = bytes(new_code)

        # create new code parts
        co_args = [pyfunc_code.co_argcount]
        if sys.version_info > (3, 0):
            co_args.append(pyfunc_code.co_kwonlyargcount)
        co_args.extend([pyfunc_code.co_nlocals,
                        pyfunc_code.co_stacksize,
                        pyfunc_code.co_flags,
                        new_code,
                        pyfunc_code.co_consts,
                        prange_names,
                        pyfunc_code.co_varnames,
                        pyfunc_code.co_filename,
                        pyfunc_code.co_name,
                        pyfunc_code.co_firstlineno,
                        pyfunc_code.co_lnotab,
                        pyfunc_code.co_freevars,
                        pyfunc_code.co_cellvars
                        ])

        # create code object with prange mutation
        prange_code = pytypes.CodeType(*co_args)

        # get function
        pfunc = pytypes.FunctionType(prange_code, globals())

        # Compile functions
        # compile a standard njit of the original function
        sig = tuple([numba.typeof(x) for x in args])
        cfunc = self.compile_njit(pyfunc, sig)

        # compile the prange injected function
        cpfunc = self.compile_parallel(pfunc, sig)

        # compare
        self.check_three_way(pyfunc, cfunc, cpfunc, *args, **kwargs)

    def test_prange01(self):
        def test_impl():
            n = 4
            A = np.zeros(n)
            for i in range(n):
                A[i] = 2.0 * i
            return A
        self.prange_tester(test_impl)

    def test_prange02(self):
        def test_impl():
            n = 4
            A = np.zeros(n - 1)
            for i in range(1, n):
                A[i - 1] = 2.0 * i
            return A
        self.prange_tester(test_impl)

    def test_prange03(self):
        def test_impl():
            s = 0
            for i in range(10):
                s += 2
            return s
        self.prange_tester(test_impl)

    def test_prange04(self):
        def test_impl():
            a = 2
            b = 3
            A = np.empty(4)
            for i in range(4):
                if i == a:
                    A[i] = b
                else:
                    A[i] = 0
            return A
        self.prange_tester(test_impl)

    def test_prange05(self):
        def test_impl():
            n = 4
            A = np.ones((n), dtype=np.float64)
            s = 0
            for i in range(1, n - 1, 1):
                s += A[i]
            return s
        self.prange_tester(test_impl)

    def test_prange06(self):
        def test_impl():
            n = 4
            A = np.ones((n), dtype=np.float64)
            s = 0
            for i in range(1, 1, 1):
                s += A[i]
            return s
        self.prange_tester(test_impl)

    def test_prange07(self):
        def test_impl():
            n = 4
            A = np.ones((n), dtype=np.float64)
            s = 0
            for i in range(n):
                s += A[i]
            return s
        self.prange_tester(test_impl)

    def test_prange08(self):
        def test_impl():
            n = 4
            A = np.ones((n))
            acc = 0
            for i in range(len(A)):
                for j in range(len(A)):
                    acc += A[i]
            return acc

        test_impl()

    def test_prange09(self):
        def test_impl():
            n = 4
            acc = 0
            for i in range(n):
                for j in range(n):
                    acc += 1
            return acc
        # patch inner loop to 'prange'
        self.prange_tester(test_impl, patch_instance=[1])

    def test_prange10(self):
        def test_impl():
            n = 4
            acc2 = 0
            for j in range(n):
                acc1 = 0
                for i in range(n):
                    acc1 += 1
                acc2 += acc1
            return acc2
        # patch outer loop to 'prange'
        self.prange_tester(test_impl, patch_instance=[0])

    #TODO: Fails
    def test_prange11(self):
        # List comprehension with a `prange` fails with
        # `No definition for lowering <class 'numba.parfor.prange'>(int64,) -> range_state_int64`.
        def test_impl():
            n = 4
            return [np.sin(j) for j in range(n)]
        self.prange_tester(test_impl)

    def test_prange12(self):
        def test_impl():
            acc = 0
            n = 4
            X = np.ones(n)
            for i in range(-len(X)):
                acc += X[i]
            return acc
        self.prange_tester(test_impl)

    def test_kde_example(self):
        def test_impl(X):
            # KDE example
            b = 0.5
            points = np.array([-1.0, 2.0, 5.0])
            N = points.shape[0]
            n = X.shape[0]
            exps = 0
            for i in range(n):
                p = X[i]
                d = (-(p - points)**2) / (2 * b**2)
                m = np.min(d)
                exps += m - np.log(b * N) + np.log(np.sum(np.exp(d - m)))
            return exps

        n = 128
        X = np.random.ranf(n)
        self.prange_tester(test_impl, X)


if __name__ == "__main__":
    unittest.main()
