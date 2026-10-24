"""Read-only identity must not invoke reflected plugin objects."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = '_nerve_observer_test'
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT / 'hermes_nerve')]
sys.modules[PACKAGE] = package
spec = importlib.util.spec_from_file_location(PACKAGE + '.provenance', ROOT / 'hermes_nerve/provenance.py')
assert spec is not None and spec.loader is not None
observer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = observer
spec.loader.exec_module(observer)


class ObserverBoundaryTests(unittest.TestCase):
    def test_opaque_constant_cannot_dispatch_repr(self):
        calls = []
        class Value:
            def __repr__(self):
                calls.append('repr')
                return 'opaque'
        with self.assertRaises(TypeError):
            observer._canonical_code_value(Value())
        self.assertEqual(calls, [])

    def test_tuple_subclass_cannot_dispatch_iteration(self):
        calls = []
        class Value(tuple):
            def __iter__(self):
                calls.append('iter')
                return super().__iter__()
        with self.assertRaises(TypeError):
            observer._canonical_code_value(Value(('literal',)))
        self.assertEqual(calls, [])

    def test_exact_constant_check_cannot_dispatch_metaclass_equality(self):
        calls = []
        class Meta(type):
            def __eq__(self, other):
                calls.append('eq')
                return False
        class Value(metaclass=Meta):
            pass
        self.assertIs(observer._immutable_identity_constant(Value()), False)
        self.assertEqual(calls, [])

    def test_code_normalization_cannot_dispatch_arbitrary_constant_repr(self):
        calls = []
        class Value:
            def __repr__(self):
                calls.append('repr')
                return 'value'
        def example():
            return 1
        code = example.__code__.replace(co_consts=(None, Value()))
        with self.assertRaises(TypeError):
            observer._code_digest([code])
        self.assertEqual(calls, [])

    def test_unknown_class_metaclass_is_not_observed_via_vars(self):
        calls = []
        class Meta(type):
            def __getattribute__(self, key):
                calls.append(key)
                return super().__getattribute__(key)
        class Value(metaclass=Meta):
            pass
        self.assertEqual(observer._code_objects(Value), [])
        self.assertEqual(calls, [])

    def test_code_walk_cannot_dispatch_constant_class_hook(self):
        calls = []
        class Value:
            @property
            def __class__(self):
                calls.append('class')
                return object
        def example():
            return 1
        code = example.__code__.replace(co_consts=(None, Value()))
        self.assertEqual(observer._all_code_objects(code), [code])
        self.assertEqual(calls, [])

    def test_custom_descriptor_wrapper_uses_builtin_getter(self):
        calls = []
        class Wrapper(staticmethod):
            def __getattribute__(self, name):
                calls.append(name)
                return super().__getattribute__(name)
        def example():
            return 1
        self.assertEqual(observer._code_objects(Wrapper(example)), [example.__code__])
        self.assertEqual(calls, [])

    def test_tampered_opaque_constant_yields_unverifiable_not_a_match(self):
        calls = []
        class Value:
            def __repr__(self):
                calls.append('repr')
                return 'opaque'
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'observed.py'
            path.write_text('def example():\n    return 1\n')
            spec = importlib.util.spec_from_file_location('_observed_boundary', path)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertEqual(*observer._module_code_fingerprints(module))
            module.example.__code__ = module.example.__code__.replace(co_consts=(None, Value()))
            live, disk = observer._module_code_fingerprints(module)
            self.assertEqual(disk, '')
            self.assertNotEqual(live, disk)
        self.assertEqual(calls, [])

    def test_code_qualname_subclass_cannot_dispatch_sorting(self):
        calls = []
        class Text(str):
            def __lt__(self, other):
                calls.append('lt')
                return super().__lt__(other)
        def example():
            return 1
        code = example.__code__.replace(co_qualname=Text('altered'))
        with self.assertRaises(TypeError):
            observer._code_digest([code, example.__code__])
        self.assertEqual(calls, [])

    def test_nested_code_metadata_cannot_dispatch_repr_during_sort(self):
        calls = []
        class Text(str):
            def __repr__(self):
                calls.append('repr')
                return 'unsafe'
        def example():
            return 1
        try:
            inner = example.__code__.replace(co_names=(Text('name'),))
        except (SystemError, TypeError):
            # CPython rejects non-exact names at construction on this runtime.
            self.assertEqual(calls, [])
            return
        outer = example.__code__.replace(co_consts=(frozenset({inner}),))
        with self.assertRaises(TypeError):
            observer._code_digest([outer])
        self.assertEqual(calls, [])

    def test_compiler_slice_constant_remains_observable(self):
        def example(value):
            return value[1:3]
        digest = observer._code_digest([example.__code__])
        self.assertRegex(digest, r'^[0-9a-f]{64}$')
        self.assertEqual(observer._canonical_code_value(slice(1, 3)),
                         {'type': 'slice', 'repr': 'slice(1, 3, None)'})

    def test_slice_with_opaque_component_cannot_dispatch_repr(self):
        calls = []
        class Value:
            def __repr__(self):
                calls.append('repr')
                return 'opaque'
        with self.assertRaises(TypeError):
            observer._canonical_code_value(slice(Value(), 3))
        self.assertEqual(calls, [])

    def test_ordinary_immutable_constants_and_code_remain_stable(self):
        def example():
            return ('literal', 1, frozenset({'a', 'b'}))
        first = observer._code_digest([example.__code__])
        self.assertRegex(first, r'^[0-9a-f]{64}$')
        self.assertEqual(first, observer._code_digest([example.__code__]))
        for value in [None, 'a', b'a', True, 1, 1.5, 1+2j, ('a', 2), frozenset({'a', 'b'})]:
            self.assertIs(observer._immutable_identity_constant(value), True)
            self.assertTrue(observer._canonical_code_value(value))


if __name__ == '__main__':
    unittest.main()
