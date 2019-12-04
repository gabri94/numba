from __future__ import print_function, absolute_import, division

import numpy as np

from numba.tests.support import TestCase, MemoryLeakMixin
from numba import njit, types, typed, ir, errors, literal_unroll
from numba.testing import unittest
from numba.extending import overload
from numba.compiler_machinery import PassManager, register_pass, FunctionPass
from numba.compiler import CompilerBase
from numba.untyped_passes import (TranslateByteCode, FixupArgs, IRProcessing,
                                  SimplifyCFG, IterLoopCanonicalization,
                                  MixedContainerUnroller)
from numba.typed_passes import (NopythonTypeInference, IRLegalization,
                                NoPythonBackend, PartialTypeInference)
from numba.ir_utils import (compute_cfg_from_blocks, find_topo_order,
                            flatten_labels)


class TestLiteralTupleInterpretation(MemoryLeakMixin, TestCase):

    def check(self, func, var):
        cres = func.overloads[func.signatures[0]]
        ty = cres.fndesc.typemap[var]
        self.assertTrue(isinstance(ty, types.Tuple))
        for subty in ty:
            self.assertTrue(isinstance(subty, types.Literal), "non literal")

    def test_homogeneous_literal(self):
        @njit
        def foo():
            x = (1, 2, 3)
            return x[1]

        self.assertEqual(foo(), foo.py_func())
        self.check(foo, 'x')

    def test_heterogeneous_literal(self):
        @njit
        def foo():
            x = (1, 2, 3, 'a')
            return x[3]

        self.assertEqual(foo(), foo.py_func())
        self.check(foo, 'x')

    def test_non_literal(self):
        @njit
        def foo():
            x = (1, 2, 3, 'a', 1j)
            return x[4]

        self.assertEqual(foo(), foo.py_func())
        with self.assertRaises(AssertionError) as e:
            self.check(foo, 'x')

        self.assertIn("non literal", str(e.exception))


@register_pass(mutates_CFG=False, analysis_only=True)
class PreserveIR(FunctionPass):
    _name = "preserve_ir"

    def __init__(self):
        FunctionPass.__init__(self)

    def run_pass(self, state):
        state.metadata['func_ir'] = state.func_ir
        return False


@register_pass(mutates_CFG=False, analysis_only=False)
class ResetTypeInfo(FunctionPass):
    _name = "reset_the_type_information"

    def __init__(self):
        FunctionPass.__init__(self)

    def run_pass(self, state):
        state.typemap = None
        state.return_type = None
        state.calltypes = None
        return True


