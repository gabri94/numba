from __future__ import print_function, absolute_import
import warnings
from numba import jit, autojit
from numba.errors import (NumbaDeprecationWarning,
                          NumbaPendingDeprecationWarning, NumbaWarning)
import numba.unittest_support as unittest


class TestDeprecation(unittest.TestCase):

    def test_autojit(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")

            def dummy():
                pass
            autojit(dummy)
            self.assertEqual(len(w), 1)

    def check_warning(self, warnings, expected_str, category):
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].category, category)
        self.assertIn(expected_str, str(warnings[0].message))
        self.assertIn("http://numba.pydata.org", str(warnings[0].message))

    def test_jitfallback(self):
        # tests that @jit falling back to object mode raises a
        # NumbaDeprecationWarning
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("ignore", category=NumbaWarning)
            warnings.simplefilter("always", category=NumbaDeprecationWarning)

            def foo():
                return []  # empty list cannot be typed
            jit(foo)()

            msg = ("Fall-back from the nopython compilation path to the object "
                   "mode compilation path")
            self.check_warning(w, msg, NumbaDeprecationWarning)

    def test_reflection_of_mutable_container(self):
        # tests that reflection in list/set warns
        def foo_list(a):
            return a.append(1)

        def foo_set(a):
            return a.add(1)

        for f, depclazz in ((foo_list, NumbaDeprecationWarning),
                            (foo_set, NumbaPendingDeprecationWarning)):
            container = f.__name__.strip('foo_')
            inp = eval(container)([10, ])
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("ignore", category=NumbaWarning)
                warnings.simplefilter("always",
                                      category=depclazz)
                jit(nopython=True)(f)(inp)
                self.assertEqual(len(w), 1)
                self.assertEqual(w[0].category, depclazz)
                warn_msg = str(w[0].message)
                msg = (".*Encountered the use of a type that is.*deprecat.*")
                self.assertRegexpMatches(warn_msg, msg)
                msg = ("\'reflected %s\' found for argument" % container)
                self.assertIn(msg, warn_msg)
                self.assertIn("http://numba.pydata.org", warn_msg)


if __name__ == '__main__':
    unittest.main()
