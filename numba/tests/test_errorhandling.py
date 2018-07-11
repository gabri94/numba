"""
Unspecified error handling tests
"""
from __future__ import division

from numba import jit, njit
from numba import unittest_support as unittest
from numba import errors, utils


class TestErrorHandlingBeforeLowering(unittest.TestCase):

    expected_msg = ("Numba encountered the use of a language feature it does "
                    "not support in this context: %s")

    def test_unsupported_make_function_lambda(self):
        def func(x):
            f = lambda x: x  # requires `make_function`

        for pipeline in jit, njit:
            with self.assertRaises(errors.UnsupportedError) as raises:
                pipeline(func)(1)

            expected = self.expected_msg % "<lambda>"
            self.assertIn(expected, str(raises.exception))

    def test_unsupported_make_function_listcomp(self):
        try:
            @jit
            def func(x):
                a = [i for i in x]
                return undefined_global  # force error

            with self.assertRaises(errors.UnsupportedError) as raises:
                func([1])

            expected = self.expected_msg % "<listcomp>"
            self.assertIn(expected, str(raises.exception))
        except NameError: #py27 cannot handle the undefined global
            self.assertTrue(utils.PY2)

    def test_unsupported_make_function_dictcomp(self):
        @jit
        def func():
            return {i:0 for i in range(1)}

        with self.assertRaises(errors.UnsupportedError) as raises:
            func()

        expected = self.expected_msg % "<dictcomp>"
        self.assertIn(expected, str(raises.exception))

    def test_unsupported_make_function_return_inner_func(self):
        def func(x):
            """ return the closure """
            z = x + 1

            def inner(x):
                return x + z
            return inner

        for pipeline in jit, njit:
            with self.assertRaises(errors.UnsupportedError) as raises:
                pipeline(func)(1)

            expected = self.expected_msg % \
                "<creating a function from a closure>"
            self.assertIn(expected, str(raises.exception))


if __name__ == '__main__':
    unittest.main()