class TestLoopCanonicalisation(MemoryLeakMixin, TestCase):

    def get_pipeline(use_canonicaliser, use_partial_typing=False):
        class NewCompiler(CompilerBase):

            def define_pipelines(self):
                pm = PassManager("custom_pipeline")

                # untyped
                pm.add_pass(TranslateByteCode, "analyzing bytecode")
                pm.add_pass(IRProcessing, "processing IR")
                if use_partial_typing:
                    pm.add_pass(PartialTypeInference, "do partial typing")
                if use_canonicaliser:
                    pm.add_pass(IterLoopCanonicalization, "Canonicalise loops")
                pm.add_pass(SimplifyCFG, "Simplify the CFG")

                # typed
                if use_partial_typing:
                    pm.add_pass(ResetTypeInfo, "resets the type info state")

                pm.add_pass(NopythonTypeInference, "nopython frontend")

                # legalise
                pm.add_pass(IRLegalization, "ensure IR is legal")

                # preserve
                pm.add_pass(PreserveIR, "save IR for later inspection")

                # lower
                pm.add_pass(NoPythonBackend, "nopython mode backend")

                # finalise the contents
                pm.finalize()

                return [pm]
        return NewCompiler

    # generate variants
    LoopIgnoringCompiler = get_pipeline(False)
    LoopCanonicalisingCompiler = get_pipeline(True)
    TypedLoopCanonicalisingCompiler = get_pipeline(True, True)

    def test_simple_loop_in_depth(self):
        """ This heavily checks a simple loop transform """

        def get_info(pipeline):
            @njit(pipeline_class=pipeline)
            def foo(tup):
                acc = 0
                for i in tup:
                    acc += i
                return acc

            x = (1, 2, 3)
            self.assertEqual(foo(x), foo.py_func(x))
            cres = foo.overloads[foo.signatures[0]]
            func_ir = cres.metadata['func_ir']
            return func_ir, cres.fndesc

        ignore_loops_ir, ignore_loops_fndesc = \
            get_info(self.LoopIgnoringCompiler)
        canonicalise_loops_ir, canonicalise_loops_fndesc = \
            get_info(self.LoopCanonicalisingCompiler)

        # check CFG is the same
        def compare_cfg(a, b):
            a_cfg = compute_cfg_from_blocks(flatten_labels(a.blocks))
            b_cfg = compute_cfg_from_blocks(flatten_labels(b.blocks))
            self.assertEqual(a_cfg, b_cfg)

        compare_cfg(ignore_loops_ir, canonicalise_loops_ir)

        # check there's three more call types in the canonicalised one:
        # len(tuple arg)
        # range(of the len() above)
        # getitem(tuple arg, index)
        self.assertEqual(len(ignore_loops_fndesc.calltypes) + 3,
                         len(canonicalise_loops_fndesc.calltypes))

        def find_getX(fd, op):
            return [x for x in fd.calltypes.keys()
                    if isinstance(x, ir.Expr) and x.op == op]

        il_getiters = find_getX(ignore_loops_fndesc, "getiter")
        self.assertEqual(len(il_getiters), 1)  # tuple iterator

        cl_getiters = find_getX(canonicalise_loops_fndesc, "getiter")
        self.assertEqual(len(cl_getiters), 1)  # loop range iterator

        cl_getitems = find_getX(canonicalise_loops_fndesc, "getitem")
        self.assertEqual(len(cl_getitems), 1)  # tuple getitem induced by loop

        # check the value of the untransformed IR getiter is now the value of
        # the transformed getitem
        self.assertEqual(il_getiters[0].value.name, cl_getitems[0].value.name)

        # check the type of the transformed IR getiter is a range iter
        range_inst = canonicalise_loops_fndesc.calltypes[cl_getiters[0]].args[0]
        self.assertTrue(isinstance(range_inst, types.RangeType))

    def test_transform_scope(self):
        """ This checks the transform, when there's no typemap, will happily
        transform a loop on something that's not tuple-like
        """
        def get_info(pipeline):
            @njit(pipeline_class=pipeline)
            def foo():
                acc = 0
                for i in [1, 2, 3]:
                    acc += i
                return acc

            self.assertEqual(foo(), foo.py_func())
            cres = foo.overloads[foo.signatures[0]]
            func_ir = cres.metadata['func_ir']
            return func_ir, cres.fndesc

        ignore_loops_ir, ignore_loops_fndesc = \
            get_info(self.LoopIgnoringCompiler)
        canonicalise_loops_ir, canonicalise_loops_fndesc = \
            get_info(self.LoopCanonicalisingCompiler)

        # check CFG is the same
        def compare_cfg(a, b):
            a_cfg = compute_cfg_from_blocks(flatten_labels(a.blocks))
            b_cfg = compute_cfg_from_blocks(flatten_labels(b.blocks))
            self.assertEqual(a_cfg, b_cfg)

        compare_cfg(ignore_loops_ir, canonicalise_loops_ir)

        # check there's three more call types in the canonicalised one:
        # len(literal list)
        # range(of the len() above)
        # getitem(literal list arg, index)
        self.assertEqual(len(ignore_loops_fndesc.calltypes) + 3,
                         len(canonicalise_loops_fndesc.calltypes))

        def find_getX(fd, op):
            return [x for x in fd.calltypes.keys()
                    if isinstance(x, ir.Expr) and x.op == op]

        il_getiters = find_getX(ignore_loops_fndesc, "getiter")
        self.assertEqual(len(il_getiters), 1)  # list iterator

        cl_getiters = find_getX(canonicalise_loops_fndesc, "getiter")
        self.assertEqual(len(cl_getiters), 1)  # loop range iterator

        cl_getitems = find_getX(canonicalise_loops_fndesc, "getitem")
        self.assertEqual(len(cl_getitems), 1)  # list getitem induced by loop

        # check the value of the untransformed IR getiter is now the value of
        # the transformed getitem
        self.assertEqual(il_getiters[0].value.name, cl_getitems[0].value.name)

        # check the type of the transformed IR getiter is a range iter
        range_inst = canonicalise_loops_fndesc.calltypes[cl_getiters[0]].args[0]
        self.assertTrue(isinstance(range_inst, types.RangeType))

    @unittest.skip("Waiting for pass to be enabled for all tuples")
    def test_influence_of_typed_transform(self):
        """ This heavily checks a typed transformation only impacts tuple
        induced loops"""

        def get_info(pipeline):
            @njit(pipeline_class=pipeline)
            def foo(tup):
                acc = 0
                for i in range(4):
                    for y in tup:
                        for j in range(3):
                            acc += 1
                return acc

            x = (1, 2, 3)
            self.assertEqual(foo(x), foo.py_func(x))
            cres = foo.overloads[foo.signatures[0]]
            func_ir = cres.metadata['func_ir']
            return func_ir, cres.fndesc

        ignore_loops_ir, ignore_loops_fndesc = \
            get_info(self.LoopIgnoringCompiler)
        canonicalise_loops_ir, canonicalise_loops_fndesc = \
            get_info(self.TypedLoopCanonicalisingCompiler)

        # check CFG is the same
        def compare_cfg(a, b):
            a_cfg = compute_cfg_from_blocks(flatten_labels(a.blocks))
            b_cfg = compute_cfg_from_blocks(flatten_labels(b.blocks))
            self.assertEqual(a_cfg, b_cfg)

        compare_cfg(ignore_loops_ir, canonicalise_loops_ir)

        # check there's three more call types in the canonicalised one:
        # len(tuple arg)
        # range(of the len() above)
        # getitem(tuple arg, index)
        self.assertEqual(len(ignore_loops_fndesc.calltypes) + 3,
                         len(canonicalise_loops_fndesc.calltypes))

        def find_getX(fd, op):
            return [x for x in fd.calltypes.keys()
                    if isinstance(x, ir.Expr) and x.op == op]

        il_getiters = find_getX(ignore_loops_fndesc, "getiter")
        self.assertEqual(len(il_getiters), 3)  # 1 * tuple + 2 * loop range

        cl_getiters = find_getX(canonicalise_loops_fndesc, "getiter")
        self.assertEqual(len(cl_getiters), 3)  # 3 * loop range iterator

        cl_getitems = find_getX(canonicalise_loops_fndesc, "getitem")
        self.assertEqual(len(cl_getitems), 1)  # tuple getitem induced by loop

        # check the value of the untransformed IR getiter is now the value of
        # the transformed getitem
        self.assertEqual(il_getiters[1].value.name, cl_getitems[0].value.name)

        # check the type of the transformed IR getiter's are all range iter
        for x in cl_getiters:
            range_inst = canonicalise_loops_fndesc.calltypes[x].args[0]
            self.assertTrue(isinstance(range_inst, types.RangeType))

    def test_influence_of_typed_transform_literal_unroll(self):
        """ This heavily checks a typed transformation only impacts loops with
        literal_unroll marker"""

        def get_info(pipeline):
            @njit(pipeline_class=pipeline)
            def foo(tup):
                acc = 0
                for i in range(4):
                    for y in literal_unroll(tup):
                        for j in range(3):
                            acc += 1
                return acc

            x = (1, 2, 3)
            self.assertEqual(foo(x), foo.py_func(x))
            cres = foo.overloads[foo.signatures[0]]
            func_ir = cres.metadata['func_ir']
            return func_ir, cres.fndesc

        ignore_loops_ir, ignore_loops_fndesc = \
            get_info(self.LoopIgnoringCompiler)
        canonicalise_loops_ir, canonicalise_loops_fndesc = \
            get_info(self.TypedLoopCanonicalisingCompiler)

        # check CFG is the same
        def compare_cfg(a, b):
            a_cfg = compute_cfg_from_blocks(flatten_labels(a.blocks))
            b_cfg = compute_cfg_from_blocks(flatten_labels(b.blocks))
            self.assertEqual(a_cfg, b_cfg)

        compare_cfg(ignore_loops_ir, canonicalise_loops_ir)

        # check there's three more call types in the canonicalised one:
        # len(tuple arg)
        # range(of the len() above)
        # getitem(tuple arg, index)
        self.assertEqual(len(ignore_loops_fndesc.calltypes) + 3,
                         len(canonicalise_loops_fndesc.calltypes))

        def find_getX(fd, op):
            return [x for x in fd.calltypes.keys()
                    if isinstance(x, ir.Expr) and x.op == op]

        il_getiters = find_getX(ignore_loops_fndesc, "getiter")
        self.assertEqual(len(il_getiters), 3)  # 1 * tuple + 2 * loop range

        cl_getiters = find_getX(canonicalise_loops_fndesc, "getiter")
        self.assertEqual(len(cl_getiters), 3)  # 3 * loop range iterator

        cl_getitems = find_getX(canonicalise_loops_fndesc, "getitem")
        self.assertEqual(len(cl_getitems), 1)  # tuple getitem induced by loop

        # check the value of the untransformed IR getiter is now the value of
        # the transformed getitem
        self.assertEqual(il_getiters[1].value.name, cl_getitems[0].value.name)

        # check the type of the transformed IR getiter's are all range iter
        for x in cl_getiters:
            range_inst = canonicalise_loops_fndesc.calltypes[x].args[0]
            self.assertTrue(isinstance(range_inst, types.RangeType))

    @unittest.skip("Waiting for pass to be enabled for all tuples")
    def test_lots_of_loops(self):
        """ This heavily checks a simple loop transform """

        def get_info(pipeline):
            @njit(pipeline_class=pipeline)
            def foo(tup):
                acc = 0
                for i in tup:
                    acc += i
                    for j in tup + (4, 5, 6):
                        acc += 1 - j
                        if j > 5:
                            break
                    else:
                        acc -= 2
                for i in tup:
                    acc -= i % 2

                return acc

            x = (1, 2, 3)
            self.assertEqual(foo(x), foo.py_func(x))
            cres = foo.overloads[foo.signatures[0]]
            func_ir = cres.metadata['func_ir']
            return func_ir, cres.fndesc

        ignore_loops_ir, ignore_loops_fndesc = \
            get_info(self.LoopIgnoringCompiler)
        canonicalise_loops_ir, canonicalise_loops_fndesc = \
            get_info(self.LoopCanonicalisingCompiler)

        # check CFG is the same
        def compare_cfg(a, b):
            a_cfg = compute_cfg_from_blocks(flatten_labels(a.blocks))
            b_cfg = compute_cfg_from_blocks(flatten_labels(b.blocks))
            self.assertEqual(a_cfg, b_cfg)

        compare_cfg(ignore_loops_ir, canonicalise_loops_ir)

        # check there's three * N more call types in the canonicalised one:
        # len(tuple arg)
        # range(of the len() above)
        # getitem(tuple arg, index)
        self.assertEqual(len(ignore_loops_fndesc.calltypes) + 3 * 3,
                         len(canonicalise_loops_fndesc.calltypes))


class TestMixedTupleUnroll(MemoryLeakMixin, TestCase):
    class DebugCompiler(CompilerBase):

        def define_pipelines(self):
            pm = PassManager("custom_pipeline")

            # untyped
            pm.add_pass(TranslateByteCode, "analyzing bytecode")
            pm.add_pass(IRProcessing, "processing IR")

            pm.add_pass(PartialTypeInference, "performs partial type inference")
            pm.add_pass(IterLoopCanonicalization,
                        "switch iter loops for range driven loops")
            pm.add_pass(
                MixedContainerUnroller,
                "performs mixed container unroll")

            # typed
            pm.add_pass(NopythonTypeInference, "nopython frontend")

            # legalise
            pm.add_pass(IRLegalization, "ensure IR is legal")

            # preserve
            pm.add_pass(PreserveIR, "save IR for later inspection")

            # lower
            pm.add_pass(NoPythonBackend, "nopython mode backend")

            # finalise the contents
            pm.finalize()

            return [pm]

    def debug(self, func):
        # use the debug compiler above with this
        cres = func.overloads[func.signatures[0]]
        func_ir = cres.metadata['func_ir']
        func_ir.dump()
        func_ir.render_dot().view()
        from pprint import pprint
        pprint(cres.fndesc.typemap)
        import pdb
        pdb.set_trace()
        pass

    def test_01(self):

        @njit
        def foo(idx, z):
            a = (12, 12.7, 3j, 4, z, 2 * z)
            acc = 0
            for i in range(len(literal_unroll(a))):
                acc += a[i]
                if acc.real < 26:
                    acc -= 1
                else:
                    break
            return acc

        f = 9
        k = f

        self.assertEqual(foo(2, k), foo.py_func(2, k))

    def test_02(self):
        # same as test_1 but without the explicit loop canonicalisation

        @njit
        def foo(idx, z):
            x = (12, 12.7, 3j, 4, z, 2 * z)
            acc = 0
            for a in literal_unroll(x):
                acc += a
                if acc.real < 26:
                    acc -= 1
                else:
                    break
            return acc

        f = 9
        k = f

        self.assertEqual(foo(2, k), foo.py_func(2, k))

    def test_03(self):
        # two unrolls
        @njit
        def foo(idx, z):
            x = (12, 12.7, 3j, 4, z, 2 * z)
            y = ('foo', z, 2 * z)
            acc = 0
            for a in literal_unroll(x):
                acc += a
                if acc.real < 26:
                    acc -= 1
                else:
                    for t in literal_unroll(y):
                        acc += t is False
                    break
            return acc

        f = 9
        k = f

        self.assertEqual(foo(2, k), foo.py_func(2, k))

    def test_04(self):
        # mixed ref counted types
        @njit
        def foo(tup):
            acc = 0
            for a in literal_unroll(tup):
                acc += a.sum()
            return acc

        n = 10
        tup = (np.ones((n,)), np.ones((n, n)), np.ones((n, n, n)))
        self.assertEqual(foo(tup), foo.py_func(tup))

    def test_05(self):
        # mix unroll and static_getitem
        @njit
        def foo(tup1, tup2):
            acc = 0
            for a in literal_unroll(tup1):
                if a == 'a':
                    acc += tup2[0].sum()
                elif a == 'b':
                    acc += tup2[1].sum()
                elif a == 'c':
                    acc += tup2[2].sum()
                elif a == 12:
                    acc += tup2[3].sum()
                elif a == 3j:
                    acc += tup2[3].sum()
                elif a == ('f',):
                    acc += tup2[3].sum()
                else:
                    raise RuntimeError("Unreachable")
            return acc

        n = 10
        tup1 = ('a', 'b', 'c', 12, 3j, ('f',))
        tup2 = (np.ones((n,)), np.ones((n, n)), np.ones((n, n, n)),
                np.ones((n, n, n, n)), np.ones((n, n, n, n, n)))
        self.assertEqual(foo(tup1, tup2), foo.py_func(tup1, tup2))

    @unittest.skip("needs more clever branch prune")
    def test_06(self):
        # This wont work because both sides of the branch need typing as neither
        # can be pruned by the current pruner
        @njit
        def foo(tup):
            acc = 0
            idx = 0
            str_buf = typed.List.empty_list(types.unicode_type)
            for a in literal_unroll(tup):
                if a == 'a':
                    str_buf.append(a)
                else:
                    acc += a
            return acc

        tup = ('a', 12)
        self.assertEqual(foo(tup), foo.py_func(tup))

    def test_07(self):
        # A mix bag of stuff as an arg to a function that unifies as `intp`.
        @njit
        def foo(tup):
            acc = 0
            for a in literal_unroll(tup):
                acc += len(a)
            return acc

        n = 10
        tup = (np.ones((n,)), np.ones((n, n)), "ABCDEFGHJI", (1, 2, 3),
               (1, 'foo', 2, 'bar'), {3, 4, 5, 6, 7})
        self.assertEqual(foo(tup), foo.py_func(tup))

    def test_08(self):
        # dispatch to functions

        @njit
        def foo(tup1, tup2):
            acc = 0
            for a in literal_unroll(tup1):
                if a == 'a':
                    acc += tup2[0]()
                elif a == 'b':
                    acc += tup2[1]()
                elif a == 'c':
                    acc += tup2[2]()
            return acc

        def gen(x):
            def impl():
                return x
            return njit(impl)

        n = 10
        tup1 = ('a', 'b', 'c', 12, 3j, ('f',))
        tup2 = (gen(1), gen(2), gen(3))
        self.assertEqual(foo(tup1, tup2), foo.py_func(tup1, tup2))

    def test_09(self):
        # illegal RHS, has a mixed tuple being index dynamically

        @njit
        def foo(tup1, tup2):
            acc = 0
            idx = 0
            for a in literal_unroll(tup1):
                if a == 'a':
                    acc += tup2[idx]
                elif a == 'b':
                    acc += tup2[idx]
                elif a == 'c':
                    acc += tup2[idx]
                idx += 1
            return idx, acc

        @njit
        def func1():
            return 1

        @njit
        def func2():
            return 2

        @njit
        def func3():
            return 3

        n = 10
        tup1 = ('a', 'b', 'c')
        tup2 = (1j, 1, 2)

        with self.assertRaises(errors.TypingError) as raises:
            foo(tup1, tup2)

        self.assertIn("Invalid use", str(raises.exception))

    def test_10(self):
        # dispatch on literals triggering @overload resolution

        def dt(value):
            if value == "apple":
                return 1
            elif value == "orange":
                return 2
            elif value == "banana":
                return 3
            elif value == 0xca11ab1e:
                return 0x5ca1ab1e + value

        @overload(dt, inline='always')
        def ol_dt(li):
            if isinstance(li, types.StringLiteral):
                value = li.literal_value
                if value == "apple":
                    def impl(li):
                        return 1
                elif value == "orange":
                    def impl(li):
                        return 2
                elif value == "banana":
                    def impl(li):
                        return 3
                return impl
            elif isinstance(li, types.IntegerLiteral):
                value = li.literal_value
                if value == 0xca11ab1e:
                    def impl(li):
                        # close over the dispatcher :)
                        return 0x5ca1ab1e + value
                    return impl

        @njit
        def foo():
            acc = 0
            for t in literal_unroll(('apple', 'orange', 'banana', 3390155550)):
                acc += dt(t)
            return acc

        self.assertEqual(foo(), foo.py_func())

    def test_11(self):

        @njit
        def foo():
            x = []
            z = ('apple', 'orange', 'banana')
            for i in range(len(literal_unroll(z))):
                t = z[i]
                if t == "apple":
                    x.append("0")
                elif t == "orange":
                    x.append(t)
                elif t == "banana":
                    x.append("2.0")
            return x

        self.assertEqual(foo(), foo.py_func())

    def test_11a(self):

        @njit
        def foo():
            x = typed.List()
            z = ('apple', 'orange', 'banana')
            for i in range(len(literal_unroll(z))):
                t = z[i]
                if t == "apple":
                    x.append("0")
                elif t == "orange":
                    x.append(t)
                elif t == "banana":
                    x.append("2.0")
            return x

        self.assertEqual(foo(), foo.py_func())

    def test_12(self):
        # unroll the same target twice
        @njit
        def foo(idx, z):
            a = (12, 12.7, 3j, 4, z, 2 * z)
            acc = 0
            for i in literal_unroll(a):
                acc += i
                if acc.real < 26:
                    acc -= 1
                else:
                    for x in literal_unroll(a):
                        acc += x
                    break
            if a[0] < 23:
                acc += 2
            return acc

        f = 9
        k = f

        self.assertEqual(foo(2, k), foo.py_func(2, k))

    def test_13(self):
        # nesting unrolls is illegal
        @njit
        def foo(idx, z):
            a = (12, 12.7, 3j, 4, z, 2 * z)
            acc = 0
            for i in literal_unroll(a):
                acc += i
                if acc.real < 26:
                    acc -= 1
                else:
                    for x in literal_unroll(a):
                        for j in literal_unroll(a):
                            acc += j
                        acc += x
                for x in literal_unroll(a):
                    acc += x
            for x in literal_unroll(a):
                acc += x
            if a[0] < 23:
                acc += 2
            return acc

        f = 9
        k = f

        with self.assertRaises(errors.UnsupportedError) as raises:
            foo(2, k)

        self.assertIn("Nesting of literal_unroll is unsupported",
                      str(raises.exception))

    def test_14(self):
        # unituple unroll can return derivative of the induction var

        @njit
        def foo():
            x = (1, 2, 3, 4)
            acc = 0
            for a in literal_unroll(x):
                acc += a
            return a

        self.assertEqual(foo(), foo.py_func())

    def test_15(self):
        # mixed tuple unroll cannot return derivative of the induction var

        @njit
        def foo(x):
            acc = 0
            for a in literal_unroll(x):
                acc += len(a)
            return a

        n = 5
        tup = (np.ones((n,)), np.ones((n, n)), "ABCDEFGHJI", (1, 2, 3),
               (1, 'foo', 2, 'bar'), {3, 4, 5, 6, 7})

        with self.assertRaises(errors.TypingError) as raises:
            foo(tup)

        self.assertIn("Cannot unify", str(raises.exception))

    def test_16(self):
        # unituple slice and unroll is ok

        def dt(value):
            if value == 1000:
                return "a"
            elif value == 2000:
                return "b"
            elif value == 3000:
                return "c"
            elif value == 4000:
                return "d"

        @overload(dt, inline='always')
        def ol_dt(li):
            if isinstance(li, types.IntegerLiteral):
                value = li.literal_value
                if value == 1000:
                    def impl(li):
                        return "a"
                elif value == 2000:
                    def impl(li):
                        return "b"
                elif value == 3000:
                    def impl(li):
                        return "c"
                elif value == 4000:
                    def impl(li):
                        return "d"
                return impl

        @njit
        def foo():
            x = (1000, 2000, 3000, 4000)
            acc = ""
            for a in literal_unroll(x[:2]):
                acc += dt(a)
            return acc

        self.assertEqual(foo(), foo.py_func())

    def test_17(self):
        # mixed tuple slice and unroll is ok

        def dt(value):
            if value == 1000:
                return "a"
            elif value == 2000:
                return "b"
            elif value == 3000:
                return "c"
            elif value == 4000:
                return "d"
            elif value == 'f':
                return "EFF"

        @overload(dt, inline='always')
        def ol_dt(li):
            if isinstance(li, types.IntegerLiteral):
                value = li.literal_value
                if value == 1000:
                    def impl(li):
                        return "a"
                elif value == 2000:
                    def impl(li):
                        return "b"
                elif value == 3000:
                    def impl(li):
                        return "c"
                elif value == 4000:
                    def impl(li):
                        return "d"
                return impl
            elif isinstance(li, types.StringLiteral):
                value = li.literal_value
                if value == 'f':
                    def impl(li):
                        return "EFF"
                    return impl

        @njit
        def foo():
            x = (1000, 2000, 3000, 'f')
            acc = ""
            for a in literal_unroll(x[1:]):
                acc += dt(a)
            return acc

        self.assertEqual(foo(), foo.py_func())


    def test_18(self):
        # unituple backwards slice
        @njit
        def foo():
            x = (1000, 2000, 3000, 4000, 5000, 6000)
            count = 0
            for a in literal_unroll(x[::-1]):
                count += 1
                if a < 3000:
                    break
            return count

        self.assertEqual(foo(), foo.py_func())

class TestConstListUnroll(MemoryLeakMixin, TestCase):

    def test_01(self):

        @njit
        def foo():
            a = [12, 12.7, 3j, 4]
            acc = 0
            for i in range(len(literal_unroll(a))):
                acc += a[i]
                if acc.real < 26:
                    acc -= 1
                else:
                    break
            return acc

        self.assertEqual(foo(), foo.py_func())

    def test_02(self):
        # same as test_1 but without the explicit loop canonicalisation

        @njit
        def foo():
            x = [12, 12.7, 3j, 4]
            acc = 0
            for a in literal_unroll(x):
                acc += a
                if acc.real < 26:
                    acc -= 1
                else:
                    break
            return acc

        self.assertEqual(foo(), foo.py_func())

    def test_03(self):
        # two unrolls
        @njit
        def foo():
            x = [12, 12.7, 3j, 4]
            y = ['foo', 8]
            acc = 0
            for a in literal_unroll(x):
                acc += a
                if acc.real < 26:
                    acc -= 1
                else:
                    for t in literal_unroll(y):
                        acc += t is False
                    break
            return acc

        self.assertEqual(foo(), foo.py_func())

    def test_04(self):
        # two unrolls, one is a const list, one is a tuple
        @njit
        def foo():
            x = [12, 12.7, 3j, 4]
            y = ('foo', 8)
            acc = 0
            for a in literal_unroll(x):
                acc += a
                if acc.real < 26:
                    acc -= 1
                else:
                    for t in literal_unroll(y):
                        acc += t is False
                    break
            return acc

        self.assertEqual(foo(), foo.py_func())


    def test_05(self):
        # fine, list has to be homogeneous in dtype as its an arg
        @njit
        def foo(tup1, tup2):
            acc = 0
            for a in literal_unroll(tup1):
                if a[0] > 1:
                    acc += tup2[0].sum()
            return acc

        n = 10
        tup1 = [np.zeros(10), np.zeros(10)]
        tup2 = (np.ones((n,)), np.ones((n, n)), np.ones((n, n, n)),
                np.ones((n, n, n, n)), np.ones((n, n, n, n, n)))

        with self.assertRaises(errors.UnsupportedError) as raises:
            foo(tup1, tup2)

        msg = "Invalid use of literal_unroll with a function argument"
        self.assertIn(msg, str(raises.exception))

    def test_06(self):
        # illegal: list containing non const
        @njit
        def foo():
            n = 10
            tup = [np.ones((n,)), np.ones((n, n)), "ABCDEFGHJI", (1, 2, 3),
                (1, 'foo', 2, 'bar'), {3, 4, 5, 6, 7}]
            acc = 0
            for a in literal_unroll(tup):
                acc += len(a)
            return acc

        with self.assertRaises(errors.UnsupportedError) as raises:
            foo()

        self.assertIn("Found non-constant value at position 0",
                      str(raises.exception))

    def test_7(self):
        # dispatch on literals triggering @overload resolution

        def dt(value):
            if value == "apple":
                return 1
            elif value == "orange":
                return 2
            elif value == "banana":
                return 3
            elif value == 0xca11ab1e:
                return 0x5ca1ab1e + value

        @overload(dt, inline='always')
        def ol_dt(li):
            if isinstance(li, types.StringLiteral):
                value = li.literal_value
                if value == "apple":
                    def impl(li):
                        return 1
                elif value == "orange":
                    def impl(li):
                        return 2
                elif value == "banana":
                    def impl(li):
                        return 3
                return impl
            elif isinstance(li, types.IntegerLiteral):
                value = li.literal_value
                if value == 0xca11ab1e:
                    def impl(li):
                        # close over the dispatcher :)
                        return 0x5ca1ab1e + value
                    return impl

        @njit
        def foo():
            acc = 0
            for t in literal_unroll(['apple', 'orange', 'banana', 3390155550]):
                acc += dt(t)
            return acc

        self.assertEqual(foo(), foo.py_func())

    def test_8(self):

        @njit
        def foo():
            x = []
            z = ['apple', 'orange', 'banana']
            for i in range(len(literal_unroll(z))):
                t = z[i]
                if t == "apple":
                    x.append("0")
                elif t == "orange":
                    x.append(t)
                elif t == "banana":
                    x.append("2.0")
            return x

        self.assertEqual(foo(), foo.py_func())

    def test_9(self):
        # unroll the same target twice
        @njit
        def foo(idx, z):
            a = [12, 12.7, 3j, 4]
            acc = 0
            for i in literal_unroll(a):
                acc += i
                if acc.real < 26:
                    acc -= 1
                else:
                    for x in literal_unroll(a):
                        acc += x
                    break
            if a[0] < 23:
                acc += 2
            return acc

        f = 9
        k = f

        self.assertEqual(foo(2, k), foo.py_func(2, k))

    def test_10(self):
        # nesting unrolls is illegal
        @njit
        def foo(idx, z):
            a = (12, 12.7, 3j, 4, z, 2 * z)
            b = [12, 12.7, 3j, 4]
            acc = 0
            for i in literal_unroll(a):
                acc += i
                if acc.real < 26:
                    acc -= 1
                else:
                    for x in literal_unroll(a):
                        for j in literal_unroll(b):
                            acc += j
                        acc += x
                for x in literal_unroll(a):
                    acc += x
            for x in literal_unroll(a):
                acc += x
            if a[0] < 23:
                acc += 2
            return acc

        f = 9
        k = f

        with self.assertRaises(errors.UnsupportedError) as raises:
            foo(2, k)

        self.assertIn("Nesting of literal_unroll is unsupported",
                      str(raises.exception))

    def test_11(self):
        # homogeneous const list unroll can return derivative of the induction
        # var

        @njit
        def foo():
            x = [1, 2, 3, 4]
            acc = 0
            for a in literal_unroll(x):
                acc += a
            return a

        self.assertEqual(foo(), foo.py_func())

    def test_12(self):
        # mixed unroll cannot return derivative of the induction var
        @njit
        def foo():
            acc = 0
            x = [1, 2, 'a']
            for a in literal_unroll(x):
                acc += bool(a)
            return a

        n = 5

        with self.assertRaises(errors.TypingError) as raises:
            foo()

        self.assertIn("Cannot unify", str(raises.exception))

    def test_13(self):
        # list slice is illegal

        @njit
        def foo():
            x = [1000, 2000, 3000, 4000]
            acc = ""
            for a in literal_unroll(x[:2]):
                acc += a
            return acc

        with self.assertRaises(errors.UnsupportedError) as raises:
            foo()

        self.assertIn("Invalid use of literal_unroll", str(raises.exception))


if __name__ == '__main__':
    unittest.main()
